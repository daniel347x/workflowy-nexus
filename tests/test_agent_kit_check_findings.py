"""U05 §6.6: the CLI and both input adapters consume the one vendored rulebook."""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src", ROOT / "agent-kit/.claude/skills/code-map/scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import code_map as cm
from workflowy_mcp import beacon_integrity as bi
from beacon_check_fixtures import PROBLEM_FIXTURES, CLEAN_FIXTURES, DIAG_FIXTURES, SEAM_FIXTURES
from test_beacon_check_engine import _install_write_guard, _FORBIDDEN
import nexus_map_codebase as nmc
from test_agent_kit_commands import block, catalog_text, make_repo, run_kit


FIXTURES = {**PROBLEM_FIXTURES, **CLEAN_FIXTURES, **DIAG_FIXTURES, **SEAM_FIXTURES}
# Deliberate scope omissions: Markdown and Vue are not v1 languages; .bash is
# another daemon-supported suffix, but the kit's shell suffix remains .sh only.
OMITTED_EXTENSIONS = {".md", ".vue", ".bash"}
SUPPORTED = {name: fixture for name, fixture in FIXTURES.items()
             if Path(fixture[0]).suffix in cm.V1_EXTENSIONS}


def _signature(rows):
    return sorted((row["code"], row["severity"], row["line"],
                   tuple(member["line"] for member in row["members"])) for row in rows)


