"""Deterministic N01 fixture; ported from probe_n01.py, seed 20261017."""

import random
import json
import hashlib

FIXTURE_SCALE = 1.2408
FIXTURE_DIGEST = "49209c14f3aab229629fe16cfc860e3d3e6c47e1c29291dd4cfdf1e61af8a0b7"
FIXTURE_BYTES = 67_080_195

# Generated ONCE by the frozen old-code reducer before production edits:
# PYTHONPATH=tests:src .venv-wsl/bin/python tests/n01_copy_fixture.py \
#   --generate-goldens /mnt/c/tmp/n01-impl-327368e9/goldens.json
# effects blob3193a1b9818f570478d187a2dd52b7911c189d9b;
# transition blob16e3e7e74d85fc312ef4e5a7f401add82ca08d05.
# Node5000 workload adopted by conductor DECISION DMMILU; never regenerate
# expectations with the implementation under test.
EMPTY_DIAGNOSTICS_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
GOLDEN_PAYLOADS = {
    ("warm_start", 200): "48370d33d7956a5a3d4da7e10127dffc5ea01c6b0adfbd005d801b9456370b17",
    ("warm_start", 500): "ddf590cad6086c566655ea2df8dd0eabc8db10fbb981aa785482b422a8d8ded3",
    ("full_refresh", 200): "48370d33d7956a5a3d4da7e10127dffc5ea01c6b0adfbd005d801b9456370b17",
    ("full_refresh", 500): "ddf590cad6086c566655ea2df8dd0eabc8db10fbb981aa785482b422a8d8ded3",
    ("compat", 200): "eb7c8566c7e58956c134a5b52526415e38eff956498af55b587d0fd646015bce",
    ("compat", 500): "4665cb87e66e3e91d40a57129e8bda9ba1af388109d36e49bd2fa1d150c22797",
    ("checkpoint", 200): "32d9c4a2f9237e24653043baac444c04f148a1038a8330bbe5b63a50f94cc470",
    ("checkpoint", 500): "77c3ffaff52fd999b2824cda55114587e4469b4ae4ab6ed459b2224ed5762410",
}

N_TOTAL = 38_735
N_SECTIONS = 40
FLAT_PARENT = 41
FLAT_CHILDREN = 111
FIRST_SPARSE = 42 + FLAT_CHILDREN  # 153
SEED = 20_261_017
MIB = 1024 * 1024
TARGET_BYTES = 64 * MIB
TARGET_INDEX = 5000  # node the one NODE_FIELDS transaction targets

NAME_WORDS = (
    "sprint", "review", "draft", "plan", "audit", "notes", "ideas", "weekly",
    "sync", "deploy", "meeting", "follow-up", "spec", "outline", "budget",
    "travel", "reading", "checklist", "archive", "inbox", "retro", "roadmap",
    "journal", "recipes", "garden", "repairs", "invoice", "quotes", "links",
    "drafts",
)
PROSE_WORDS = (
    "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and",
    "then", "keeps", "walking", "through", "notes", "with", "a", "small",
    "list", "of", "things", "to", "do", "before", "the", "next", "review",
    "meeting", "where", "we", "decide", "what", "lands", "in", "release",
    "for", "this", "week", "while", "tracking", "open", "questions", "and",
    "loose", "ends", "that", "need", "a", "follow-up", "later", "today",
    "tomorrow", "sometime", "soon", "maybe", "next", "month", "after",
    "the", "handoff", "because", "context", "matters", "more", "than",
    "speed", "when", "the", "work", "is", "subtle", "and", "long", "running",
)
SPARSE_SIZES = (1, 2, 3, 1, 2, 2, 3, 1, 4, 2, 1, 3)


def nid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-{i:012x}"


def _ts(i: int) -> int:
    return 1_600_000_000_000 + i * 3_721


def _text_blob(rng: random.Random, size: int) -> str:
    parts: list[str] = []
    total = 0
    line: list[str] = []
    line_len = 0
    limit = 60
    while total < size:
        w = PROSE_WORDS[rng.randrange(len(PROSE_WORDS))]
        line.append(w)
        line_len += len(w) + 1
        if line_len >= limit:
            row = " ".join(line)
            parts.append(row)
            total += len(row) + 1
            line = []
            line_len = 0
            limit = 55 + (total % 97) % 55
    return "\n".join(parts)


