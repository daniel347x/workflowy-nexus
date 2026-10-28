"""U7 ([ap:256S33]) — slice-group read-modify-write regression suite.

Pins the three-part fix for the P7LEUA tether (sequential single-field
``slice_group.update`` calls reverting each other, and the ``[-N]``
order-index suffix flickering inside the stored name):

1. the DURABLE merge base — post-fix, the browser answers
   ``get_slice_group_config`` from ``chrome.storage.local`` via the service
   worker (browser side pinned by ``tests/js/test_slice_group_durable_read.js``;
   here the fake channel models it as the ``stored`` read source),
2. the server-side ``_SLICE_GROUP_CONFIG_LOCK`` serializing the four RMW
   sites (update / create / delete / merge.persist) so even genuinely
   parallel tool calls cannot interleave a stale base,
3. ``[-N]`` / ``[n]`` order-index preservation on rename via the ONE
   grammar parser (``parse_slice_group_name``).

The races are FORCED, not timed (spec §9): staleness is injected by
construction — a scripted late broadcast echo that reverts the fake tab
mirror, and an artificial read delay that forces the read-read-write-write
interleave — because the live loopback is too fast for the echo to win by
timing (RECON-1 could not reproduce it that way).

Fakes the Direction-A WS transports after the repo's established patterns
(tests/test_merge_slice_groups.py · tests/test_sgsort_default_sort.py);
no live account, no browser.
"""

import asyncio
import copy

import pytest

import workflowy_mcp.server as server


ROOT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def fresh_config_lock(monkeypatch):
    """Give every test its OWN _SLICE_GROUP_CONFIG_LOCK.

    Production runs one event loop for the daemon's lifetime, so the one
    module-level asyncio.Lock is correct there (the same idiom as the
    server's existing _job_lock / _carto_jobs_lock). pytest, however, spins
    a fresh loop per asyncio.run, and a CONTENDED asyncio.Lock binds to the
    loop that first contended it — the parallel tests below would otherwise
    poison each other across closed loops. A fresh lock per test keeps the
    production object untouched while each test's loops see only their own
    lock. ``raising=False`` so the fixture also works against the PRE-FIX
    tree (where the attribute does not exist yet) — the pre-fix failures
    stay behavioral, not fixture errors.
    """
    monkeypatch.setattr(
        server, "_SLICE_GROUP_CONFIG_LOCK", asyncio.Lock(), raising=False
    )


def _group(
    gid="g1",
    name="Nexus | Meta-Meta · Curation (uncurated tail)[-2000]",
    description="OLD DESC",
    prefixes=("nexus--meta-meta",),
):
    return {
        "id": gid,
        "name": name,
        "prefixes": list(prefixes),
        "root_uuid": ROOT,
        "description": description,
        "default_sort_by": "none",
        "default_sort_direction": "asc",
    }


