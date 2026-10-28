"""U04 Area 6 frozen evidence, deliberately changed policy and positive twins.

Frozen inputs/outcomes are data, never executable live beacon examples. The
original pairs all have distinct ids or unsafe/non-AST operands: none may be
merged. Same-id twins below separately prove that coalescing still works.
"""
import base64
import hashlib
import json
from pathlib import Path

import pytest

import nexus_map_codebase as nmc
from beacon_source import read_source_snapshot
from test_beacon_hygiene_shared import block, control, declaration, inputs, rows_bytes, assert_outside_edits


ROOT = Path(__file__).resolve().parents[1]
AREA6 = ROOT / "tests/beacon_robustness_corpus/area6"
CASES = json.loads((AREA6 / "area6_inputs.json").read_text(encoding="utf-8"))["fixture_module"]["cases"]
OUTCOMES = {r["case"]: r for r in json.loads((AREA6 / "area6_outcomes.json").read_text(encoding="utf-8"))["matrix_full"]}
OTHER = json.loads((AREA6 / "area6_inputs.json").read_text(encoding="utf-8"))["other_inputs"]

# Exact expected local outcomes, not a permissive assertion accepting the old
# destructive merge. None means the shared grammar/attachment excludes it.
REASONS = {
    "py_01": "distinct_ids", "py_02": "distinct_ids", "py_03": "distinct_ids",
    "py_04": None, "py_05": None,
    "py_06": "incomplete_or_uncertain_member", "py_07": "incomplete_or_uncertain_member",
    "py_08": "incomplete_or_uncertain_member", "py_09": "incomplete_or_uncertain_member",
    "py_10": "distinct_ids", "py_11": None, "py_12": None,
    "py_13": "incomplete_or_uncertain_member", "py_14": "distinct_ids", "py_15": "distinct_ids",
    "py_16": "distinct_ids", "py_17": "distinct_ids", "py_18": "distinct_ids", "py_19": "distinct_ids",
    "py_20": "distinct_ids", "py_21": "distinct_ids", "py_22": "distinct_ids", "py_23": "distinct_ids",
    "js_01": "distinct_ids", "js_02": "incomplete_or_uncertain_member", "js_03": "distinct_ids",
    "js_04": "distinct_ids", "js_05": None, "js_06": None,
    "js_07": "incomplete_or_uncertain_member", "js_08": "incomplete_or_uncertain_member",
    "ts_09": "distinct_ids", "ts_24": None, "vue_10": "distinct_ids", "vue_11": None,
    "md_01": "distinct_ids", "md_02": "distinct_ids", "md_03": None,
    "md_04": "incomplete_or_uncertain_member", "md_05": "distinct_ids",
    "md_06": "incomplete_or_uncertain_member", "md_07": None,
    "md_08": "incomplete_or_uncertain_member", "md_09": "distinct_ids",
}


def language(case):
    return {"py": "python", "md": "markdown", "vue": "vue"}.get(case["ext"], "js_ts")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_frozen_area6_bytes_and_local_choices(tmp_path, case):
    """ab0653d merges different ids or returns a whole-file proof refusal."""
    raw = case["text"].encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == case["sha256"]
    path = tmp_path / (case["name"] + "." + case["ext"])
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and not result["changed"], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    reason = REASONS["_".join(case["name"].split("_")[:2])]
    assert [g["reason"] for g in result["skipped_groups"]] == ([reason] if reason else [])
    for group in result["skipped_groups"]:
        assert group["candidates"] == group["members"]
        assert all("id" in m and "labels" in m and "comment" in m and "anchor" in m for m in group["members"])
        assert not group["edited_ranges"]
    _assert_same_file_safe_group(path, case)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_every_frozen_case_allows_same_file_safe_group(tmp_path, case):
    """One corrupt/ambiguous group must not poison the file's optional merge."""
    _assert_same_file_safe_group(tmp_path / ("neighbour_" + case["name"] + "." + case["ext"]), case)


