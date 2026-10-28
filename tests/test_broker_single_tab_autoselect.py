"""Regression tests for GLIMPSE broker single-tab auto-selection + dead-endpoint pruning.

Symptom fixed here (Dan, 2026): with exactly ONE Workflowy tab open, an
untargeted GLIMPSE would sometimes hang for ~15s (the MCP-side reply timeout)
instead of routing to that one tab. Clicking "Set default" in the tab's UUID
Explorer widget worked around it 100% of the time.

Root cause: the broker's browser-facing listener runs with protocol autoping
DISABLED (a slow-but-alive tab must not be reaped). The trade-off is that a
GENUINELY-dead browser socket (Chrome MV3 idle-kill / network churn) can linger
in the registry until its close event is finally observed. During that window
``resolve_target_endpoint`` sees ``len(endpoints) == 1`` and auto-selects the
STALE endpoint (step 3); the forward ``_safe_send`` swallows the failed write,
no reply ever comes back, and the MCP side times out. "Set default" masked it
because the live tab re-registered its endpoint on its current live socket.

Fix: ``Broker.prune_dead_endpoints`` drops endpoints whose owning socket is not
OPEN, and it is invoked at the top of ``resolve_target_endpoint`` /
``resolve_target_tab`` so routing decisions are made against LIVE endpoints
only. The literal "one connected tab -> auto-select it" behavior was already
present; these tests lock in that it now holds even when a dead endpoint is
still registered, AND that the multi-tab ambiguity / zero-tab error paths are
preserved.
"""

from __future__ import annotations

import pytest

from workflowy_mcp.broker import Broker


class _FakeState:
    """Mimic websockets' State enum members enough for _ws_is_live (name check)."""

    def __init__(self, name: str) -> None:
        self.name = name


# Singletons mirroring websockets.protocol.State names used by _ws_is_live.
OPEN = _FakeState("OPEN")
CLOSED = _FakeState("CLOSED")
CLOSING = _FakeState("CLOSING")


class FakeWS:
    """Minimal stand-in for a websockets connection.

    Only needs a ``.state`` with a ``.name`` (the modern websockets>=10 shape
    that ``_ws_is_live`` reads first). ``id(ws)`` is used by the broker as the
    reverse-lookup key, so distinct instances are distinct sockets.
    """

    def __init__(self, state: _FakeState = OPEN) -> None:
        self.state = state


def _new_broker() -> Broker:
    return Broker(required_token="test-token")


def _register_tab(broker: Broker, conn_id: str, endpoint_id: str, alias=None) -> FakeWS:
    """Register a browser connection carrying a single endpoint; return its ws."""
    ws = FakeWS(OPEN)
    broker.register_browser_connection(ws, conn_id)
    ep, collision = broker.register_endpoint(ws, endpoint_id, alias, None, None, None)
    assert ep is not None and not collision
    return ws


# --------------------------------------------------------------------------
# Single-tab auto-selection
# --------------------------------------------------------------------------

def test_single_live_tab_autoselected_no_target():
    """One live tab, no target, no default -> that tab is auto-selected."""
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-1")

    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is None
    assert ep is not None
    assert ep.endpoint_id == "ep-1"


def test_single_live_tab_autoselected_via_public_target_tab_none():
    """Same, but through the public resolve_target_tab(None) entry point."""
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-1")

    ep, err = broker.resolve_target_tab(None)
    assert err is None
    assert ep is not None
    assert ep.endpoint_id == "ep-1"


# --------------------------------------------------------------------------
# The actual bug: a dead-but-registered endpoint must not be auto-selected
# --------------------------------------------------------------------------

def test_stale_endpoint_pruned_then_live_tab_autoselected():
    """A dead endpoint lingers; the live tab re-registered on a new socket.

    Before the fix, ``len(endpoints) == 2`` -> ambiguous error (or, if the
    only registered endpoint was the stale one, a 15s black-hole). After the
    fix, the dead endpoint is pruned and the single live tab is auto-selected.
    """
    broker = _new_broker()
    # Stale connection: socket already CLOSED but still in the registry.
    stale_ws = FakeWS(OPEN)
    broker.register_browser_connection(stale_ws, "chrome-stale")
    broker.register_endpoint(stale_ws, "ep-old", None, None, None, None)
    stale_ws.state = CLOSED  # SW died; close not yet observed by the broker.

    # Live reconnect: same tab, fresh socket + endpoint.
    _register_tab(broker, "chrome-live", "ep-new")

    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is None, f"expected auto-select, got error: {err}"
    assert ep is not None
    assert ep.endpoint_id == "ep-new"
    # The stale endpoint is gone from the registry entirely.
    assert broker.endpoint_by_id("ep-old") is None