def _make_name(i: int, rng: random.Random) -> str:
    base = f"Item {i:05d}"
    r = rng.random()
    if r < 0.35:
        return base
    w1 = NAME_WORDS[rng.randrange(len(NAME_WORDS))]
    if r < 0.75:
        return f"{base} \u2014 {w1}"
    w2 = NAME_WORDS[rng.randrange(len(NAME_WORDS))]
    if r < 0.95:
        return f"{base} \u2014 {w1} {w2} note"
    if r < 0.98:
        return f'"{base}" \u2014 {w1} & {w2} <draft>'
    return f"{base} \u2014 {w1} \U0001f4cc"


def _make_note(i: int, rng: random.Random, blob: str, scale: float) -> str:
    r = rng.random()
    if r < 0.14:
        return ""
    if r < 0.44:
        t = 40 + rng.randrange(160)
    elif r < 0.72:
        t = 200 + rng.randrange(500)
    elif r < 0.90:
        t = 700 + rng.randrange(1800)
    elif r < 0.98:
        t = 2500 + rng.randrange(5500)
    else:
        t = 8000 + rng.randrange(16000)
    t = int(t * scale)
    if t <= 8:
        return ""
    off = rng.randrange(0, max(1, len(blob) - t - 2))
    s = blob[off : off + t]
    cut = s.rfind(" ")
    if cut > t // 3:
        s = s[:cut]
    return s


def _make_data(i: int, rng: random.Random) -> dict:
    r = rng.random()
    if r < 0.45:
        return {}
    if r < 0.75:
        return {"layoutMode": "bullets"}
    if r < 0.92:
        return {
            "layoutMode": "todo",
            "extras": {"flag": i % 7, "tags": ["alpha", "beta"]},
        }
    return {
        "layoutMode": "bullets",
        "unknown": {
            "nested": {"list": [1, 2, {"deep": ["x", None, True]}], "n": i},
            "map": {"k1": "v1", "k2": [{"m": i % 13}]},
        },
    }


def build_fixture(scale: float = FIXTURE_SCALE) -> tuple[dict, dict]:
    """Deterministic synthetic nodes-export payload + structure stats."""
    rng = random.Random(SEED)
    blob = _text_blob(rng, 3_000_000)
    nodes: list[dict] = []
    stats = {
        "sections": 0,
        "flat_children": 0,
        "sparse_groups": 0,
        "sparse_hist": {},
        "completed": 0,
        "note_bytes": 0,
        "name_bytes": 0,
        "data_bytes": 0,
    }

    def mk(i: int, parent_id: str | None, priority: int) -> dict:
        t = _ts(i)
        name = _make_name(i, rng)
        note = _make_note(i, rng, blob, scale)
        data = _make_data(i, rng)
        node: dict = {
            "id": nid(i),
            "parent_id": parent_id,
            "priority": priority,
            "name": name,
            "note": note,
            "data": data,
            "createdAt": t,
            "modifiedAt": t + 900_000,
        }
        if i > 0 and i % 23 == 0:
            node["completedAt"] = t + 123_456
            node["completed"] = True
            stats["completed"] += 1
        if i > 0 and i % 41 == 0:
            node["futureField"] = {"cacheOnly": [i, {"z": None}]}
        stats["note_bytes"] += len(note)
        stats["name_bytes"] += len(name)
        stats["data_bytes"] += len(json.dumps(data))
        nodes.append(node)
        return node

    root = mk(0, None, 100)
    root["name"] = "Root"
    for s in range(1, N_SECTIONS + 1):
        mk(s, nid(0), 100 * s)
    stats["sections"] = N_SECTIONS
    mk(FLAT_PARENT, nid(1), 100)
    for k in range(FLAT_CHILDREN):
        mk(42 + k, nid(FLAT_PARENT), 100 * (k + 1))
    stats["flat_children"] = FLAT_CHILDREN
    i = FIRST_SPARSE
    j = 0
    sec_children = {s: 1 for s in range(1, N_SECTIONS + 1)}
    while i < N_TOTAL:
        k = SPARSE_SIZES[j % len(SPARSE_SIZES)]
        sec = 1 + (j % N_SECTIONS)
        sec_children[sec] += 1
        pidx = i
        mk(pidx, nid(sec), 100 * sec_children[sec])
        i += 1
        actual = 0
        for c in range(k):
            if i >= N_TOTAL:
                break
            mk(i, nid(pidx), 100 * (c + 1))
            i += 1
            actual += 1
        stats["sparse_groups"] += 1
        stats["sparse_hist"][actual] = stats["sparse_hist"].get(actual, 0) + 1
        j += 1
    assert len(nodes) == N_TOTAL, len(nodes)
    return {"nodes": nodes}, stats


