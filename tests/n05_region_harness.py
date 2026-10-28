"""N05 deterministic topology and isolated in-process boundary fixtures.

The literal parent model is independent of the production region predicates.
No second lock implementation, real API client, process or runtime home.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from coherence_harness import ACCOUNT_A, make_boundary_capable


def node_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"n05-region:{label}"))


# Literal expected geometry, not read back from a production cache/predicate.
PARENTS = {"vault": None, "a": "vault", "p": "a", "x": "p", "g": "vault", "q": "g"}


def graph_cache(parents: Mapping[str, str | None] = PARENTS) -> dict:
    return {
        node_uuid(label): {
            "id": node_uuid(label), "name": label,
            "parent_id": node_uuid(parent) if parent is not None else None,
        }
        for label, parent in parents.items()
    }


def model_chain(label: str, parents: Mapping[str, str | None] = PARENTS) -> tuple[str, ...]:
    chain = []
    while label is not None:
        if label in chain or label not in parents:
            raise ValueError("unknown model topology")
        chain.append(label)
        label = parents[label]
    return tuple(chain)


def region_client(tmp_path, parents: Mapping[str, str | None] = PARENTS):
    """A publisher-capable double with fake credentials and private claim scope."""
    client = SimpleNamespace(
        api_key="n05-fake-key-never-connect",
        _write_governor=SimpleNamespace(paths=SimpleNamespace(
            jobs_root=tmp_path / "n05-jobs", account_scope_id=ACCOUNT_A,
        )),
    )
    return make_boundary_capable(client, nodes=graph_cache(parents).values())


def isolate_claim_scope(client, tmp_path):
    """Give a legacy HTTP double the private observer inputs N05 now consumes."""
    client.api_key = "n05-fake-key-never-connect"
    client._write_governor = SimpleNamespace(paths=SimpleNamespace(
        jobs_root=tmp_path / "n05-jobs", account_scope_id=ACCOUNT_A,
    ))
    return client


def publish_fixture_nodes(client, nodes):
    """An HTTP double's completed create becomes visible like the real CRUD COW."""
    publisher = client._cache_view_publisher
    before = publisher.capture()
    publisher.publish(type(before).build(
        epoch_number=before.epoch_number, epoch_id=before.epoch_id,
        adopted_sequence=before.adopted_sequence,
        nodes_export={"nodes": list(nodes)}, account_scope_id=before.account_scope_id,
    ))


class LookupOnlyMap(Mapping):
    """Detect a cache-wide walk/copy; count only keyed region lookup work."""

    def __init__(self, source):
        self.source = source
        self.lookups = 0

    def __getitem__(self, key):
        self.lookups += 1
        return self.source[key]

    def __len__(self):
        return len(self.source)

    def get(self, key, default=None):
        self.lookups += 1
        return self.source.get(key, default)

    def __iter__(self):
        raise AssertionError("region admission iterated the cache")

    def keys(self):
        raise AssertionError("region admission copied the cache")

    def items(self):
        raise AssertionError("region admission iterated cache items")

    def values(self):
        raise AssertionError("region admission iterated cache values")


def large_region_tree(size: int, *, depth: int = 64):
    """Deterministic independent branches reaching depth64, then leaf filler.

    Branches0/1 are MOVE's target/former-parent/destination,2..101 holders,
    102..201 claims. None of those branches contains another. Both account
    sizes have identical hot paths; extra account nodes cannot add lookup work.
    This is fixture geometry only, not a second intersection implementation.
    """
    assert size >= 202 * depth and depth == 64
    rows, branches = [], []
    for branch in range(202):
        chain = []
        for level in range(depth):
            key = node_uuid(f"cost-branch-{branch}-level-{level}")
            rows.append({"id": key, "name": f"b{branch}:{level}",
                         "parent_id": chain[-1] if chain else None})
            chain.append(key)
        branches.append(tuple(chain))
    for index in range(size - len(rows)):
        rows.append({"id": node_uuid(f"cost-filler-{index}"), "name": "filler",
                     "parent_id": branches[0][0]})
    return rows, tuple(branches)