class FakeConfigChannel:
    """Fake Direction-A config channel modeling the browser side of U7.

    ``stored_*`` is the DURABLE store (``chrome.storage.local``): it advances
    on every acked ``set_slice_groups``. Post-fix, the page handler answers
    ``get_slice_group_config`` from THIS (via the SW ``get_expand_groups``
    bridge kind).

    ``mirror_*`` is the serving tab's in-memory ``extensionSettings`` mirror:
    updated SYNCHRONOUSLY on write (``persistExpandGroups``' self-update) and
    then possibly REVERTED by a scripted late broadcast echo
    (``arm_echo_revert``) — the forced, deterministic form of the P7LEUA
    wound. Pre-fix, the page handler answered reads from THIS.

    ``read_source`` selects which copy the fake read answers from
    (``'stored'`` = post-fix durable read; ``'mirror'`` = pre-fix path, kept
    for the in-suite failure demonstration). ``read_delay_s`` widens the read
    window so the parallel-call interleave is forced, not hoped for.
    """

    def __init__(self, groups, active_group=None, read_source="stored",
                 read_delay_s=0.0):
        self.stored_groups = copy.deepcopy(groups)
        self.stored_active = active_group
        self.mirror_groups = copy.deepcopy(groups)
        self.mirror_active = active_group
        self.read_source = read_source
        self.read_delay_s = read_delay_s
        self.echo_revert_to = None
        self.reads = 0
        self.writes = 0
        self.read_log = []

    def arm_echo_revert(self):
        """Arm the FORCED stale echo: after the next write's synchronous
        mirror update, a late ``storage.onChanged`` broadcast (carrying the
        pre-write state) reverts the mirror — Manager-Session-007's wound by
        construction, never by timing."""
        self.echo_revert_to = (
            copy.deepcopy(self.mirror_groups),
            self.mirror_active,
        )

    # --- the /nodes-export cache the create core now resolves against -----
    #
    # root_handle resolution (slice_group.create root_handle) runs the real
    # shared UUID/prefix/AP-ID boundary over the live nodes cache before any
    # config write, so the fake must expose the real ROOT this suite stores
    # (a full UUID) or create would now fail its mandatory root validation.

    def _get_nodes_export_cache_nodes(self):
        return [
            {
                "id": ROOT,
                "name": "RMW Root",
                "note": "",
                "parent_id": None,
                "priority": 0,
            }
        ]

    # --- fake WorkFlowyClientNexus Direction-A transports -----------------

    async def get_slice_group_config(self, *, ws_connection, ws_queue,
                                     target_tab=None, target_endpoint_id=None,
                                     tab_optional=True, timeout_s=15.0):
        # The read's ANSWER is captured when the read STARTS (that is what the
        # serving side saw); the transport delay lands AFTER the capture, so a
        # concurrent RMW's ack can land inside the window. This is what forces
        # the read-read-write-write interleave deterministically — a sleep
        # BEFORE the capture would let the first task's whole (synchronous
        # after the read) RMW complete before the second task even looks, and
        # the race would never happen.
        if self.read_source == "mirror":
            groups, active = self.mirror_groups, self.mirror_active
        else:
            groups, active = self.stored_groups, self.stored_active
        captured = copy.deepcopy(groups)
        if self.read_delay_s:
            await asyncio.sleep(self.read_delay_s)
        self.reads += 1
        self.read_log.append(captured)
        return {
            "success": True,
            "expand_groups": captured,
            "active_group": active,
        }

    async def set_slice_groups(self, *, ws_connection, ws_queue, expand_groups,
                               active_group=None, target_tab=None,
                               tab_optional=True, timeout_s=15.0):
        self.writes += 1
        self.stored_groups = copy.deepcopy(expand_groups)
        self.stored_active = active_group
        # persistExpandGroups' synchronous mirror self-update ...
        self.mirror_groups = copy.deepcopy(expand_groups)
        self.mirror_active = active_group
        # ... THEN the scripted late echo reverts the mirror once (forced).
        if self.echo_revert_to is not None:
            self.mirror_groups, self.mirror_active = self.echo_revert_to
            self.echo_revert_to = None
        return {
            "success": True,
            "expand_groups": copy.deepcopy(self.stored_groups),
            "active_group": self.stored_active,
        }


def _install(channel, monkeypatch):
    """Wire the channel in as the Direction-A transport (the repo's fake
    idiom: get_client + get_ws_connection_with_grace_wait)."""

    async def fake_ws(*args, **kwargs):
        return object(), object()

    monkeypatch.setattr(server, "get_client", lambda: channel)
    monkeypatch.setattr(
        server, "get_ws_connection_with_grace_wait", fake_ws
    )


# ---------------------------------------------------------------------------
# §9.1 — the tether repro, deterministically
# ---------------------------------------------------------------------------


