"""U02 final-hash, binary publication and ordinary failure/kill witnesses.

Before-fix distinctions: the old writer hashes before temp IO, uses text
translation, and has no shared ownership proof; the executor reopens to parse.
All mutation fixtures are disposable tmp_path files; no daemon or live repo IO.
"""
import ast
import builtins
from collections import Counter
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import beacon_edits as edits
from beacon_source import read_source_snapshot, source_snapshot_from_bytes
import nexus_map_codebase as nmc


def make_plan(path, *, source_exists=True):
    raw = b"# @beacon[\n# id=target,\n# slice_labels=old,\n# kind=ast,\n# ]\nkeep()\n"
    if source_exists:
        path.write_bytes(raw)
        snapshot = read_source_snapshot(path)
        record = nmc._beacon_edit_records(snapshot, "python")[0]
        owner = record["opener_range"][0]
        start = raw.index(b"old")
        splice = edits.BeaconEdit(start, start + 3, b"new", owner)
        return edits.BeaconEditPlan(snapshot, "python", (splice,), (owner,), (
            {"owner": owner, "operation": "present", "fields": {"slice_labels": "new"}},
        ))
    raw = raw.removesuffix(b"keep()\n")
    snapshot = source_snapshot_from_bytes(b"", str(path))
    return edits.BeaconEditPlan(snapshot, "python", (edits.BeaconEdit(0, 0, raw, "insertion"),),
                                insertion_slots=(0,), source_exists=False)


def assert_safe_neighbour(tmp_path):
    path = tmp_path / "safe.py"
    plan = make_plan(path)
    assert edits.commit_beacon_edit_plan(plan)["success"]
    assert path.read_bytes() == plan.snapshot.raw_bytes.replace(b"old", b"new")


@pytest.mark.parametrize("point", ["after_snapshot", "after_plan", "before_verification", "after_temp_write", "after_fsync", "after_copymode", "before_final_hash"])
def test_external_save_before_final_hash_survives_and_next_file_commits(tmp_path, monkeypatch, point):
    path = tmp_path / "raced.py"
    plan = make_plan(path)
    external = plan.snapshot.raw_bytes + b"editor_save()\r\n"
    state = {}

    def save():
        path.write_bytes(external)
        state["mtime"] = path.stat().st_mtime_ns

    with monkeypatch.context() as patch:
        if point in {"after_snapshot", "after_plan"}:
            save()
        elif point == "before_verification":
            original = edits.verify_beacon_edit_plan
            def verify(*args, **kwargs):
                save()
                return original(*args, **kwargs)
            patch.setattr(edits, "verify_beacon_edit_plan", verify)
        elif point in {"after_temp_write", "after_fsync"}:
            original = edits.os.fsync
            def fsync(fd):
                if point == "after_temp_write":
                    save()
                original(fd)
                if point == "after_fsync":
                    save()
            patch.setattr(edits.os, "fsync", fsync)
        elif point == "after_copymode":
            original = edits.shutil.copymode
            def copymode(*args, **kwargs):
                original(*args, **kwargs)
                save()
            patch.setattr(edits.shutil, "copymode", copymode)
        else:
            original = builtins.open
            def final_open(name, mode="r", *args, **kwargs):
                if os.fspath(name) == str(path) and mode == "rb":
                    save()
                return original(name, mode, *args, **kwargs)
            patch.setattr(builtins, "open", final_open)
        result = edits.commit_beacon_edit_plan(plan)
    assert result["error_kind"] == "source_changed_since_plan", result
    assert result["evidence"]["expected"]["sha256"] == plan.source_sha256
    assert result["evidence"]["actual"]["sha256"] == hashlib.sha256(external).hexdigest()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (external, state["mtime"])
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    assert_safe_neighbour(tmp_path)