def test_only_stale_endpoint_yields_fast_no_browser_error_not_timeout():
    """If the ONLY registered endpoint is dead, we must fail FAST, not route.

    This is the exact 15s-timeout scenario: a single stale endpoint would be
    auto-selected (step 3) and the send would silently black-hole. Post-fix the
    dead endpoint is pruned first, leaving zero endpoints -> the immediate
    ``no_browser_connected`` error instead of a hung send.
    """
    broker = _new_broker()
    dead_ws = FakeWS(OPEN)
    broker.register_browser_connection(dead_ws, "chrome-dead")
    broker.register_endpoint(dead_ws, "ep-dead", None, None, None, None)
    dead_ws.state = CLOSED

    ep, err = broker.resolve_target_endpoint(None, None)
    assert ep is None
    assert err is not None
    assert err["code"] == "no_browser_connected"


# --------------------------------------------------------------------------
# Preserve the documented multi-tab / zero-tab behavior
# --------------------------------------------------------------------------

def test_two_live_tabs_no_target_still_ambiguous_error():
    """2+ live tabs, no target, no default -> actionable ambiguity error."""
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-1", alias="spine")
    _register_tab(broker, "chrome-B", "ep-2", alias="debug")

    ep, err = broker.resolve_target_endpoint(None, None)
    assert ep is None
    assert err is not None
    assert err["code"] == "ambiguous_browser_target"
    # Lists the available tabs (aliases included) so the caller/human can pick.
    listed = {e["endpoint_id"] for e in err["available_endpoints"]}
    assert listed == {"ep-1", "ep-2"}


def test_two_tabs_one_dead_collapses_to_single_autoselect():
    """2 registered, 1 dead -> pruned to 1 live -> auto-selected (no error)."""
    broker = _new_broker()
    live_ws = _register_tab(broker, "chrome-A", "ep-live")
    dead_ws = FakeWS(OPEN)
    broker.register_browser_connection(dead_ws, "chrome-B")
    broker.register_endpoint(dead_ws, "ep-dead", None, None, None, None)
    dead_ws.state = CLOSING  # non-OPEN => treated as dead

    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is None
    assert ep is not None
    assert ep.endpoint_id == "ep-live"
    assert live_ws.state is OPEN  # untouched


def test_alias_target_ignores_dead_endpoint():
    """Targeting by alias must not match a stale endpoint that shares it."""
    broker = _new_broker()
    dead_ws = FakeWS(OPEN)
    broker.register_browser_connection(dead_ws, "chrome-stale")
    broker.register_endpoint(dead_ws, "ep-old", "spine", None, None, None)
    dead_ws.state = CLOSED
    _register_tab(broker, "chrome-live", "ep-new", alias="spine")

    ep, err = broker.resolve_target_tab("spine")
    assert err is None
    assert ep is not None
    assert ep.endpoint_id == "ep-new"


def test_zero_tabs_error_unchanged():
    """No tabs at all -> the same no_browser_connected error."""
    broker = _new_broker()
    ep, err = broker.resolve_target_endpoint(None, None)
    assert ep is None
    assert err is not None
    assert err["code"] == "no_browser_connected"


def test_prune_returns_count_and_is_idempotent():
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-live")
    dead_ws = FakeWS(OPEN)
    broker.register_browser_connection(dead_ws, "chrome-B")
    broker.register_endpoint(dead_ws, "ep-dead", None, None, None, None)
    dead_ws.state = CLOSED

    assert broker.prune_dead_endpoints() == 1
    assert broker.prune_dead_endpoints() == 0  # nothing left to prune
    assert broker.endpoint_by_id("ep-dead") is None
    assert broker.endpoint_by_id("ep-live") is not None