class TestTetherRepro:
    def test_sequential_updates_on_durable_base_keep_both_fields(
        self, monkeypatch
    ):
        """Post-fix expectation (spec §9.1): name-then-description on an
        order-index group, over the DURABLE read base, keeps BOTH fields and
        preserves the [-2000] suffix. A late echo still reverts the mirror —
        nothing reads it anymore.

        Mutation check: FAILS pre-fix — the pre-fix update core stores the
        rename verbatim, dropping the omitted [-2000] (the suffix assertions
        below fail on r1 already).
        """
        channel = FakeConfigChannel([_group()], active_group="g1",
                                    read_source="stored")
        _install(channel, monkeypatch)
        channel.arm_echo_revert()

        async def run():
            r1 = await server._slice_group_update_core(
                group_id="g1",
                new_name="Nexus | Manager Sessions · Curation (uncurated tail)",
            )
            # The echo has fired by now: the mirror is REVERTED to the
            # pre-write state — and it no longer matters, because the next
            # merge base is the durable store.
            assert channel.mirror_groups[0]["name"] == (
                "Nexus | Meta-Meta · Curation (uncurated tail)[-2000]"
            )
            r2 = await server._slice_group_update_core(
                group_id="g1",
                description="Manager Sessions curation view",
            )
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1["success"] is True, r1
        # U7 postcondition 3: the omitted index is preserved on the rename.
        assert r1["slice_group"]["name"] == (
            "Nexus | Manager Sessions · Curation (uncurated tail) [-2000]"
        )
        assert r1["slice_group"]["order_index"] == -2000
        assert r1["slice_group"]["description"] == "OLD DESC"
        assert r2["success"] is True, r2

        final = channel.stored_groups[0]
        assert final["description"] == "Manager Sessions curation view"
        assert final["name"] == (
            "Nexus | Manager Sessions · Curation (uncurated tail) [-2000]"
        )
        view = r2["slice_group"]
        assert view["order_index"] == -2000
        # short_name is the final PIPE segment with the index stripped.
        assert view["short_name"] == (
            "Manager Sessions · Curation (uncurated tail)"
        )
        assert view["description"] == "Manager Sessions curation view"

    def test_forced_stale_mirror_reverts_name_demonstration(self, monkeypatch):
        """The pre-fix failure demonstration (spec §9.1 parenthetical / §11.2):
        when the read IS answered from a mirror that a late echo reverted (the
        pre-fix page handler), the second update merges onto the stale base and
        the NAME REVERTS — the exact Manager-Session-007 symptom, forced by
        construction instead of timing.

        Honest scope note: this mode passes on BOTH trees — the server trusts
        its transport, and the Python-side fix (lock + index preservation)
        cannot repair a lying read. What eliminates this mode is the
        BROWSER-side durable read, pinned behaviorally by
        tests/js/test_slice_group_durable_read.js (test 5/6). This test is the
        durable in-suite record of WHY the durable read is load-bearing.
        """
        channel = FakeConfigChannel([_group()], active_group="g1",
                                    read_source="mirror")
        _install(channel, monkeypatch)
        channel.arm_echo_revert()

        async def run():
            r1 = await server._slice_group_update_core(
                group_id="g1",
                new_name="Nexus | Manager Sessions · Curation (uncurated tail)",
            )
            r2 = await server._slice_group_update_core(
                group_id="g1",
                description="Manager Sessions curation view",
            )
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1["success"] is True, r1
        assert "Manager Sessions" in r1["slice_group"]["name"]
        assert r2["success"] is True, r2

        final = channel.stored_groups[0]
        # The wound: the description stuck, the name REVERTED to the stale
        # base's — with its [-2000] suffix riding back inside it.
        assert final["description"] == "Manager Sessions curation view"
        assert final["name"] == (
            "Nexus | Meta-Meta · Curation (uncurated tail)[-2000]"
        )


# ---------------------------------------------------------------------------
# §9.2 — parallel calls serialize on the lock
# ---------------------------------------------------------------------------