class AdmissionClock:
    """Test-only monotonic source; the real loop/Events remain unmodified."""

    def __init__(self, start=100.0):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AdmissionCounters:
    """Count published-reference and claim I/O observations, never own locks."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.values = dict(captures=0, stats=0, scans=0, json_opens=0,
                           mutex=0, liveness=0)

    def observe(self, name, call):
        def counted(*args, **kwargs):
            self.values[name] += 1
            return call(*args, **kwargs)
        return counted

    def forbidden(self, name):
        def fail(*args, **kwargs):
            self.values[name] += 1
            raise AssertionError(f"admission performed {name}")
        return fail


class RegionHTTPProbe:
    """HTTP-only fake with an independent effect-footprint oracle at every send.

    Real client CRUD, ETCH, cache publication, governor and server fronts run
    unchanged. The expected graph is the fake account's literal state, never
    the production cache/predicate. Violations are recorded rather than thrown
    inside a retrying client: the witness asserts them after the real reply.
    """

    def __init__(self, nodes, *, held_regions, reserved_parents):
        self.nodes = {row["id"]: {"createdAt": 100, "modifiedAt": 100,
                                  "note": "", "priority": 0, **row} for row in nodes}
        self.sends = []
        self.reads = []
        self.violations = []
        self.outer = frozenset()
        self.created = []
        self.clock = 1000
        self.before_send = None
        self.held_regions = held_regions
        self.reserved_parents = reserved_parents

    def _chain(self, key):
        seen = []
        while key not in (None, ""):
            if key in seen or key not in self.nodes:
                raise AssertionError("invalid independent HTTP fixture graph")
            seen.append(key)
            key = self.nodes[key].get("parent_id")
        return seen

    def _covered(self, held, kind, key):
        if (kind, key) in held:
            return True
        if key is None or key == "inbox" or kind == "ap_core":
            return False
        return any(other_kind == "subtree" and root in self._chain(key)
                   for other_kind, root in held)

    async def request(self, request):
        import json
        import httpx

        def parent_key(value):
            return (self.reserved_parents.ROOT if value in (None, "") else
                    self.reserved_parents.INBOX if value == "inbox" else value)

        held = frozenset((r.kind, r.key) for r in self.held_regions())
        missing_outer = self.outer - held
        if missing_outer:
            self.violations.append(("declared outer region missing", missing_outer, held))
        parts = request.url.path.rstrip("/").split("/")
        terminal = parts[-1]
        if request.method == "GET":
            self.reads.append((request.url.path, held))
            if terminal == "nodes":
                parent = request.url.params.get("parent_id", request.url.params.get("parentId"))
                children = sorted((row for row in self.nodes.values() if row.get("parent_id") == parent),
                                  key=lambda row: row.get("priority", 0))
                return httpx.Response(200, json={"nodes": children})
            return httpx.Response(200, json={"node": self.nodes[terminal]})

        body = json.loads(request.content) if request.content else {}
        required = []
        if request.method == "POST" and terminal == "nodes":
            parent = body.get("parent_id", body.get("parentId"))
            required = [("child_set", parent_key(parent))]
            new_id = node_uuid(f"created-{len(self.created)}")
            self.created.append(new_id)
            siblings = [r for r in self.nodes.values() if r.get("parent_id") == parent]
            self.nodes[new_id] = {"id": new_id, "name": body["name"], "note": body.get("note", ""),
                                  "parent_id": parent, "priority": len(siblings),
                                  "createdAt": self.clock, "modifiedAt": self.clock}
            response = {"item_id": new_id}
        elif request.method == "DELETE":
            required = [("subtree", terminal), ("child_set", parent_key(self.nodes[terminal].get("parent_id")))]
            response = {"status": "ok"}
        elif request.method == "POST" and terminal == "move":
            key = parts[-2]
            parent = body.get("parent_id", body.get("parentId"))
            required = [("exact_node", key), ("child_set", parent_key(self.nodes[key].get("parent_id"))),
                        ("child_set", parent_key(parent))]
            response = {"status": "ok"}
        elif request.method == "POST":
            key = parts[-2] if terminal in ("complete", "uncomplete") else terminal
            required = [("exact_node", key)]
            response = {"status": "ok"}
        else:
            raise AssertionError(f"unexpected fake HTTP route {request.method} {request.url.path}")
        for kind, key in required:
            if not self._covered(held, kind, key):
                self.violations.append(("send footprint uncovered", kind, key, held))
        self.sends.append((request.method, request.url.path, required, held))
        if self.before_send is not None:
            await self.before_send(request, held, required)
        self.clock += 1
        if request.method == "DELETE":
            removed = [key for key in self.nodes if terminal in self._chain(key)]
            for key in removed:
                del self.nodes[key]
        elif request.method == "POST" and terminal == "move":
            self.nodes[parts[-2]]["parent_id"] = body.get("parent_id", body.get("parentId"))
        elif request.method == "POST" and terminal != "nodes":
            key = parts[-2] if terminal in ("complete", "uncomplete") else terminal
            row = self.nodes[key]
            if terminal in ("complete", "uncomplete"):
                row["completedAt"] = self.clock if terminal == "complete" else None
            else:
                row.update(body)
                if "layoutMode" in body:
                    row["data"] = {**row.get("data", {}), "layoutMode": body["layoutMode"]}
            row["modifiedAt"] = self.clock
        return httpx.Response(200, json=response)