@pytest.mark.parametrize("name", sorted(SUPPORTED))
def test_supported_fixture_rows_match_core(tmp_path, monkeypatch, name):
    """Before K the kit has no common findings path; this is not a text-only shim."""
    filename, text = SUPPORTED[name]
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mtime_ns
    _install_write_guard(monkeypatch)
    def trap(*args, **kwargs):
        pytest.fail("check entered a writer")
    for module in (nmc, cm._engine):
        for symbol in _FORBIDDEN:
            if hasattr(module, symbol):
                monkeypatch.setattr(module, symbol, trap)
    repo = cm.Repository(str(tmp_path), [filename])
    rows = repo.findings(filename)
    expected = bi.check_beacon_file(str(path), display_path=filename)
    assert _signature(rows) == _signature(expected)
    assert {r["finding_id"] for r in rows} == {r["finding_id"] for r in expected}
    for row in rows:
        if row["view"] == "writer":
            assert row["authority"].startswith("code-map metadata capability")
        if row["code"] in {"two_blocks_one_symbol", "writer_anchor_ambiguous", "legacy_field"}:
            counterpart = next(r for r in expected if r["finding_id"] == row["finding_id"])
            assert row["fix"] == counterpart["fix"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_unsupported_extensions_are_explicit_not_expanded():
    assert {Path(f[0]).suffix for f in FIXTURES.values()} - set(cm.V1_EXTENSIONS) == OMITTED_EXTENSIONS
    assert cm.V1_EXTENSIONS == (".py", ".js", ".jsx", ".ts", ".tsx", ".sql", ".sh", ".yml", ".yaml")


@pytest.mark.parametrize("case", ["idless", "duplicate-group", "module", "resemblance"])
def test_special_physical_cases_match_core_and_cli(tmp_path, case):
    text = _special_case_text(case)
    repo_path = make_repo(tmp_path, {"x.py": text}, catalog=catalog_text([]))
    path = tmp_path / "x.py"
    before = path.read_bytes(), path.stat().st_mtime_ns
    rows = cm.Repository(str(tmp_path), ["x.py"]).findings("x.py")
    expected = bi.check_beacon_file(str(path), display_path="x.py")
    assert _signature(rows) == _signature(expected)
    codes = collections.Counter(r["code"] for r in rows)
    if case == "idless":
        assert codes["missing_id"] == 2 and not codes["duplicate_id"]
    elif case == "duplicate-group":
        duplicates = [r for r in rows if r["code"] == "duplicate_id"]
        assert len(duplicates) == 1 and len(duplicates[0]["members"]) == 3
    elif case == "module":
        assert codes["block_not_mapped"] == 1
    else:
        assert [r["severity"] for r in rows if r["code"] == "id_unlike_symbol"] == ["info"]
    result = run_kit(repo_path, "check")
    assert result.returncode == (1 if any(r["severity"] != "info" for r in rows) or
                                  "catalog:" in result.stdout.decode() else 0)
    printed = [line for line in result.stdout.decode().splitlines() if line.startswith("x.py:")]
    assert printed == [f"x.py:{r['line']}: {r['severity']} {r['code']}: {r['message']}" for r in rows]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def _special_case_text(case):
    if case == "idless":
        text = (block("One.", bid="", labels="d") + "\ndef one():\n    pass\n\n" +
                block("Two.", bid="", labels="d") + "\ndef two():\n    pass\n")
    elif case == "duplicate-group":
        text = "\n".join(block("Copy.", bid="d@copy", labels="d") +
                           f"\ndef f{i}():\n    pass\n" for i in range(3))
    elif case == "module":
        text = block("Module.", bid="d@module", labels="d") + '\n"""Module docstring."""\n'
    else:
        text = block("Descriptive.", bid="d@other", labels="d") + "\ndef target():\n    pass\n\ndef other():\n    pass\n"
    return text


@pytest.mark.parametrize("case", ["idless", "duplicate-group", "module", "resemblance"])
def test_cli_physical_classification_before_any_python_api(tmp_path, case):
    """Independent CLI witnesses reach the old private classification on P."""
    repo = make_repo(tmp_path, {"x.py": _special_case_text(case)},
                     catalog=catalog_text([("d", 1 if case == "resemblance" else 2 if case == "idless" else 3, "Demo.")]
                                          if case != "module" else []))
    assert run_kit(repo, "slice", "--write").returncode == 0
    result = run_kit(repo, "check")
    output = result.stdout.decode()
    if case == "idless":
        assert output.count(": error missing_id:") == 2
        assert "duplicate_id" not in output and "duplicate block id" not in output
    elif case == "duplicate-group":
        assert output.count(": error duplicate_id:") == 1
        assert "3 physical copies" in output
    elif case == "module":
        assert output.count(": error block_not_mapped:") == 1
    else:
        assert output.count(": info id_unlike_symbol:") == 1
        assert result.returncode == 0, output


def test_info_only_open_span_keeps_green_exit_and_safe_neighbour(tmp_path):
    text = block("Region.", bid="d@region", labels="d", kind="span") + "\nVALUE = 1\n"
    neighbour = block("Neighbour.", bid="d@safe", labels="d") + "\ndef safe():\n    pass\n"
    repo = make_repo(tmp_path, {"open.py": text, "safe.py": neighbour},
                     catalog=catalog_text([("d", 2, "Regions and symbols.")]))
    assert run_kit(repo, "slice", "--write").returncode == 0
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (repo / "open.py", repo / "safe.py")}
    result = run_kit(repo, "check")
    assert result.returncode == 0, result.stdout
    assert result.stdout.decode().splitlines() == [
        "open.py:1: info span_without_close: This legal open-only span runs to its file/section end; keep it or explicitly choose an exact close."]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


@pytest.mark.parametrize("suffix,prefix", [(".py", "#"), (".js", "//"), (".sql", "--"), (".sh", "#"), (".yml", "#")])
def test_close_only_file_cli_matches_core(tmp_path, suffix, prefix):
    """The old opener-only prefilter hid structural orphans before core entry."""
    name = "orphan" + suffix
    text = "\n".join([prefix + " @beacon-close[", prefix + "   id=d@absent,", prefix + " ]", ""])
    repo = make_repo(tmp_path, {name: text}, catalog=catalog_text([]))
    path = repo / name
    before = path.read_bytes(), path.stat().st_mtime_ns
    expected = bi.check_beacon_file(str(path), display_path=name)
    assert [(r["code"], r["severity"], r["line"]) for r in expected] == [("orphan_close", "info", 1)]
    result = run_kit(repo, "check")
    assert result.returncode == 0, result.stdout
    assert result.stdout.decode().splitlines() == [
        f"{name}:1: info orphan_close: This structural close matches no unique eligible exact-id span opener; ordinary marker mentions are not close candidates."]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