class TestParallelSerialization:
    def test_parallel_name_and_description_updates_both_survive(
        self, monkeypatch
    ):
        """Two concurrent update calls on one group with a delayed fake read.

        Mutation check: FAILS pre-fix — the 50ms read delay forces the
        read-read-write-write interleave (both tasks read the same base
        before either writes), so the second writer silently drops the
        first's field. Post-fix, _SLICE_GROUP_CONFIG_LOCK serializes the two
        RMWs: the second read happens only after the first's ack, and both
        fields survive.
        """
        channel = FakeConfigChannel(
            [_group(name="G", description="OLD")],
            active_group="g1",
            read_source="stored",
            read_delay_s=0.05,
        )
        _install(channel, monkeypatch)

        async def run():
            return await asyncio.gather(
                server._slice_group_update_core(group_id="g1",
                                                new_name="NEWNAME"),
                server._slice_group_update_core(group_id="g1",
                                                description="NEWDESC"),
            )

        r_name, r_desc = asyncio.run(run())
        assert r_name["success"] is True, r_name
        assert r_desc["success"] is True, r_desc

        final = channel.stored_groups[0]
        assert final["name"] == "NEWNAME"
        assert final["description"] == "NEWDESC"
        # Serialized, not interleaved: each RMW read exactly once, and the
        # second read saw the first's write.
        assert channel.reads == 2 and channel.writes == 2
        assert channel.read_log[1][0]["name"] == "NEWNAME"

    def test_parallel_create_and_update_do_not_drop_the_create(
        self, monkeypatch
    ):
        """Symmetric shape across two DIFFERENT RMW sites: a parallel
        create + update serialize on the same lock, so the create is not
        lost from the array when the update's write lands last.

        Mutation check: FAILS pre-fix whenever the interleave lands
        read-read-write-write (forced here by the read delay) — the update's
        whole-array write resurrects the pre-create array.
        """
        channel = FakeConfigChannel(
            [_group(name="G", description="OLD")],
            active_group="g1",
            read_source="stored",
            read_delay_s=0.05,
        )
        _install(channel, monkeypatch)

        async def run():
            return await asyncio.gather(
                server._slice_group_create_core(
                    name="Brand New", tags=["brand-new"], root_uuid=ROOT,
                ),
                server._slice_group_update_core(group_id="g1",
                                                description="NEWDESC"),
            )

        r_create, r_update = asyncio.run(run())
        assert r_create["success"] is True, r_create
        assert r_update["success"] is True, r_update

        names = [g["name"] for g in channel.stored_groups]
        assert "Brand New" in names
        assert len(channel.stored_groups) == 2
        updated = next(g for g in channel.stored_groups if g["id"] == "g1")
        assert updated["description"] == "NEWDESC"


# ---------------------------------------------------------------------------
# §9.3 — suffix grammar (pure update-core + parse_slice_group_name level)
# ---------------------------------------------------------------------------


