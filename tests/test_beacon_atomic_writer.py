"""SPEC v5 §7/§10.5 + §6 -- atomic source writer, CAS, fail-closed anchors.

Commit-1 regression locks for the Lightning Rod beacon mutation seam work:

* ``_write_markdown_lines_sanitized`` is now an ATOMIC REPLACE (temp file in
  the same directory -> flush+fsync -> copymode (permissions only, fresh
  mtime) -> os.replace), with an optional ``expected_sha256`` CAS.
* py/js Case-2 beacon creation is FAIL-CLOSED on an unresolved AST anchor
  (the top-of-file ``insert_idx = 0`` fallback is deleted).
* Ordinary Markdown updates publish EXACTLY ONCE through the shared byte-edit
  commit, with no whole-file control-char pre-write or generation serializer.
"""

import asyncio
import hashlib
import os
import stat
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import nexus_map_codebase as nmc  # noqa: E402


# ---------------------------------------------------------------------------
# Atomic writer basics + CAS
# ---------------------------------------------------------------------------


def test_atomic_writer_exactly_one_trailing_newline(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("a\n\n\n", encoding="utf-8")
    nmc._write_markdown_lines_sanitized(str(p), ["a", "", ""])
    assert p.read_text(encoding="utf-8") == "a\n"


def test_atomic_writer_cas_match_writes(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("line1\nline2\n", encoding="utf-8")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    nmc._write_markdown_lines_sanitized(
        str(p), ["line1", "line2", "line3"], expected_sha256=digest
    )
    assert p.read_text(encoding="utf-8") == "line1\nline2\nline3\n"


def test_atomic_writer_cas_mismatch_writes_nothing(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("original\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(nmc.SourceChangedSincePlanError):
        nmc._write_markdown_lines_sanitized(
            str(p), ["changed"], expected_sha256="0" * 64
        )
    assert p.read_bytes() == before


def test_generation_adapter_final_hash_follows_temp_io(tmp_path, monkeypatch):
    """U02: compatibility serialization delegates IO, including the late hash."""
    p = tmp_path / "generated.md"
    p.write_bytes(b"original\n")
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    original = nmc.shutil.copymode
    external = b"editor saved during temp preparation\r\n"
    state = {}
    def copymode(*args, **kwargs):
        original(*args, **kwargs)
        p.write_bytes(external)
        state["mtime"] = p.stat().st_mtime_ns
    with monkeypatch.context() as patch:
        patch.setattr(nmc.shutil, "copymode", copymode)
        with pytest.raises(nmc.SourceChangedSincePlanError) as raised:
            nmc._write_markdown_lines_sanitized(str(p), ["new"], expected_sha256=expected)
    assert raised.value.evidence["expected"]["sha256"] == expected
    assert raised.value.evidence["actual"]["sha256"] == hashlib.sha256(external).hexdigest()
    assert (p.read_bytes(), p.stat().st_mtime_ns) == (external, state["mtime"])
    # Independent stable work remains usable; no retry or rollback.
    nmc._write_markdown_lines_sanitized(str(p), ["good"])
    assert p.read_bytes() == b"good\n"


@pytest.mark.parametrize("fault", ["external_save", "fsync"])
def test_claimed_c0_commit_failure_is_local_and_structured(tmp_path, monkeypatch, fault):
    path = tmp_path / "repair.md"
    raw = b"\xef\xbb\xbf# H\r\ntext\x1b\x0c\r\n\r\n"
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    logs, state = [], {}
    external = raw + b"external save\r\n"
    with monkeypatch.context() as patch:
        if fault == "external_save":
            original = nmc.shutil.copymode
            def interpose(*args, **kwargs):
                original(*args, **kwargs)
                path.write_bytes(external)
                state["mtime"] = path.stat().st_mtime_ns
            patch.setattr(nmc.shutil, "copymode", interpose)
        else:
            def fail(*_args, **_kwargs):
                raise PermissionError("injected fsync failure")
            patch.setattr(os, "fsync", fail)
        result = nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed", log_callback=logs.append)
    expected_kind = "source_changed_since_plan" if fault == "external_save" else "disk_write_failed"
    assert result.get("error_kind") == expected_kind, result
    assert result["success"] is False and result["changed"] is False
    assert result["path"] == str(path) and result["counts"] == {"C0-1B": 1, "FF": 1}
    assert result["evidence"]["expected"] != result["evidence"]["actual"]
    expected = (external, state["mtime"]) if fault == "external_save" else before
    assert (path.read_bytes(), path.stat().st_mtime_ns) == expected
    assert logs and expected_kind in logs[0]
    assert logs[0].startswith("Claimed Markdown C0 repair")
    assert not result["events"] and not result["edited_ranges"] and result["after_sha256"] is None
    assert len(result["findings"]) == len(result["skipped_repairs"]) == 1
    finding = result["findings"][0]
    assert finding["operation"] == "repair_markdown_c0" and finding["reason"] == expected_kind
    assert finding["file_path"] == str(path) and finding["candidates"]
    assert finding["commit_evidence"]["evidence"] == result["evidence"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    neighbour = tmp_path / "safe.md"
    neighbour.write_bytes(raw)
    safe = nmc.repair_markdown_c0_controls_on_disk(str(neighbour), claim_id="claimed")
    assert safe["success"] and safe["changed"]
    assert neighbour.read_bytes() == raw.replace(b"\x1b", b"").replace(b"\x0c", b"\\f")
    assert safe["events"][0]["status"] == "committed" and not safe["findings"]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("unterminated", [False, True])
def test_claimed_c0_repair_changes_only_explicit_controls(tmp_path, ending, unterminated):
    path = tmp_path / "repair.md"
    raw = b"\xef\xbb\xbf# H" + ending + 'literal \x1b\x0c\x0b\x85\u2028\u2029'.encode() + ending + b"mixed\r\n\r\n"
    if unterminated:
        raw = raw[:-2] + b"unterminated"
    path.write_bytes(raw)
    os.chmod(path, 0o744)
    result = nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed")
    assert result["success"], result
    assert path.read_bytes() == raw.replace(b"\x1b", b"").replace(b"\x0c", b"\\f").replace(b"\x0b", b"\\v")
    assert stat.S_IMODE(path.stat().st_mode) == 0o744
    assert result["events"][0]["before_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["events"][0]["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = path.stat().st_mtime_ns
    assert not nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed")["changed"]
    assert path.stat().st_mtime_ns == mtime


def test_claimed_c0_keeps_claim_gate_and_cancels_after_temp_preparation(tmp_path, monkeypatch):
    path = tmp_path / "cancel.md"
    path.write_bytes(b"# H\ntext\x0b\n")
    before = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(ValueError):
        nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="")
    cancelled = [False]
    original = os.fsync
    def fsync(fd):
        original(fd)
        cancelled[0] = True
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(RuntimeError):
            nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed", cancel_check=lambda: cancelled[0])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    assert nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed")["success"]


def test_claimed_c0_read_io_failure_is_local_finding_not_authority_or_cancel(tmp_path, monkeypatch):
    """ab0653d raises read errors; content failures must not abort refresh."""
    path = tmp_path / "read.md"
    raw = b"# H\r\ntext\x07\r\n"
    path.write_bytes(raw)
    before = path.read_bytes(), path.stat().st_mtime_ns
    logs = []
    with monkeypatch.context() as patch:
        def fail(_path):
            raise PermissionError("injected read failure")
        patch.setattr(nmc, "read_source_snapshot", fail)
        result = nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed", log_callback=logs.append)
        with pytest.raises(ValueError):
            nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="")
    assert not result["success"] and not result["changed"] and result["error_kind"] == "io_error"
    assert result["path"] == str(path) and result["counts"] == {}
    assert result["findings"][0] == {"operation": "repair_markdown_c0", "reason": "io_error", "file_path": str(path),
                                     "error": "injected read failure", "candidates": []}
    assert not result["events"] and not result["edited_ranges"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert logs[0].startswith("Claimed Markdown C0 repair") and "io_error" in logs[0]
    with pytest.raises(RuntimeError):
        nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed", cancel_check=lambda: True)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    safe = nmc.repair_markdown_c0_controls_on_disk(str(path), claim_id="claimed")
    assert safe["success"] and safe["changed"] and path.read_bytes() == raw.replace(b"\x07", b"\\a")


def test_atomic_writer_injected_failure_leaves_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    p = tmp_path / "f.md"
    p.write_text("original\n", encoding="utf-8")
    before = p.read_bytes()

    def _boom(*_a, **_k):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        nmc._write_markdown_lines_sanitized(str(p), ["changed"])
    assert p.read_bytes() == before
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".beacon_atomic_")]
    assert leftovers == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are near-no-op on Windows")
def test_atomic_writer_preserves_posix_mode_bits(tmp_path):
    p = tmp_path / "script.sh"
    p.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(p, 0o744)
    nmc._write_markdown_lines_sanitized(str(p), ["#!/bin/sh", "echo ho"])
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o744
    assert p.read_text(encoding="utf-8") == "#!/bin/sh\necho ho\n"


# ---------------------------------------------------------------------------
# Fail-closed py/js Case-2 anchoring (insert_idx=0 fallback deleted)
# ---------------------------------------------------------------------------


def test_python_case2_unresolved_ast_anchor_fails_closed(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("# header comment\n\ndef foo():\n    return 1\n", encoding="utf-8")
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    res = nmc.update_beacon_from_node_python(
        str(p), "Foo #newtag", "AST_QUALNAME: missing_fn\n---\n"
    )
    assert res.get("error")
    assert res['error_kind'] == 'no_beacon_anchor_for_node'
    assert res['noop_reason'] == 'no_beacon_anchor_for_node'
    assert res.get("operation") == "noop"
    assert (p.read_bytes(), p.stat().st_mtime_ns) == (before, mtime)
    assert nmc.update_beacon_from_node_python(str(p), 'foo #new', 'AST_QUALNAME: foo\n')['operation'] == 'created_beacon'


def test_python_case2_creates_above_target_not_top_of_file(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("# header comment\n\ndef foo():\n    return 1\n", encoding="utf-8")
    res = nmc.update_beacon_from_node_python(
        str(p), "Foo #newtag", "AST_QUALNAME: foo\n---\n"
    )
    assert res.get("operation") == "created_beacon"
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# header comment"
    def_idx = lines.index("def foo():")
    block_head = max(0, def_idx - 6)
    assert any("@beacon[" in lines[k] for k in range(block_head, def_idx))


@pytest.mark.skipif(not nmc._HAVE_TREE_SITTER, reason="tree-sitter not available")
def test_js_case2_unresolved_ast_anchor_fails_closed(tmp_path):
    p = tmp_path / "m.js"
    p.write_text("// header\nfunction foo() { return 1; }\n", encoding="utf-8")
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    res = nmc.update_beacon_from_node_js_ts(
        str(p), "Foo #newtag", "AST_QUALNAME: missing_fn\n---\n"
    )
    assert res.get("error")
    assert res['error_kind'] == 'no_beacon_anchor_for_node'
    assert res['noop_reason'] == 'no_beacon_anchor_for_node'
    assert res.get("operation") == "noop"
    assert (p.read_bytes(), p.stat().st_mtime_ns) == (before, mtime)
    assert nmc.update_beacon_from_node_js_ts(str(p), 'foo #new', 'AST_QUALNAME: foo\n')['operation'] == 'created_beacon'


# ---------------------------------------------------------------------------
# MD path: ordinary update calls the shared commit EXACTLY ONCE, even with
# literal controls outside metadata. The generation serializer is forbidden.
# ---------------------------------------------------------------------------


def test_md_update_invokes_atomic_writer_exactly_once(tmp_path, monkeypatch):
    md = tmp_path / "doc.md"
    md.write_text(
        "# Doc\n"
        "\n"
        "<!--\n"
        "@beacon[\n"
        "  id=auto-beacon@abcd,\n"
        "  role=Target,\n"
        "  slice_labels=old,\n"
        "  kind=ast,\n"
        "]\n"
        "-->\n"
        "## Target\n"
        "\n"
        "body\x1b\f\u2028\r\n\r\n",
        encoding="utf-8",
    )
    note = (
        "MD_PATH:\n# Doc\n## Target\n---\n"
        "BEACON (MD AST)\nid: auto-beacon@abcd\nrole: Target\n"
        "slice_labels: old\nkind: ast\n---\n"
    )

    calls = []
    before = md.read_bytes()
    real_writer = nmc.commit_beacon_edit_plan

    def _counting_writer(plan, **kwargs):
        calls.append(plan)
        return real_writer(plan, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary update reached whole-document serializer")
    monkeypatch.setattr(nmc, "commit_beacon_edit_plan", _counting_writer)
    monkeypatch.setattr(nmc, "_write_markdown_lines_sanitized", forbidden)
    res = nmc.update_beacon_from_node_markdown(
        str(md), "Target #new", note, occurrence=None
    )
    assert res.get("operation") == "updated_beacon", res
    assert len(calls) == 1
    assert calls[0].snapshot.raw_bytes == before
    assert calls[0].permitted_structural_effects == ()
    assert md.read_bytes() == before.replace(b"slice_labels=old,", b"slice_labels=new,")
    content = md.read_text(encoding="utf-8")
    assert "slice_labels=new," in content
    assert "slice_labels=old," not in content