@pytest.mark.parametrize("point", ["mkstemp", "fsync", "copymode", "final_read", "replace"])
def test_io_failure_leaves_original_and_next_file_commits(tmp_path, monkeypatch, point):
    path = tmp_path / "failed.py"
    plan = make_plan(path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    def fail(*_args, **_kwargs):
        raise PermissionError("injected IO failure")
    with monkeypatch.context() as patch:
        if point == "mkstemp":
            patch.setattr(edits.tempfile, "mkstemp", fail)
        elif point == "copymode":
            patch.setattr(edits.shutil, point, fail)
        elif point == "final_read":
            original = builtins.open
            def final_open(name, mode="r", *args, **kwargs):
                if os.fspath(name) == str(path) and mode == "rb":
                    fail()
                return original(name, mode, *args, **kwargs)
            patch.setattr(builtins, "open", final_open)
        else:
            patch.setattr(edits.os, point, fail)
        result = edits.commit_beacon_edit_plan(plan)
    assert result["error_kind"] == "disk_write_failed", result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(tmp_path.glob(".beacon_atomic_*"))
    assert_safe_neighbour(tmp_path)


def test_cleanup_failure_is_evidence_not_retry_or_rollback(tmp_path, monkeypatch):
    path = tmp_path / "failed.py"
    plan = make_plan(path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    def fail(*_args, **_kwargs):
        raise PermissionError("injected")
    with monkeypatch.context() as patch:
        patch.setattr(edits.os, "replace", fail)
        patch.setattr(edits.os, "unlink", fail)
        result = edits.commit_beacon_edit_plan(plan)
    assert result["error_kind"] == "disk_write_failed"
    assert result["cleanup_error"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert len(list(tmp_path.glob(".beacon_atomic_*"))) == 1
    assert_safe_neighbour(tmp_path)


def test_final_hash_to_replace_is_explicit_t2_not_atomic_cas(tmp_path, monkeypatch):
    path = tmp_path / "t2.py"
    plan = make_plan(path)
    original = edits.os.replace
    def replace(source, destination):
        # Outside the guaranteed window: an uncooperative save occurs strictly
        # between the final check and the publication instruction.
        path.write_bytes(b"external after final hash\n")
        return original(source, destination)
    monkeypatch.setattr(edits.os, "replace", replace)
    result = edits.commit_beacon_edit_plan(plan)
    assert result["success"]
    assert path.read_bytes() == plan.snapshot.raw_bytes.replace(b"old", b"new")


@pytest.mark.parametrize("existing", [None, b""])
def test_create_compares_expected_absence_not_empty_hash(tmp_path, existing):
    path = tmp_path / "new.py"
    plan = make_plan(path, source_exists=False)
    if existing is not None:
        path.write_bytes(existing)
        before = path.stat().st_mtime_ns
    result = edits.commit_beacon_edit_plan(plan)
    if existing is None:
        assert result["success"], result
        assert result["before_sha256"] is None
    else:
        assert result["error_kind"] == "source_changed_since_plan"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (existing, before)
        assert_safe_neighbour(tmp_path)


def test_deleted_source_is_not_recreated(tmp_path):
    path = tmp_path / "deleted.py"
    plan = make_plan(path)
    path.unlink()
    result = edits.commit_beacon_edit_plan(plan)
    assert result["error_kind"] == "source_changed_since_plan"
    assert not path.exists()
    assert_safe_neighbour(tmp_path)


def test_binary_path_bypasses_windows_text_translation(tmp_path, monkeypatch):
    path = tmp_path / "lf.py"
    plan = make_plan(path)
    original = edits.os.fdopen
    seen = []
    def windows_fdopen(fd, mode="r", *args, **kwargs):
        seen.append(mode)
        if "b" not in mode:
            kwargs["newline"] = "\r\n"
        return original(fd, mode, *args, **kwargs)
    monkeypatch.setattr(edits.os, "fdopen", windows_fdopen)
    result = edits.commit_beacon_edit_plan(plan)
    assert result["success"], result
    assert seen == ["wb"]
    assert path.read_bytes() == plan.snapshot.raw_bytes.replace(b"old", b"new")


def test_executor_parses_precheck_snapshot_not_disk_parser(tmp_path, monkeypatch):
    path = tmp_path / "one-read.py"
    plan = make_plan(path)
    def stale_parser(*_args, **_kwargs):
        raise AssertionError("executor reopened its source to parse")
    monkeypatch.setattr(nmc, "_parse_disk_beacons_for_language", stale_parser)
    from beacon_plan_fixture import physical_operand
    result = nmc._execute_beacon_write_plan(language="python", file_path=str(path), name="target", note="",
        write_intent=None, write_plan=SimpleNamespace(success=True, planned_operation="update", source_path=str(path),
            source_sha256=plan.source_sha256, intended_slice_labels=("new",), resolved_comment=None, beacon_id="target",
            **physical_operand(path, "python", "target")))
    assert result.get("success"), result


@pytest.mark.parametrize("point", ["fsync", "replace"])
def test_process_death_leaves_old_or_whole_new_source(tmp_path, point):
    path = tmp_path / "kill.py"
    plan = make_plan(path)
    root = Path(__file__).resolve().parents[1]
    program = "\n".join([
        "import os, sys", "sys.path.insert(0, sys.argv[1])",
        "import beacon_edits as e", "from beacon_source import read_source_snapshot",
        "p=sys.argv[2]; s=read_source_snapshot(p)",
        "a=s.raw_bytes.index(b'old')",
        "plan=e.BeaconEditPlan(s, 'python', (e.BeaconEdit(a,a+3,b'new',2),), (2,))",
        "point=sys.argv[3]", "original=getattr(e.os,point)",
        "def crash(*args, **kwargs):", "    original(*args, **kwargs)", "    os._exit(77)",
        "setattr(e.os,point,crash)", "e.commit_beacon_edit_plan(plan)",
    ])
    completed = subprocess.run([sys.executable, "-c", program, str(root), str(path), point],
                               timeout=20, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 77, completed.stderr.decode()
    expected = plan.snapshot.raw_bytes if point == "fsync" else plan.snapshot.raw_bytes.replace(b"old", b"new")
    assert path.read_bytes() == expected


# Inventory 1: U02-owned paths and the explicitly dispositioned exceptions.
# Counts include temp creation and publication, not just open-w. U08's Markdown
# CLI now delegates to the publisher. Protected phase 6 remains J's R5 join;
# the unwired server helper is not an owned reachable beacon publication path.
# Main's MCI wave routes phase 6 and both server Markdown materializers through
# the one _write_generated_markdown(operation="write") dispatcher (BR-07).
OWNED_PATH_WRITE_INVENTORY = {
    ("beacon_edits.py", "commit_beacon_edit_plan"): (3, "the sole binary publisher"),
    ("src/workflowy_mcp/markdown_generate_pipeline.py", "_write_generated_markdown"): (1, "protected phase-6 early publication (U08 component ready, J R5 outstanding) and both server Markdown materializers outside U02 §14 (BR-07), one dispatcher"),
    # G5: _write_job_payload has zero direct writes; both paths use the sole
    # CartoJobRecord and guarded status funnel. No owned-write exception remains.
    ("src/workflowy_mcp/markdown_generate_pipeline.py", "_append_log"): (1, "job log, not source"),
    ("nexus_map_codebase.py", "<module>"): (1, "__main__ CLI JSON output, not daemon source publication"),
    ("src/workflowy_mcp/server.py", "_maybe_create_ast_beacon_from_tags"): (1, "unwired helper retained, not an owned reachable writer"),
}

# Inventory 2: exact OUT-OF-SCOPE server/diagnostic writers. Decision 435XPX,
# Ledger BR-07: keep these separate from the owned-path allowlist. The two
# materializers really write source, now through the dispatcher in Inventory 1.
OUT_OF_SCOPE_SERVER_WRITE_INVENTORY = {
    ("src/workflowy_mcp/debug_log_rotation.py", "_append_unlocked"): (1, "shared debug log append, including server startup (R-b)"),
    ("src/workflowy_mcp/debug_log_rotation.py", "_rotate"): (2, "debug log generation rename publications (R-b)"),
    ("src/workflowy_mcp/debug_log_rotation.py", "reset_debug_log"): (2, "debug log reset, ordinary and rotation-locked branches (R-b)"),
    ("src/workflowy_mcp/server.py", "_set_cache_refresh_skip"): (1, "cache deadline file"),
    ("src/workflowy_mcp/server.py", "log_event"): (1, "server log append"),
    ("src/workflowy_mcp/server.py", "_ensure_broker_running"): (1, "broker log append"),
    ("src/workflowy_mcp/server.py", "_append_carto_job_log"): (1, "job log append"),
    ("src/workflowy_mcp/server.py", "validate_secret_code"): (1, "secret file, not source"),
    ("src/workflowy_mcp/server.py", "_node_read_impl"): (1, "note-export artifact publication"),
}

# The kit has NO source filesystem front: its source writes use the generated
# publisher. These three pre-existing outputs are the catalog/slice artifacts,
# not source mutation exceptions and not part of the seven server exceptions.
KIT_MAP_ARTIFACT_WRITE_INVENTORY = {
    ("agent-kit/.claude/skills/code-map/scripts/code_map.py", "write_catalog"): (1, "generated label catalog"),
    ("agent-kit/.claude/skills/code-map/scripts/code_map.py", "_slice_write"): (1, "generated Markdown slice"),
    ("agent-kit/.claude/skills/code-map/scripts/code_map.py", "cmd_init"): (1, "initial catalog from template"),
}


def _is_source_write_call(node):
    if not isinstance(node, ast.Call):
        return False
    call = ast.unparse(node.func)
    if call in {"tempfile.mkstemp", "os.replace", "os.rename", "shutil.move"}:
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr in {"write_text", "write_bytes"}:
        return True
    if call in {"open", "builtins.open", "os.fdopen"} or isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(c in mode.value for c in "wax+")
    return False


def _source_write_inventory(sources):
    writes = Counter()
    def visit(node, file, owner="<module>"):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = node.name if owner == "<module>" else owner + "." + node.name
        if _is_source_write_call(node):
            writes[file, owner] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, file, owner)
    for file, text in sources.items():
        visit(ast.parse(text), file)
    return writes


def test_owned_source_writes_have_one_authority_and_pinned_exceptions():
    """42131b6: hygiene/alias/EOF/id still contain unclassified raw writes."""
    root = Path(__file__).resolve().parents[1]
    files = ("nexus_map_codebase.py", "beacon_edits.py", "markdown_roundtrip.py",
             "src/workflowy_mcp/markdown_generate_pipeline.py", "src/workflowy_mcp/server.py",
             "src/workflowy_mcp/debug_log_rotation.py",
             "agent-kit/.claude/skills/code-map/scripts/code_map.py")
    # Generated _engine.py is intentionally excluded: test_agent_kit_build B1
    # pins it byte-for-byte to the source closure (including beacon_edits.py).
    sources = {file: (root / file).read_text(encoding="utf-8") for file in files}
    assert not OWNED_PATH_WRITE_INVENTORY.keys() & OUT_OF_SCOPE_SERVER_WRITE_INVENTORY.keys()
    expected = {key: count for key, (count, reason) in {
        **OWNED_PATH_WRITE_INVENTORY, **OUT_OF_SCOPE_SERVER_WRITE_INVENTORY, **KIT_MAP_ARTIFACT_WRITE_INVENTORY,
    }.items() if reason}
    assert _source_write_inventory(sources) == expected

    owned = {"_execute_beacon_write_plan", "_normalize_duplicate_ast_beacons", "_update_beacon_from_node_absolute",
             "normalize_duplicate_ast_beacons_in_file", "_normalize_duplicate_ast_beacons_markdown",
             "_normalize_duplicate_ast_beacons_python", "_normalize_duplicate_ast_beacons_js_ts",
             "normalize_beacon_comment_field_aliases_in_file", "repair_markdown_c0_controls_on_disk",
             "_normalize_markdown_file_eof", "_disambiguate_duplicate_beacon_ids_in_file",
             "prepare_generated_markdown_beacons", "publish_generated_markdown_beacons", "reapply_markdown_ast_beacons",
             *("update_beacon_from_node_" + lang for lang in ("python", "js_ts", "vue", "markdown", "sql", "shell", "yaml"))}
    found = set()
    for file in ("nexus_map_codebase.py", "src/workflowy_mcp/markdown_generate_pipeline.py", "markdown_roundtrip.py"):
        for function in ast.walk(ast.parse(sources[file])):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name in owned:
                found.add(function.name)
                calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
                assert not any(_is_source_write_call(n) for n in calls), (file, function.name)
                assert not any(ast.unparse(n.func).split(".")[-1] == "_write_markdown_lines_sanitized" for n in calls), (file, function.name)
    assert found == owned
    kit = ast.parse(sources["agent-kit/.claude/skills/code-map/scripts/code_map.py"])
    retired = {"_line_starts", "_detected_newline", "_block_text", "_replace_lines_new_text",
               "_insert_lines_new_text", "_delete_lines_new_text", "_ast_physical_row"}
    assert not retired & {n.name for n in kit.body if isinstance(n, ast.FunctionDef)}
    for function in kit.body:
        if isinstance(function, ast.FunctionDef) and function.name in {"_atomic_replace", "_write_source_text", "cmd_tag", "cmd_untag"}:
            assert not any(_is_source_write_call(n) for n in ast.walk(function)), function.name


@pytest.mark.parametrize("call", ["open(p, 'w')", "open(p, mode='wb')", "open(p, 'a')", "open(p, 'x')",
    "os.fdopen(fd, 'wb')", "tempfile.mkstemp()", "os.replace(a, b)", "os.rename(a, b)",
    "shutil.move(a, b)", "Path(p).write_text(s)", "p.write_bytes(b'')"])
def test_source_write_inventory_detects_a_planted_extra_writer(call):
    """A new owner cannot hide behind an existing file-level exception."""
    source = "def extra_writer():\n    " + call + "\n"
    assert _source_write_inventory({"nexus_map_codebase.py": source}) == {("nexus_map_codebase.py", "extra_writer"): 1}
    assert ("nexus_map_codebase.py", "extra_writer") not in OWNED_PATH_WRITE_INVENTORY


def test_seven_legacy_adapters_have_exact_delegation_and_one_publication_recipe():
    """Pin exact call inventory, not a file-wide allowance for extra writers.

    The former private id finders, builders, qualname deletes and row-bridge
    commits must be gone from these public bodies, not shadowed by a new path.
    """
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'nexus_map_codebase.py').read_text(encoding='utf-8'))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for language in ('python', 'js_ts', 'vue', 'markdown', 'sql', 'shell', 'yaml'):
        fn = functions['update_beacon_from_node_' + language]
        calls = Counter(ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call))
        assert calls == {'_assert_write_intent_plan_pair': 1, '_execute_beacon_write_plan': 1,
                         '_update_beacon_from_node_absolute': 1}, (fn.name, calls)
        assert not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn for n in ast.walk(fn))
    recipe = functions['_update_beacon_from_node_absolute']
    calls = Counter(ast.unparse(n.func) for n in ast.walk(recipe) if isinstance(n, ast.Call))
    assert {name: calls[name] for name in ('read_source_snapshot', '_beacon_selection_structures',
        'select_beacon_block_for_node', 'plan_beacon_node_mutation', 'commit_beacon_edit_plan',
        'allocate_beacon_id')} == {'read_source_snapshot': 1, '_beacon_selection_structures': 1,
            'select_beacon_block_for_node': 1, 'plan_beacon_node_mutation': 2,
            'commit_beacon_edit_plan': 1, 'allocate_beacon_id': 1}
    assert calls['_generate_auto_beacon_hash'] == 0
    assert not any(calls[n] for n in ('_commit_beacon_row_changes', '_find_anchor_matched_beacons',
        '_find_beacon_close_block_extent', '_build_beacon_block_for_language', '_extract_show_span_from_note'))
    assert '_find_vue_beacon_block_by_exact_id' not in functions
    assert '_commit_beacon_row_changes' not in functions
    adapter_callers = {fn.name for fn in functions.values() if any(isinstance(n, ast.Call) and
        ast.unparse(n.func) == '_normalize_duplicate_ast_beacons' for n in ast.walk(fn))}
    assert adapter_callers == {'_normalize_duplicate_ast_beacons_markdown', '_normalize_duplicate_ast_beacons_python',
                               '_normalize_duplicate_ast_beacons_js_ts'}