class TestOrderIndexPreservation:
    def _rename(self, monkeypatch, stored_name, new_name):
        channel = FakeConfigChannel([_group(name=stored_name)],
                                    active_group="g1")
        _install(channel, monkeypatch)
        r = asyncio.run(server._slice_group_update_core(
            group_id="g1", new_name=new_name,
        ))
        assert r["success"] is True, r
        return channel.stored_groups[0]["name"], r["slice_group"]

    def test_rename_without_index_preserves_stored_trailing(self, monkeypatch):
        """Mutation check: FAILS pre-fix (the rename stored the supplied
        string verbatim, silently dropping the [-2000])."""
        stored, view = self._rename(
            monkeypatch,
            "Nexus | Meta-Meta · Curation (uncurated tail)[-2000]",
            "Nexus | Manager Sessions · Curation (uncurated tail)",
        )
        assert stored == (
            "Nexus | Manager Sessions · Curation (uncurated tail) [-2000]"
        )
        assert view["order_index"] == -2000
        assert view["is_expander"] is False

    def test_rename_with_explicit_index_honors_it(self, monkeypatch):
        """An explicitly supplied index is honored verbatim — that is how an
        index CHANGES (passes pre-fix too: verbatim storage already honored
        it; this is the other half of the preservation rule's contract)."""
        stored, view = self._rename(
            monkeypatch, "Alpha|Beta [-2000]", "Alpha|Gamma [-100]",
        )
        assert stored == "Alpha|Gamma [-100]"
        assert view["order_index"] == -100

    def test_expander_rename_preserves_leading_index(self, monkeypatch):
        """Mutation check: FAILS pre-fix (the leading [2] expander index was
        dropped by a verbatim rename)."""
        stored, view = self._rename(monkeypatch, "[2]A|B", "A|B|C")
        assert stored == "[2]A|B|C"
        assert view["is_expander"] is True
        assert view["order_index"] == 2

    def test_supplied_expander_form_over_stored_leaf_index(self, monkeypatch):
        """Symmetric shape: stored LEAF index, supplied name carries an
        explicit LEADING expander index — the supplied index wins (it IS an
        explicit index; the row becomes an expander)."""
        stored, view = self._rename(monkeypatch, "Leaf [-2000]", "[5]Cat|Leaf")
        assert stored == "[5]Cat|Leaf"
        assert view["is_expander"] is True
        assert view["order_index"] == 5

    def test_no_stored_index_rename_stays_verbatim(self, monkeypatch):
        stored, view = self._rename(monkeypatch, "Plain Name", "Renamed Plain")
        assert stored == "Renamed Plain"
        assert view["order_index"] is None

    def test_helper_defers_to_the_one_grammar_parser(self):
        """The preservation rule never hand-parses: both names go through
        parse_slice_group_name (f8_slice_policy) — the two-runtime grammar
        authority this unit uses but does not edit."""
        from workflowy_mcp.f8_slice_policy import parse_slice_group_name

        leaf = parse_slice_group_name("A|B [-2000]")
        assert leaf["index"] == -2000 and leaf["is_expander"] is False
        expander = parse_slice_group_name("[2]A|B")
        assert expander["index"] == 2 and expander["is_expander"] is True

        assert server._reattach_stored_order_index("A [-2000]", "B") == \
            "B [-2000]"
        assert server._reattach_stored_order_index("[2]A|B", "C|D") == \
            "[2]C|D"
        assert server._reattach_stored_order_index("A [-2000]", "B [-1]") == \
            "B [-1]"
        assert server._reattach_stored_order_index("A", "B") == "B"


# ---------------------------------------------------------------------------
# §9.4 — read-your-writes
# ---------------------------------------------------------------------------


class TestReadYourWrites:
    def test_get_immediately_after_update_reflects_it(self, monkeypatch):
        """slice_group.get right after an update reflects the update: the
        durable base advanced with the acked write and the lock was RELEASED
        after the ack (a follow-up read through the same transport completes
        — no deadlock, no held lock).

        Honest mutation note: passes pre-fix too against this fake durable
        channel (its job is to pin the lock-release/read-your-writes
        semantics the lock introduces, not to fail on the old tree).
        """
        channel = FakeConfigChannel([_group()], active_group="g1")
        _install(channel, monkeypatch)

        async def run():
            r1 = await server._slice_group_update_core(
                group_id="g1", description="FRESH",
            )
            got = await server._slice_group_get_impl("g1", None)
            return r1, got

        r1, got = asyncio.run(run())
        assert r1["success"] is True, r1
        assert got["success"] is True, got
        assert got["slice_group"]["description"] == "FRESH"
        assert got["slice_group"]["name"] == channel.stored_groups[0]["name"]
        lock = getattr(server, "_SLICE_GROUP_CONFIG_LOCK", None)
        assert lock is None or not lock.locked()