def _assert_same_file_safe_group(path, case):
    lang = language(case)
    safe_before, safe_after = control(lang)
    raw = case["text"].encode("utf-8")
    if lang == "vue":
        assert b"</script>" in raw
        before, after = raw.replace(b"</script>", safe_before + b"</script>", 1), raw.replace(b"</script>", safe_after + b"</script>", 1)
    else:
        before, after = raw + b"\n" + safe_before, raw + b"\n" + safe_after
    path.write_bytes(before)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == after
    assert result["events"][0]["retained_ids"] == ["control"]
    assert result["events"][0]["dropped_ids"] == ["control"]
    assert_outside_edits(before, after, result)


POSITIVE_TWINS = {"py_01", "py_02", "py_03", "py_14", "py_15", "py_16", "py_17", "py_18", "py_19",
                  "py_20", "py_21", "py_23", "js_01", "js_03", "js_04", "ts_09", "vue_10", "md_01", "md_02", "md_05", "md_09"}


@pytest.mark.parametrize("case", [c for c in CASES if "_".join(c["name"].split("_")[:2]) in POSITIVE_TWINS], ids=lambda c: c["name"])
def test_same_id_twins_merge_at_shared_anchor_and_remain_writable(tmp_path, case):
    """ab0653d either loses fields/attachment/form or has no structured events."""
    lang = language(case)
    raw = case["text"].encode("utf-8")
    snapshot = nmc.source_snapshot_from_bytes(raw)
    structures, index = inputs(snapshot, lang)
    groups = {}
    for b in index["blocks"]:
        if b["anchor"] and b["record"]["fields"].get("kind") == "ast":
            groups.setdefault(b["anchor"]["line"], []).append(b)
    group = next(g for g in groups.values() if len(g) > 1)
    first_id = group[0]["record"]["fields"]["id"]
    for b in group[1:]:
        raw = raw.replace(("id=" + b["record"]["fields"]["id"] + ",").encode(), ("id=" + first_id + ",").encode())
    if case["name"].startswith("py_14"):
        # Both members explicitly agree; the original distinct-id/show_span
        # conflict stays unchanged in the frozen matrix above.
        raw = raw.replace(b"#   comment=Second.,", b"#   show_span=true,\n#   comment=Second.,")
    path = tmp_path / ("same_" + case["name"] + "." + case["ext"])
    path.write_bytes(raw)
    snapshot = read_source_snapshot(path)
    structures, index = inputs(snapshot, lang)
    group = [b for b in index["blocks"] if b["record"]["fields"].get("id") == first_id]
    expected_labels = nmc._canonicalize_slice_labels_union([b["record"]["fields"].get("slice_labels") or "" for b in group])
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["changed"] and result["merged_groups"] == 1, result
    assert result["events"][0]["survivor"]["line"] == group[-1]["record"]["line"]
    assert_outside_edits(raw, path.read_bytes(), result)
    final = read_source_snapshot(path)
    structures, index = inputs(final, lang)
    remaining = [b for b in index["blocks"] if b["record"]["fields"].get("id") == first_id]
    assert len(remaining) == 1 and remaining[0]["anchor"]
    assert remaining[0]["record"]["fields"]["slice_labels"] == expected_labels
    lines, beacons = nmc._parse_snapshot_beacons_for_language(final, lang)
    assert len(nmc._find_anchor_matched_beacons(lang, lines, beacons, remaining[0]["anchor"]["line"] - 1)) == 1
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert not nmc.normalize_duplicate_ast_beacons_in_file(str(path))["changed"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# Transcribed from Area 6 merge_probe.py (three cases) and linemodel_probe.py.
# No final test imports scratch, git or Workflowy.
EXTRAS = {
    "mp_py_F2_labels_last": ("py", ["# @beacon[", "#   id=lost@0,", "#   kind=ast,", "#   slice_labels=lab-lost,",
        "PRECIOUS = {", '    "k": 1,', "}", "VALUES = [", "    2,", "]", "# @beacon[", "#   id=good@g,",
        "#   slice_labels=lab-good,", "#   kind=ast,", "# ]", "def g():", "    return 1"]),
    "mp_js_F2_inline_comment": ("js", ["// @beacon[", "//   id=lost@0,", "//   kind=ast,", "//   comment=Lost.,",
        "const P = [ /* keep */", "  1,", "];", "// @beacon[", "//   id=good@g,", "//   kind=ast,",
        "//   comment=Good.,", "// ]", "function g() {}"]),
    "mp_py_unknown_key": ("py", ["# @beacon[", "#   id=a@f,", "#   slice_labels=lab-a,", "#   owner=dan,", "#   kind=ast,",
        "# ]", "# @beacon[", "#   id=b@f,", "#   slice_labels=lab-b,", "#   kind=ast,", "# ]", "def f():", "    return 1"]),
    "linemodel_probe": ("py", ["X = 'page\fbreak'   # a form feed inside a string literal", "# @beacon[", "#   id=a@f,",
        "#   slice_labels=lab-a,", "#   kind=ast,", "# ]", "# @beacon[", "#   id=b@f,", "#   slice_labels=lab-b,",
        "#   kind=ast,", "# ]", "def f():", "    return X"]),
}


@pytest.mark.parametrize("name", list(EXTRAS))
def test_extra_probes_unchanged_with_local_choice_and_safe_group(tmp_path, name):
    ext, rows = EXTRAS[name]
    lang = "python" if ext == "py" else "js_ts"
    bad = ("\n".join(rows) + "\n").encode()
    safe_before, safe_after = control(lang)
    path = tmp_path / (name + "." + ext)
    path.write_bytes(bad + safe_before)
    result = nmc.normalize_duplicate_ast_beacons_in_file(str(path))
    assert result["success"] and result["merged_groups"] == 1, result
    assert path.read_bytes() == bad + safe_after
    assert result["skipped_groups"]


def test_nexus_frozen_corpus_still_plans_zero_duplicate_edits():
    """No live repository cleanup; the frozen source inventory is the corpus."""
    corpus = ROOT / "tests/beacon_robustness_corpus/nexus_base/sources"
    paths = [p for p in corpus.rglob("*.txt") if any(str(p).endswith(ext + ".txt") for ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".md", ".markdown", ".vue"))]
    assert paths
    for path in paths:
        suffix = path.name[:-4].rsplit(".", 1)[-1]
        lang = {"py": "python", "md": "markdown", "markdown": "markdown", "vue": "vue"}.get(suffix, "js_ts")
        snapshot = nmc.source_snapshot_from_bytes(path.read_bytes(), str(path)[:-4])
        structures, index = inputs(snapshot, lang)
        plan = nmc.plan_beacon_hygiene(snapshot, structures, index)
        assert not plan["edits"], (str(path), plan["applied_groups"])