def test_kit_ordinary_updates_have_only_shared_selection_and_field_planning():
    """Retire the kit's id-cardinality extent and full-rebuild update authority."""
    path = Path(__file__).resolve().parents[1] / 'agent-kit/.claude/skills/code-map/scripts/code_map.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    functions = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    def calls(node):
        return Counter(ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call))
    attached = calls(functions['_attached_block'])
    assert attached['_engine.select_beacon_block_for_node'] == 1
    assert attached['_engine._beacon_block_extent_for_language'] == attached['repo.blocks'] == 0
    tag, untag = functions['cmd_tag'], functions['cmd_untag']
    for fn in (tag, untag):
        assert calls(fn)['_engine.plan_beacon_node_mutation'] == 1
    assert calls(untag)['_engine._build_beacon_block_for_language'] == calls(untag)['_source_edit_plan'] == 0
    ordinary = next(node for node in ast.walk(tag) if isinstance(node, ast.If) and
                    ast.unparse(node.test) == "state.get('block') is not None")
    body = ast.Module(body=ordinary.body, type_ignores=[])
    creates = ast.Module(body=ordinary.orelse, type_ignores=[])
    assert calls(body)['_engine._build_beacon_block_for_language'] == calls(body)['_source_edit_plan'] == 0
    assert calls(creates)['_engine._build_beacon_block_for_language'] == 1
    assert calls(creates)['_engine.allocate_beacon_id'] == calls(creates)['_source_edit_plan'] == 1


