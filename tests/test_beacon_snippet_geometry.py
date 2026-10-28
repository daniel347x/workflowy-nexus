"""U07 own-block geometry witnesses; fixtures are quoted physical line lists.

The span endpoint/target/outcome matrix is completed by the next U07 leg.
These tests exercise the production context adapters, not a second parser.
"""
import pytest
import hashlib
import json
from functools import wraps
from pathlib import Path

import beacon_obtain_code_snippet as bocs
from beacon_source import read_source_snapshot
from beacon_source import scan_beacon_structure, source_lexical_evidence


CLOSE_LAYOUTS = json.loads(Path(__file__).with_name("beacon_snippet_close_layouts.json").read_text())


CONTEXT_READERS = {
    "python": bocs._python_comment_block_span,
    "shell": bocs._sh_comment_block_span,
    "yaml": bocs._yaml_comment_block_span,
    "sql": bocs._sql_comment_block_span,
    "js_ts": bocs._comment_block_span_js,
    "markdown": bocs._markdown_comment_block_span,
}
SUFFIX = {"python": ".py", "shell": ".sh", "yaml": ".yaml", "sql": ".sql",
          "js_ts": ".js", "markdown": ".md"}

# Exactly the 36 real frozen open-only spans (not the four excluded literals).
# Each is a separately named U07 endpoint delta, never part of the 258 tops.
OPEN_ONLY_IDS = {
    "beacon_obtain_code_snippet.py": [
        "carto-js-ts@_python_resolve_ast_node_heuristic", "carto-js-ts@_js_ts_resolve_ast_node_heuristic",
        "carto-js-ts@_python_snippet_for_beacon", "carto-js-ts@_js_ts_find_ast_node_for_beacon",
        "carto-js-ts@_js_ts_snippet_for_beacon", "carto-js-ts@get_snippet_for_ast_qualname",
        "carto-js-ts@get_snippet_for_ast_qualname_js_ts", "carto-js-ts@get_snippet_data",
    ],
    "nexus_map_codebase.py": [
        "auto-beacon@TRIDENT-tridnt", "carto-js-ts@parse_markdown_beacon_blocks",
        "carto-js-ts@apply_markdown_beacons", "carto-js-ts@parse_python_beacon_blocks",
        "carto-js-ts@apply_python_beacons", "carto-js-ts@parse_file_outline",
        "carto-js-ts@parse_js_ts_outline", "carto-js-ts@parse_js_beacon_blocks",
        "carto-js-ts@parse_sh_beacon_blocks", "carto-js-ts@parse_yaml_beacon_blocks",
        "carto-js-ts@apply_sql_beacons", "carto-js-ts@apply_sh_beacons",
        "carto-js-ts@apply_yaml_beacon_context", "carto-js-ts@apply_yaml_beacons",
        "carto-js-ts@apply_js_beacons", "carto-js-ts@split_name_and_tags",
        "carto-js-ts@update_beacon_from_node_python", "carto-js-ts@update_beacon_from_node_js_ts",
        "carto-js-ts@update_beacon_from_node_markdown",
    ],
    "src/workflowy_mcp/broker.py": ["broker@module-overview"],
    "src/workflowy_mcp/broker_protocol.py": ["broker@protocol-constants"],
    "src/workflowy_mcp/client/api_client_nexus.py": ["carto-js-ts@js_ts_ast_qualname_resolution"],
    "src/workflowy_mcp/transport.py": ["package-loading@transport-alt-bootstrap"],
    "workflowy-glimpse-cache/glimpse-cache-client.js": [
        "f9-ambiguous-md-picker@glimpse-ext", "glimpse-md-dock@ws-get-current-modal-render-spec",
        "glimpse-path@__bulkF12p3Verdicts", "mdpreview@anchor-view-apply-row",
    ],
    "workflowy-glimpse-cache/marked-katex-init.js": ["mdpreview@math-katex-init"],
}