def canonical_bytes(value) -> bytes:
    """The contract's comparison representation, including absent/null/order."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def fixture_identity(payload) -> tuple[str, int]:
    """Probe serialization is intentionally distinct from comparison JSON."""
    raw = json.dumps(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def effect_tail(count: int = 500, *, reference: bool = False):
    """Five-record pattern from §9.3; no state-home, journal or network IO."""
    if reference:
        import n01_reference as effect_module
    else:
        from workflowy_mcp.coherence import effects as effect_module
    effect = effect_module.SemanticEffect
    children = [nid(42 + i) for i in range(FLAT_CHILDREN)]
    result = []
    for offset in range(count):
        cycle, step = divmod(offset, 5)
        fresh = nid(N_TOTAL + cycle)
        if step == 0:
            effects = [effect.node_fields(nid(5000), ["name", "note"],
                       {"name": f"N01 field {cycle} — 📌", "note": f"N01 note {cycle}"})]
        elif step == 1:
            effects = [effect.node_parentage(nid(154), nid(155 if cycle % 2 == 0 else 153))]
        elif step == 2:
            rotation = (cycle + 1) % FLAT_CHILDREN
            ordered = children[rotation:] + children[:rotation]
            effects = [effect.child_order(nid(FLAT_PARENT), ordered)]
        elif step == 3:
            effects = [effect.node_create({"id": fresh, "parent_id": nid(153),
                       "name": f"N01 create {cycle}", "data": {"nested": [None, {"x": "y"}]}})]
        else:
            effects = [effect.tombstone([fresh])]
        result.append(effects)
    return result


def generate_goldens(output_path: str) -> None:
    """Run only the frozen reducer, once, outside all measured windows."""
    from pathlib import Path
    import n01_reference as reference
    from workflowy_mcp.coherence.canonical_node import (
        IngestionProfile, canonicalize_nodes_export,
    )
    payload, _ = build_fixture()
    identity = fixture_identity(payload)
    assert identity == (FIXTURE_DIGEST, FIXTURE_BYTES), identity
    result = {"fixture_digest": identity[0], "fixture_bytes": identity[1],
              "seed": SEED, "effects_blob": reference.EFFECTS_BLOB,
              "transition_blob": reference.TRANSITION_EFFECTS_BLOB, "goldens": {}}
    mode = reference.ApplicationMode.REPLAY_AUTHORITATIVE
    tail = effect_tail(505, reference=True)
    for route in ("warm_start", "full_refresh"):
        work = (canonicalize_nodes_export(payload, profile=IngestionProfile.FULL_EXPORT_REPLACE)
                if route == "full_refresh" else payload)
        diagnostics = []
        for offset, effects in enumerate(tail[:500], 1):
            work = reference.apply_semantic_effects(work, effects, mode=mode,
                                                    diagnostics=diagnostics)
            if offset in (200, 500):
                key = f"{route}:{offset}"
                result["goldens"][key] = {"payload": digest(work),
                                          "diagnostics": digest(diagnostics)}
                print(key, result["goldens"][key], flush=True)
                if route == "warm_start":
                    patched = reference.apply_semantic_effects(
                        work, [reference.SemanticEffect.node_fields(
                            nid(5000), ["note"], {"note": "N01 final narrow patch"})],
                        mode=reference.ApplicationMode.PUBLICATION_STRICT)
                    result["goldens"][f"compat:{offset}"] = {
                        "payload": digest(patched), "diagnostics": digest(diagnostics)}
                    caught = work
                    catch_diagnostics = list(diagnostics)
                    for extra in tail[offset:offset + 5]:
                        caught = reference.apply_semantic_effects(
                            caught, extra, mode=mode, diagnostics=catch_diagnostics)
                    result["goldens"][f"checkpoint:{offset}"] = {
                        "payload": digest(caught), "diagnostics": digest(catch_diagnostics)}
    Path(output_path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate-goldens", required=True)
    args = parser.parse_args()
    generate_goldens(args.generate_goldens)