def test_pruning_clears_stale_default_designation():
    """A pruned endpoint that was the default self-clears the default."""
    broker = _new_broker()
    ws = _register_tab(broker, "chrome-A", "ep-1")
    _status, _ep = broker.set_default_endpoint("ep-1")
    assert _status == "set"
    assert broker.default_endpoint_id == "ep-1"

    ws.state = CLOSED
    broker.prune_dead_endpoints()
    assert broker.default_endpoint_id is None


# --------------------------------------------------------------------------
# [ap:WSFLAP2] ghost-endpoint 'Set default' must not CLOBBER a good default
# --------------------------------------------------------------------------
#
# Symptom fixed here (Dan, 2026-07-13): a pinned tab whose endpoint was dropped
# during an MV3 service-worker flap (a GHOST) clicks 'Set default'. Its stale
# endpoint_id reaches the broker. The OLD set_default_endpoint nulled
# default_endpoint_id in BOTH the explicit-clear AND the unknown-id branches,
# so the ghost click DESTROYED whatever tab was previously the default (e.g.
# 'NOW') while failing to install itself -> nobody default. The fix rejects an
# unknown id WITHOUT mutating state, returning status 'rejected'.


def test_ghost_set_default_does_not_clobber_existing_default():
    """A set_default for an UNKNOWN endpoint_id leaves the live default intact."""
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-live")
    # ep-live is the healthy, roster-present default (the 'NOW' tab).
    status, ep = broker.set_default_endpoint("ep-live")
    assert status == "set" and ep is not None
    assert broker.default_endpoint_id == "ep-live"

    # A GHOST tab (endpoint never registered / dropped in a flap) clicks
    # 'Set default' with its stale id.
    status2, ep2 = broker.set_default_endpoint("ep-ghost-stale")

    # It is REJECTED, and -- the whole point -- the existing default is
    # UNTOUCHED (pre-fix this would have been None: the destructive clobber).
    assert status2 == "rejected"
    assert ep2 is None
    assert broker.default_endpoint_id == "ep-live"


def test_explicit_clear_still_nulls_default():
    """The explicit-clear path (endpoint_id=None) remains the ONLY nulling path."""
    broker = _new_broker()
    _register_tab(broker, "chrome-A", "ep-live")
    broker.set_default_endpoint("ep-live")
    assert broker.default_endpoint_id == "ep-live"

    status, ep = broker.set_default_endpoint(None)
    assert status == "cleared"
    assert ep is None
    assert broker.default_endpoint_id is None


# --------------------------------------------------------------------------
# Option B: SW-authoritative endpoint roster reconciliation
# --------------------------------------------------------------------------
#
# The service worker (one socket per browser profile, multiplexing many tabs)
# is the ONLY component that can observe which tabs/endpoints currently exist.
# It reports its live endpoint roster on each keepalive ping; the broker
# reconciles registered endpoints against it. This is what heals a KILLED tab
# whose endpoint_unregister frame was lost -- the exact ghost that made a
# single-open-tab GLIMPSE see a phantom second tab and error/timeout.


def test_authoritative_roster_reaps_ghost_endpoint_same_connection():
    """Killed tab (still registered) reaped when SW's complete roster omits it.

    Both endpoints are on ONE browser connection (one SW socket, two tabs).
    The SW now reports only the surviving tab as live -> the ghost is reaped
    -> a subsequent untargeted GLIMPSE auto-selects the survivor.
    """
    broker = _new_broker()
    sw = FakeWS(OPEN)  # one service-worker socket
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-ghost", None, None, None, "tab-A")
    broker.register_endpoint(sw, "ep-live", "spine", None, None, "tab-B")
    assert len(broker.all_endpoints()) == 2

    reaped = broker.reconcile_browser_roster(sw, ["ep-live"], roster_complete=True)
    assert reaped == 1
    assert broker.endpoint_by_id("ep-ghost") is None
    assert broker.endpoint_by_id("ep-live") is not None

    # And the survivor is now auto-selected with no target/default.
    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is None
    assert ep is not None and ep.endpoint_id == "ep-live"


def test_incomplete_roster_never_prunes():
    """A non-authoritative (settling) roster refreshes liveness but reaps nothing."""
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-1", None, None, None, "tab-A")
    broker.register_endpoint(sw, "ep-2", None, None, None, "tab-B")

    # SW just reconnected; tabs have not re-registered yet -> empty, NOT complete.
    reaped = broker.reconcile_browser_roster(sw, [], roster_complete=False)
    assert reaped == 0
    assert broker.endpoint_by_id("ep-1") is not None
    assert broker.endpoint_by_id("ep-2") is not None