@pytest.mark.parametrize('language', ['vue', 'markdown'])
def test_vue_markdown_pair_branch_and_markdown_keyword_occurrence_are_retained(monkeypatch, language):
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    for kwargs in ({'write_intent': object()}, {'write_plan': object()}):
        with pytest.raises(AssertionError):
            writer('not opened', 'name', 'note', **kwargs)
    seen = []
    sentinel = {'delegated': True}
    def executor(**kwargs):
        seen.append(kwargs)
        return sentinel
    monkeypatch.setattr(nmc, '_execute_beacon_write_plan', executor)
    kwargs = {'occurrence': 3} if language == 'markdown' else {}
    assert writer('not opened', 'name', 'note', write_intent=object(), write_plan=object(), **kwargs) is sentinel
    assert len(seen) == 1 and seen[0]['language'] == language
    if language == 'markdown':
        assert seen[0]['occurrence'] == 3


@pytest.mark.parametrize('language', ['vue', 'markdown'])
def test_vue_markdown_recipe_uses_one_observation_and_one_commit_with_local_cas(tmp_path, monkeypatch, language):
    from beacon_field_layouts import VUE, ADOPT_MD
    rows = VUE if language == 'vue' else ADOPT_MD
    if language == 'markdown':
        rows = rows + [s.replace('md@sec', 'safe').replace('## Section', '## Safe') for s in ADOPT_MD[2:]]
    path = tmp_path / ('cas.vue' if language == 'vue' else 'cas.md')
    before = ('\n'.join(rows) + '\n').encode()
    path.write_bytes(before)
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    observe, commit = nmc.read_source_snapshot, nmc.commit_beacon_edit_plan
    seen, proposed, state = [], [], {}
    external = before + b'\nexternal save\n'
    def read(*args, **kwargs):
        snapshot = observe(*args, **kwargs)
        seen.append(snapshot)
        return snapshot
    def save(plan, **kwargs):
        proposed.append(plan)
        assert len(seen) == 1 and plan.snapshot is seen[0]
        path.write_bytes(external)
        state['mtime'] = path.stat().st_mtime_ns
        return commit(plan, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(nmc, 'read_source_snapshot', read)
        patch.setattr(nmc, 'commit_beacon_edit_plan', save)
        result = writer(str(path), 'target #new', 'BEACON (AST)\nid: ' + ('s0@a' if language == 'vue' else 'md@sec') + '\n')
    assert len(seen) == len(proposed) == 1
    assert result['error_kind'] == 'source_changed_since_plan' and result['physical_witness'], result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (external, state['mtime'])
    good = writer(str(path), 'safe #new', 'BEACON (AST)\nid: ' + ('s1@b' if language == 'vue' else 'safe') + '\n')
    assert good['operation'] == 'updated_beacon', good
    if language == 'vue':
        expected = external.replace(b'slice_labels=b1,', b'slice_labels=new,')
    else:
        boundary = external.rindex(b'<!--')
        expected = external[:boundary] + external[boundary:].replace(b'slice_labels=old1,', b'slice_labels=new,')
    assert path.read_bytes() == expected


@pytest.mark.parametrize('language,prefix,suffix', [('python', '#', '.py'), ('js_ts', '//', '.ts'),
    ('sql', '--', '.sql'), ('shell', '#', '.sh'), ('yaml', '#', '.yaml')])
def test_legacy_recipe_observes_once_and_cas_failure_is_local(tmp_path, monkeypatch, language, prefix, suffix):
    path = tmp_path / ('cas' + suffix)
    rows = [prefix + ' @beacon[', prefix + ' id=target,', prefix + ' slice_labels=old,', prefix + ' kind=span,', prefix + ' ]']
    safe = [s.replace('id=target,', 'id=safe,') for s in rows]
    raw = ('\n'.join(rows + ['body()'] + safe + ['safe_body()']) + '\n').encode()
    path.write_bytes(raw)
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    read, commit = nmc.read_source_snapshot, nmc.commit_beacon_edit_plan
    seen = []
    state = {}
    def observe(*args, **kwargs):
        snapshot = read(*args, **kwargs)
        seen.append(snapshot)
        return snapshot
    def save_after_plan(plan, **kwargs):
        assert plan.snapshot is seen[0]
        path.write_bytes(raw + b'editor save\n')
        state['mtime'] = path.stat().st_mtime_ns
        return commit(plan, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy private row path remained reachable')
    with monkeypatch.context() as patch:
        patch.setattr(nmc, 'read_source_snapshot', observe)
        patch.setattr(nmc, 'commit_beacon_edit_plan', save_after_plan)
        assert not hasattr(nmc, '_commit_beacon_row_changes')
        patch.setattr(nmc, '_normalize_duplicate_ast_beacons', forbidden)
        result = writer(str(path), 'target #new', 'BEACON (SPAN)\nid: target\n')
    assert len(seen) == 1
    assert result['operation'] == 'noop' and result['error_kind'] == 'source_changed_since_plan', result
    assert result['physical_witness'] and result['removed_labels'] == ['old']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw + b'editor save\n', state['mtime'])
    good = writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')
    assert good['operation'] == 'updated_beacon', good
    boundary = raw.index((prefix + ' @beacon[').encode(), 1)
    assert path.read_bytes() == raw[:boundary] + raw[boundary:].replace(b'slice_labels=old,', b'slice_labels=new,') + b'editor save\n'


@pytest.mark.parametrize('language', ['python', 'js_ts', 'sql', 'shell', 'yaml'])
def test_legacy_pair_branch_is_unchanged_and_noop_reasons_are_distinct(tmp_path, monkeypatch, language):
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    for kwargs in ({'write_intent': object()}, {'write_plan': object()}):
        with pytest.raises(AssertionError):
            writer('not opened', 'name', 'note', **kwargs)
    sentinel = {'delegated': True}
    with monkeypatch.context() as patch:
        patch.setattr(nmc, '_execute_beacon_write_plan', lambda **kw: sentinel)
        assert writer('not opened', 'name', 'note', write_intent=object(), write_plan=object()) is sentinel
    path = tmp_path / 'empty.txt'
    path.write_bytes(b'ordinary code\n')
    before = path.read_bytes(), path.stat().st_mtime_ns
    absent = writer(str(path), 'name', '')
    assert absent['operation'] == 'noop' and absent['noop_reason'] == 'no_beacon_at_target', absent
    missing = writer(str(path), 'name #new', 'BEACON (SPAN)\nid: gone\n')
    assert missing['operation'] == 'noop' and missing['noop_reason'] == 'missing_beacon_id', missing
    assert missing['error_kind'] == 'beacon_identity_conflict'
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    prefix = '//' if language == 'js_ts' else '--' if language == 'sql' else '#'
    path.write_bytes(('\n'.join([prefix + ' @beacon[', prefix + ' id=safe,', prefix + ' slice_labels=old,', prefix + ' kind=span,', prefix + ' ]']) + '\nbody()\n').encode())
    assert writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')['operation'] == 'updated_beacon'