ALIAS_RENAMED = {"A01", "A05", "A06", "A07", "A11", "A12"}
ALIAS_SKIPPED = {"A02": "no_recognized_alias", "A03": "incomplete_record", "A04": "no_recognized_alias",
                 "A08": "incomplete_record", "A09": "canonical_and_alias", "A10": "no_recognized_alias"}


def alias_language(name):
    return {"py": "python", "md": "markdown", "vue": "vue", "sql": "sql", "sh": "shell"}.get(name.rsplit(".", 1)[-1], "js_ts")


@pytest.mark.parametrize("name,text", list(OTHER["alias_cases"].items()), ids=list(OTHER["alias_cases"]))
def test_frozen_alias_bytes_and_outcomes(tmp_path, name, text):
    """ab0653d has unsafe selection, misses A12 and lacks occurrence/skip data."""
    key = name[:3]
    raw = text.encode("utf-8")
    path = tmp_path / name
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert result["success"] and result["changed"] == (key in ALIAS_RENAMED), result
    if key in ALIAS_RENAMED:
        assert path.read_bytes() == raw.replace(b"comments=", b"comment=")
        assert len(result["events"]) == 1 and not result["skipped_aliases"]
        event = result["events"][0]
        assert event["status"] == "committed" and event["id"]
        assert event["occurrence_range"][0] + len("comment") == event["edited_ranges"][0]["start_byte"]
        assert event["retained_ids"] == [event["id"]] and event["dropped_ids"] == []
        assert event["before_sha256"] == hashlib.sha256(raw).hexdigest()
        assert event["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert not result["events"] and not result["edited_ranges"]
        assert [r["reason"] for r in result["skipped_aliases"]] == [ALIAS_SKIPPED[key]]
        assert all("candidates" in r for r in result["skipped_aliases"])
        if ALIAS_SKIPPED[key] == "no_recognized_alias":
            assert not result["findings"]  # excluded literals are not invented integrity defects
        else:
            assert len(result["findings"]) == 1
    assert path.read_bytes().count(b"\r") == raw.count(b"\r")
    assert_outside_edits(raw, path.read_bytes(), result)
    json.dumps(result)


@pytest.mark.parametrize("name,text", list(OTHER["alias_cases"].items()), ids=list(OTHER["alias_cases"]))
def test_every_frozen_alias_allows_same_file_safe_alias(tmp_path, name, text):
    """A refused/excluded record never suppresses a separate owned alias."""
    lang = alias_language(name)
    metadata_lang = "markdown" if lang == "vue" else lang
    safe_rows = [r.replace("comment=", "comments=") for r in block(metadata_lang, "safe", comment="Safe.")]
    if lang == "sql":
        safe_rows = [r.replace("#", "--", 1) for r in safe_rows]
    safe = rows_bytes(safe_rows + (["<div/>"] if lang == "vue" else declaration(lang, "safe")))
    safe_after = safe.replace(b"comments=", b"comment=")
    raw = text.encode("utf-8")
    expected = raw.replace(b"comments=", b"comment=") if name[:3] in ALIAS_RENAMED else raw
    if lang == "vue":
        before, after = raw.replace(b"</template>", safe + b"</template>"), expected.replace(b"</template>", safe_after + b"</template>")
    else:
        before, after = raw + b"\n" + safe, expected + b"\n" + safe_after
    path = tmp_path / name
    path.write_bytes(before)
    result = nmc.normalize_beacon_comment_field_aliases_in_file(str(path))
    assert result["success"] and result["changed"], result
    assert path.read_bytes() == after
    assert [e["id"] for e in result["events"]].count("safe") == 1
    assert len(result["events"]) == 1 + (name[:3] in ALIAS_RENAMED)
    assert_outside_edits(before, after, result)


def c0_raw(name):
    encoded = OTHER["c0_case_bytes_base64"].get(name)
    return base64.b64decode(encoded) if encoded else OTHER["c0_cases"][name].encode("utf-8")


@pytest.mark.parametrize("name", [n for n in OTHER["c0_cases"] if not n.startswith("C06")])
def test_frozen_c0_bytes_and_outcomes(tmp_path, name):
    """ab0653d throws C03 out of refresh and has no committed/finding rows."""
    raw = c0_raw(name)
    path = tmp_path / name
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    logs = []
    result = nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed", log_callback=logs.append)
    if name.startswith("C03"):
        assert not result["success"] and not result["changed"] and result["error_kind"] == "not_utf8"
        assert result["counts"] == {} and not result["events"]
        assert result["findings"][0]["reason"] == "not_utf8" and result["findings"][0]["candidates"] == []
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    else:
        expected = raw.replace(b"\x07", b"\\a").replace(b"\x1b", b"").replace(b"\x0b", b"\\v").replace(b"\x00", b"")
        assert result["success"] and result["changed"] and path.read_bytes() == expected
        assert len(result["events"]) == 1 and not result["findings"]
        event = result["events"][0]
        assert event["operation"] == "repair_markdown_c0" and event["status"] == "committed"
        assert event["counts"] == result["counts"] and event["target"] == "document"
        assert event["before_sha256"] == hashlib.sha256(raw).hexdigest()
        assert event["after_sha256"] == hashlib.sha256(expected).hexdigest()
        assert expected.count(b"\r") == raw.count(b"\r")
        assert expected.startswith(b"\xef\xbb\xbf") == raw.startswith(b"\xef\xbb\xbf")
        assert_outside_edits(raw, expected, result)
    assert logs and logs[0].startswith("Claimed Markdown C0 repair")
    # Decode failure owns no writable part of that document. A separate file's
    # whole-document repair remains independent, not vetoed by the bad file.
    neighbour = tmp_path / "safe.md"
    neighbour.write_bytes(c0_raw("C01_bel_prose.md"))
    assert nmc.repair_markdown_c0_controls_on_disk(str(neighbour), claim_id="claimed")["changed"]
    json.dumps(result)


def test_frozen_c0_claim_absence_control(tmp_path):
    """Control: C06's existing ValueError authority gate must not change."""
    path = tmp_path / "C06_noclaim.md"
    path.write_bytes(c0_raw(path.name))
    before = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(ValueError):
        nmc.repair_markdown_c0_controls_on_disk(str(path), **OTHER["c0_case_options"][path.name])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