def test_empty_authoritative_roster_reaps_all_on_that_connection():
    """An authoritative empty roster means 'no tabs here' -> reap all its endpoints."""
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-1", None, None, None, "tab-A")

    reaped = broker.reconcile_browser_roster(sw, [], roster_complete=True)
    assert reaped == 1
    assert broker.all_endpoints() == []


def test_roster_scoped_to_reporting_connection_only():
    """A roster from one browser NEVER reaps endpoints on ANOTHER browser.

    Chrome (profile 1) and Firefox (profile 2) are distinct connections. An
    authoritative Chrome roster that omits the Firefox tab must NOT touch it.
    """
    broker = _new_broker()
    chrome_sw = FakeWS(OPEN)
    broker.register_browser_connection(chrome_sw, "chrome-profile")
    broker.register_endpoint(chrome_sw, "ep-chrome", None, None, None, "c-tab")

    firefox_sw = FakeWS(OPEN)
    broker.register_browser_connection(firefox_sw, "firefox-profile")
    broker.register_endpoint(firefox_sw, "ep-firefox", None, None, None, "f-tab")

    # Chrome's authoritative roster lists only its own endpoint.
    reaped = broker.reconcile_browser_roster(
        chrome_sw, ["ep-chrome"], roster_complete=True
    )
    assert reaped == 0  # nothing to reap on chrome's own connection
    # Firefox endpoint untouched.
    assert broker.endpoint_by_id("ep-firefox") is not None
    assert broker.endpoint_by_id("ep-chrome") is not None


def test_unknown_roster_ids_are_ignored_not_synthesized():
    """A roster id with no registered endpoint yet is ignored (wait for register)."""
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-known", None, None, None, "tab-A")

    # Roster mentions an id we haven't registered; it must not be created, and
    # the known endpoint (present in roster) must survive.
    reaped = broker.reconcile_browser_roster(
        sw, ["ep-known", "ep-not-yet-registered"], roster_complete=True
    )
    assert reaped == 0
    assert broker.endpoint_by_id("ep-known") is not None
    assert broker.endpoint_by_id("ep-not-yet-registered") is None


def test_roster_reap_clears_stale_default():
    """If the reaped ghost was the default GLIMPSE tab, the default self-clears."""
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-default", None, None, None, "tab-A")
    broker.register_endpoint(sw, "ep-other", None, None, None, "tab-B")
    _status, _ep = broker.set_default_endpoint("ep-default")
    assert _status == "set"

    broker.reconcile_browser_roster(sw, ["ep-other"], roster_complete=True)
    assert broker.endpoint_by_id("ep-default") is None
    assert broker.default_endpoint_id is None


def test_reconcile_on_unknown_connection_is_noop():
    """A roster ping from a socket with no registered connection is a safe no-op."""
    broker = _new_broker()
    stray = FakeWS(OPEN)  # never registered as a connection
    assert broker.reconcile_browser_roster(stray, ["whatever"], roster_complete=True) == 0


def test_kill_and_reopen_tab_resolves_after_roster_reconcile():
    """End-to-end of the reported bug, at the broker level.

    One profile SW socket. Old tab registered, then killed WITHOUT its
    endpoint_unregister arriving (the ghost). New tab opened + registered.
    Before reconcile: 2 endpoints -> ambiguous. After the SW's next
    authoritative roster ping (only the new tab live): ghost reaped -> the new
    tab auto-selects with no target and no default.
    """
    broker = _new_broker()
    sw = FakeWS(OPEN)
    broker.register_browser_connection(sw, "chrome-profile-1")
    broker.register_endpoint(sw, "ep-old", None, None, None, "tab-old")

    # Ambiguity would arise if a second (new) tab registers before the ghost clears.
    broker.register_endpoint(sw, "ep-new", None, None, None, "tab-new")
    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is not None and err["code"] == "ambiguous_browser_target"

    # SW's next keepalive roster reflects reality: only the new tab is live.
    broker.reconcile_browser_roster(sw, ["ep-new"], roster_complete=True)

    ep, err = broker.resolve_target_endpoint(None, None)
    assert err is None
    assert ep is not None and ep.endpoint_id == "ep-new"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