def _comment(language, text):
    if language == "markdown":
        return "<!-- " + text + " -->"
    prefix = "//" if language == "js_ts" else "--" if language == "sql" else "#"
    return prefix + (" " + text if text else "")


def _block(language, bid, *, close=False, complete=True):
    body = [("@beacon-close[" if close else "@beacon["), "id=" + bid + ","]
    if not close:
        body += ["kind=ast,", "comment=Reads [ap:WINDOW] without cutting the block,", "slice_labels=carto-fwd-source,"]
    if complete:
        body += ["]"]
    if language == "markdown":
        return ["<!--", *body, "-->"]
    return [_comment(language, row) for row in body]


@pytest.mark.parametrize("language", CONTEXT_READERS)
@pytest.mark.parametrize("neighbour", ["opening", "closing", "stray_bracket", "incomplete"])
def test_context_includes_own_block_not_neighbour(tmp_path, language, neighbour):
    """Before: below-only context cut the own opener or swallowed foreign fields."""
    foreign = (_block(language, "foreign@0", close=neighbour == "closing",
                      complete=neighbour != "incomplete")
               if neighbour != "stray_bracket" else [_comment(language, "]")])
    prose = [_comment(language, "ordinary prose"), ""]
    own = _block(language, "own@0")
    following = [_comment(language, "below context"), "real_code"]
    lines = foreign + prose + own + following
    path = tmp_path / ("source" + SUFFIX[language])
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    observed = bocs._read_lines(str(path))
    opener = len(foreign + prose) + (2 if language == "markdown" else 1)
    top, bottom = CONTEXT_READERS[language](observed, opener)
    # With no terminator/wrapper, the shared record owns the intervening
    # comment rows too. They are not reclassified as this beacon's prose.
    expected_top = (len(foreign + prose) + 1
                    if neighbour == "incomplete" and language != "markdown"
                    else len(foreign) + 1)
    assert top == expected_top
    assert bottom == len(lines) - 1
    assert top <= opener <= len(foreign + prose + own) <= bottom
    # The foreign metadata is wholly out, not just its opener.
    assert "foreign@0" not in "\n".join(observed[top - 1:bottom])
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)

    # Same-file independent positive control, after a real-code boundary.
    clean = _block(language, "clean@0")
    path.write_bytes(("\n".join(lines + clean + ["other_code"]) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    observed = bocs._read_lines(str(path))
    clean_opener = len(lines) + (2 if language == "markdown" else 1)
    a, z = CONTEXT_READERS[language](observed, clean_opener)
    assert (a, z) == (len(lines) + 1, len(lines) + len(clean))
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


@pytest.mark.parametrize("separator", ["\f", "\v", "\x85", "\u2028", "\u2029"])
def test_read_lines_matches_snapshot_physical_lines(tmp_path, separator):
    """Before: str.splitlines invented a source line before the beacon."""
    path = tmp_path / "physical.py"
    lines = ["# prefix" + separator + "not another physical line", *_block("python", "own@0"),
             "def own():", "    return 1", "", "def neighbour():", "    return 2"]
    path.write_bytes(b"\xef\xbb\xbf" + ("\r\n".join(lines) + "\r\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    observed = bocs._read_lines(str(path))
    assert observed == list(read_source_snapshot(path).physical_lines) == lines
    assert observed[1] == "# @beacon["
    assert CONTEXT_READERS["python"](observed, 2) == (1, 7)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_python_public_read_keeps_full_ap_comment_block_and_body(tmp_path):
    """Before: the window began below its opener at the ] after an AP handle."""
    path = tmp_path / "own.py"
    lines = [*_block("python", "own@0"), "# below-only context", "def own():",
             "    return 42", "", "def neighbour():", "    return 7"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = bocs.get_snippet_data(str(path), "own@0", 0)
    assert (result[3], result[4], result[5]) == (1, 9, 1)
    assert result[2][result[4] - 1] == "    return 42"
    assert "neighbour" not in "\n".join(result[2][result[3] - 1:result[4]])
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_js_url_star_sequence_does_not_supply_context(tmp_path):
    """U01's literal-aware evidence is consumed, without a bocs-side mask."""
    path = tmp_path / "url.js"
    foreign = _block("js_ts", "foreign@0")
    lines = ["const old = 'https://*.example.com/*';", "", *foreign, *_block("js_ts", "own@0"),
             "function own() {", "  return 1;", "}"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    opener = 3 + len(foreign)
    assert bocs._comment_block_span_js(bocs._read_lines(str(path)), opener) == (opener, opener + 5)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_one_snapshot_supplies_beacon_and_outline(tmp_path, monkeypatch):
    path = tmp_path / "one.py"
    lines = ["# before\fnot a source line", *_block("python", "own@0"),
             "def own():", "    return 42", "", "def neighbour():", "    return 7"]
    path.write_bytes(b"\xef\xbb\xbf" + ("\r\n".join(lines) + "\r\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    calls = []
    original = getattr(bocs, "read_source_snapshot", bocs.nexus_map_codebase.read_source_snapshot)

    def observe(path):
        calls.append(str(path))
        return original(path)

    def reread_forbidden(*args, **kwargs):
        raise AssertionError("outline reopened source instead of consuming snapshot")

    monkeypatch.setattr(bocs, "read_source_snapshot", observe, raising=False)
    monkeypatch.setattr(bocs.nexus_map_codebase, "read_source_snapshot", reread_forbidden)
    result = bocs.get_snippet_data(str(path), "own@0", 0)
    assert calls == [str(path)]
    assert result[4] == 9 and result[2][8] == "    return 42"
    assert result[6]["resolution_strategy"] == "beacon"
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_markdown_ast_id_and_md_path_share_heading_sections_with_c0(tmp_path):
    path = tmp_path / "headings.md"
    lines = ["# Pa\x07rent", "intro", "<!--", "@beacon[", "id=own@0,", "kind=ast,",
             "slice_labels=carto-fwd-source,", "]", "-->", "", "## Child", "own body", "", "### Sub", "sub body",
             "", "## Neighbour", "neighbour body"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    by_id = bocs.get_snippet_data(str(path), "own@0", 0)
    assert (by_id[3], by_id[4]) == (3, 16)
    assert "own body" in "\n".join(by_id[2][by_id[3] - 1:by_id[4]])
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    parent = bocs.nexus_map_codebase._strip_disallowed_markdown_control_chars(lines[0])
    by_path = bocs.get_snippet_for_md_path(str(path), [parent, "## Child"], 0)
    assert (by_path[3], by_path[4]) == (11, 16)
    assert by_path[2][0] == parent
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    neighbour = bocs.get_snippet_for_md_path(str(path), [parent, "## Neighbour"], 0)
    assert (neighbour[3], neighbour[4]) == (17, 18)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_md_path_duplicate_occurrence_rules_and_clean_neighbour(tmp_path):
    path = tmp_path / "duplicate.md"
    lines = ["# Parent", "", "## Same", "first", "", "## Same", "second", "", "## Other", "safe"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    for occurrence in (None, 3):
        with pytest.raises(bocs.AmbiguousMdPathError) as error:
            bocs.get_snippet_for_md_path(str(path), ["# Parent", "## Same"], 0, occurrence=occurrence)
        assert [candidate["line"] for candidate in error.value.candidates] == [3, 6]
        assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    second = bocs.get_snippet_for_md_path(str(path), ["# Parent", "## Same"], 0, occurrence=2)
    assert (second[3], second[4]) == (6, 8)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    neighbour = bocs.get_snippet_for_md_path(str(path), ["# Parent", "## Other"], 0)
    assert (neighbour[3], neighbour[4]) == (9, 10)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_legacy_explicit_text_range_is_not_replaced_by_eof(tmp_path):
    path = tmp_path / "explicit.py"
    lines = ["# @beacon[", "# id=legacy@0,", "# kind=span,", '# start_snippet="START = 1",',
             '# end_snippet="STOP = 2",', "# ]", "unrelated = 0", "START = 1", "inside = 42",
             "STOP = 2", "not_in_target = 3"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = bocs.get_snippet_data(str(path), "legacy@0", 0)
    assert (result[3], result[4]) == (1, 10)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_yaml_span_text_id_is_not_an_opener_id(tmp_path):
    path = tmp_path / "config.yaml"
    lines = ["# @beacon[", "# id=cfg@0,", "# kind=span,", "# comment=Config [ap:WHOLE],", "# ]",
             "user:", "  id: admin", "# @beacon-close[", "# id=cfg@0,", "# ]", "other: safe"]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = bocs.get_snippet_data(str(path), "cfg@0", 0)
    assert (result[3], result[4], result[5]) == (1, 7, 1)
    assert result[6]["location"]["span"]["close"]["line"] == 8
    assert "id: admin" in result[2][6]
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


@pytest.mark.parametrize("case", CLOSE_LAYOUTS["cases"], ids=lambda case: case["case"])
def test_all_154_close_layouts_use_u01_geometry_and_keep_safe_reads(tmp_path, case):
    """22 quoted layouts × seven languages: pairing, exact id, whole close out."""
    raw = ("\n".join(case["lines"]) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == case["sha256"]
    path = tmp_path / (case["case"] + case["suffix"])
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    snapshot = read_source_snapshot(path)
    language = {"js": "js_ts", "sh": "shell"}.get(case["language"], case["language"])
    if language == "vue":
        records = [record for _section, rows in bocs.nexus_map_codebase._vue_section_records(snapshot) for record in rows]
    else:
        records = scan_beacon_structure(snapshot, language, evidence=source_lexical_evidence(snapshot, language))
    for target in case["targets"]:
        own = [record for record in records if record["marker"] == "open" and record["fields"].get("id") == target]
        assert len(own) == 1
        own = own[0]
        result = bocs.get_snippet_data(str(path), target, 0)
        assert before == (path.read_bytes(), path.stat().st_mtime_ns)
        assert result[3] <= own["line"] <= own["read_end_line"] <= result[4]
        span = result[6]["location"]["span"]
        assert span["pair_state"] == own["pair_state"]
        assert span["closed"] == own["closed"]
        assert span["inner_lines"] == own["inner_lines"]
        assert span["inner_range"] == own["inner_range"]
        assert span["section"] == own["section"]
        assert result[5] == own["line"]
        if own["closed"]:
            close = next(record for record in records if record["marker"] == "close"
                         and record["line"] == own["close_line"] and record["section"] == own["section"])
            first_close_line = close["line_extent"][0] + 1 if close["line_extent"] else close["line"]
            assert result[4] == first_close_line - 1 == own["inner_lines"][1]
            for key in ("line", "end_line", "read_end_line", "line_extent", "opener_range", "bracket_range",
                        "wrapper_range", "wrapper_open_range", "wrapper_close_range", "complete", "terminated_by",
                        "pair_state", "closed", "close_line", "opener_line", "section"):
                assert span["close"][key] == close.get(key), (case["case"], key)
        else:
            assert own["pair_state"] == "open_only"
            assert result[4] == own["section"]["end_line"] == own["inner_lines"][1]
            assert span["close"] is None

    # An independent positive control, not a beacon-free refusal disguised as
    # safe work. Same-language clean neighbour remains readable for every layout.
    if language == "markdown":
        clean = ["<!--", "@beacon[", "id=clean@0,", "kind=span,", "]", "-->", "safe prose",
                 "<!--", "@beacon-close[", "id=clean@0,", "]", "-->"]
    else:
        prefix = "//" if language in {"js_ts", "vue"} else "--" if language == "sql" else "#"
        body = {"python": "value = 1", "yaml": "value: safe", "shell": "echo safe",
                "sql": "SELECT 1;", "js_ts": "const value = 1;", "vue": "const value = 1;"}[language]
        clean = [prefix + " " + line for line in ["@beacon[", "id=clean@0,", "kind=span,", "]"]]
        clean += [body] + [prefix + " " + line for line in ["@beacon-close[", "id=clean@0,", "]"]]
        if language == "vue":
            clean = ["<script>", *clean, "</script>"]
    neighbour = tmp_path / ("clean" + case["suffix"])
    neighbour.write_bytes(("\n".join(clean) + "\n").encode())
    clean_before = neighbour.read_bytes(), neighbour.stat().st_mtime_ns
    safe = bocs.get_snippet_data(str(neighbour), "clean@0", 0)
    assert safe[6]["location"]["span"]["pair_state"] == "closed"
    assert safe[4] < safe[6]["location"]["span"]["close"]["line"]
    assert clean_before == (neighbour.read_bytes(), neighbour.stat().st_mtime_ns)


def test_close_layout_payload_is_complete_and_byte_exact():
    assert CLOSE_LAYOUTS["count"] == len(CLOSE_LAYOUTS["cases"]) == 154
    assert CLOSE_LAYOUTS["per_language"] == {language: 22 for language in
                                             ("js", "markdown", "python", "sh", "sql", "vue", "yaml")}
    digest = hashlib.sha256("\n".join(
        f"{case['case']}{case['suffix']}\t{case['sha256']}" for case in CLOSE_LAYOUTS["cases"]
    ).encode()).hexdigest()
    assert digest == CLOSE_LAYOUTS["corpus_sha256"]


def test_frozen_corpus_own_block_tops_and_unchanged_endpoints(tmp_path, monkeypatch):
    """All 258 named non-test repairs, 25 closed-span tops, and every other row.

    Ruling A preserves closed display endpoints and carries the close as data.
    Only the 36 specifically named open-only spans gain an EOF endpoint.
    """
    corpus = Path(__file__).with_name("beacon_robustness_corpus")
    area = corpus / "area10"
    frozen = json.loads((area / "area10_window_rows.json").read_text())
    manifest = json.loads((area / "area10_manifest.json").read_text())
    corrections = {(r["file"], r["id"]): r for r in manifest["row_map"]}
    assert len(corrections) == 261
    assert sum(not r["tests"] for r in corrections.values()) == 258
    closed = {(r["file"], r["id"]) for r in frozen["spans"] if r["W"] is not None}
    assert sum(k in closed and not r["tests"] for k, r in corrections.items()) == 25

    literal_ids = {
        "tests/test_agent_kit_build.py": [
            "demo@LIMIT", "demo@Registry.register", "demo@Store", "demolition@Store.drop",
            "demo@serve", "demo@serve.handle", "demo@fetch", "demo@limits", "demo@quiet",
            "demo@deep", "demo@build", "demo@service",
        ],
        "tests/test_carto_dry_run_byte_purity.py": ["second"],
        "tests/test_repository_view_source_fidelity.py": [
            "rvw3@doc-example", "rvw3@beacon-example", "rvw4@leading-ast", "rvw4@leading-md",
            "rvw4@leading-beacon", "rvw4@leading-md-ctx",
        ],
    }
    excluded = {(file, bid) for file, ids in literal_ids.items() for bid in ids}
    assert len(excluded) == 19
    assert {k for k, r in corrections.items() if r["tests"]} <= excluded
    mask_tops = {
        ("workflowy-glimpse-cache/background.js", "auto-beacon@clearReconnectTimer-04hv"): 383,
        ("workflowy-glimpse-cache/background.js", "auto-beacon@scheduleReconnect-ojt9"): 396,
        ("workflowy-glimpse-cache/background.js", "auto-beacon@connectWebSocket-zwgn"): 410,
    }
    open_only = {(file, bid) for file, ids in OPEN_ONLY_IDS.items() for bid in ids}
    assert len(open_only) == 36
    frozen_open_only = {(row["file"], row["id"]) for row in frozen["spans"]
                        if row["W"] is None and (row["file"], row["id"]) not in excluded}
    assert open_only == frozen_open_only
    paths, eof_lines = {}, {}
    for relative, entry in manifest["file_index"].items():
        raw = (corpus / entry["frozen_source"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        paths[relative] = path
        eof_lines[relative] = len(read_source_snapshot(path).physical_lines)

    # Cache only pure per-file parse inputs, not any selected window/target.
    # Their keys include the exact observed version and relevant input identity.
    # All 1603 API reads, selection, bounds and purity assertions still execute.
    def memo_parser(parser, make_key):
        cache = {}

        @wraps(parser)
        def cached(snapshot, *args, **kwargs):
            key = (snapshot.canonical_path, snapshot.sha256, make_key(args, kwargs))
            if key not in cache:
                cache[key] = parser(snapshot, *args, **kwargs)
            return cache[key]
        return cached

    if hasattr(bocs, "_snippet_outline"):
        monkeypatch.setattr(bocs, "_snippet_outline", memo_parser(bocs._snippet_outline, lambda a, k: a[0]))
        monkeypatch.setattr(bocs, "_snippet_structures", memo_parser(bocs._snippet_structures, lambda a, k: (a[0], id(a[1]))))
        monkeypatch.setattr(bocs.nexus_map_codebase, "beacon_attachment_index", memo_parser(
            bocs.nexus_map_codebase.beacon_attachment_index, lambda a, k: (id(a[0]), id(a[1]))))
        monkeypatch.setattr(bocs, "source_lexical_evidence", memo_parser(bocs.source_lexical_evidence, lambda a, k: a[0]))
        monkeypatch.setattr(bocs, "_beacon_comment_rows", memo_parser(
            bocs._beacon_comment_rows, lambda a, k: (a[0], a[1], a[2], id(a[3]), tuple(a[4:]))))
        if hasattr(bocs, "_snippet_analysis_rows"):
            monkeypatch.setattr(bocs, "_snippet_analysis_rows", memo_parser(
                bocs._snippet_analysis_rows, lambda a, k: (a[0], id(a[1]), json.dumps(a[2], sort_keys=True))))
    snapshots = []
    original_read = getattr(bocs, "read_source_snapshot", bocs.nexus_map_codebase.read_source_snapshot)

    def read_once(path):
        snapshots.append(str(path))
        return original_read(path)

    monkeypatch.setattr(bocs, "read_source_snapshot", read_once, raising=False)
    repaired, missing, deltas = set(), set(), set()
    for row in frozen["rows"]:
        key = row["file"], row["id"]
        path = paths[row["file"]]
        before = path.read_bytes(), path.stat().st_mtime_ns
        try:
            result = bocs.get_snippet_data(str(path), row["id"], 0)
        except RuntimeError as error:
            assert key in excluded, (key, str(error))
            assert row["id"] in str(error) and path.name in str(error)
            missing.add(key)
        else:
            assert key not in excluded, key
            top = mask_tops.get(key, corrections.get(key, {}).get("new_top", row["core"][0]))
            expected_end = eof_lines[row["file"]] if key in open_only else row["core"][1]
            assert [result[3], result[4]] == [top, expected_end], key
            assert result[5] == row["beacon_line"], key
            assert result[3] <= row["cl"] <= row["pend"] <= result[4], key
            if key in open_only:
                assert result[6]["location"]["span"]["pair_state"] == "open_only", key
            elif key in closed:
                assert result[6]["location"]["span"]["pair_state"] == "closed", key
                assert result[6]["location"]["span"]["close"]["line"] > result[4], key
            if key in corrections and not row["tests"]:
                repaired.add(key)
            if [result[3], result[4]] != row["core"]:
                deltas.add(key)
        finally:
            assert before == (path.read_bytes(), path.stat().st_mtime_ns), key
    assert len(frozen["rows"]) == 1603
    assert len(snapshots) == 1603
    assert len(repaired) == 258
    assert missing == excluded
    assert deltas == repaired | set(mask_tops) | open_only
