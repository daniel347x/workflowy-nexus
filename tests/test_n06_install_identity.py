"""N06 T04/T05: real source ordering and actual artifact hook, isolated installs.

No daemon/shim/broker is started here. Real builds use tmp_path fixtures with
fixed source timestamps, so their subprocess bounds are hang guards rather than
idle-machine speed thresholds. N06_TEST_MUTATION is test-only and provides the
explicit red probes recorded in the implementation Result.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from n06_harness import SETUP_WAIT, Scenario
from workflowy_mcp import daemon_identity as identity

REPO = Path(__file__).resolve().parents[1]
HASH_A = "a" * 40
HASH_B = "b" * 40
SOURCE_TIME = 1700000000


@pytest.fixture(autouse=True)
def isolated_identity_environment(monkeypatch):
    scenario = Scenario.create()
    scenario.install_environment(monkeypatch)
    monkeypatch.setattr(identity, "_snapshot", None)
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    if mutation == "version_only":
        def compare(local, peer):
            lv, pv = identity._source(local)[0], identity._source(peer)[0]
            return "equal" if lv == pv else "local_newer" if lv > pv else "peer_newer"
        monkeypatch.setattr(identity, "compare_install_identity", compare)
    elif mutation == "lexicographic_hash":
        def compare(local, peer):
            lc, pc = identity._source(local)[1], identity._source(peer)[1]
            return "equal" if lc == pc else "local_newer" if str(lc) > str(pc) else "peer_newer"
        monkeypatch.setattr(identity, "compare_install_identity", compare)
    elif mutation == "missing_equal":
        real_compare = identity.compare_install_identity
        monkeypatch.setattr(identity, "compare_install_identity", lambda l, p: "equal" if real_compare(l, p) == "unknown" else real_compare(l, p))
    elif mutation == "metadata_each_call":
        monkeypatch.setattr(identity, "installed_identity", identity._load_installed_identity)
    elif mutation == "capability_removed":
        monkeypatch.setattr(identity, "can_replace_daemon", lambda *args: True)
    elif mutation == "all_posix_attach_only":
        real_capability = identity.can_replace_daemon
        monkeypatch.setattr(identity, "can_replace_daemon", lambda platform, *args: False if platform in {"linux", "darwin"} else real_capability(platform, *args))
    yield scenario


def source(commit=HASH_A, stamp_time=SOURCE_TIME, version="1.0.1", kind="git"):
    return {"package_version": version, "commit": commit, "commit_time": stamp_time, "identity_kind": kind}


@pytest.mark.parametrize(("local", "peer", "expected"), [
    (source(HASH_A, 20), source(HASH_B, 10), "local_newer"),
    (source(HASH_A, 10), source(HASH_B, 20), "peer_newer"),
    (source(HASH_A, 10), source(HASH_A, 20), "equal"),
    (source(HASH_A, None), source(HASH_A, 20), "equal"),
    (source(HASH_A, 10, "1.0rc1"), source(HASH_B, 1, "1.0"), "peer_newer"),
    (source(None, None, "1.0.post1", "editable"), source(HASH_B, 20, "1.0"), "local_newer"),
    (source(version="bad version"), source(), "unknown"),
    (source(version=None), source(), "unknown"),
    (source(HASH_A, 10), source(HASH_B, 10), "unknown"),
    (source(None, None), source(None, None), "unknown"),
    (source(HASH_A, None), source(HASH_B, 10), "unknown"),
    (source(HASH_A, True), source(HASH_B, 10), "unknown"),
    (source(HASH_A, -1), source(HASH_B, 10), "unknown"),
    (source(HASH_A.upper(), 20), source(HASH_B, 10), "unknown"),
    (source("0000", 20), source(HASH_B, 10), "unknown"),
    (source(HASH_A, 20, kind="future_kind"), source(), "unknown"),
    (source(HASH_A, 20, kind=[]), source(), "unknown"),
    (source(None, None, kind="editable"), source(), "unknown"),
    (source(None, None, kind="dirty"), source(), "unknown"),
    (source("0" * 64, 20), source("f" * 64, 10), "local_newer"),
])
def test_source_order_is_partial_not_version_only_or_hash_order(local, peer, expected):
    assert identity.compare_install_identity(local, peer) == expected


@pytest.mark.parametrize(("left", "right", "code", "reason"), [
    (source(), source(None, None), "identity_unknown", "source identity unavailable"),
    (source(), source(HASH_B), "identity_unknown", "different commits share a source timestamp"),
    (source(), source(stamp_time=SOURCE_TIME + 1), "identity_metadata_mismatch", "identical source commits"),
    (source(), source(version="malformed"), "identity_unknown", "unrecognized identity metadata"),
])
def test_comparison_diagnostics_are_nonsecret_and_nonfatal(left, right, code, reason):
    (warning,) = identity.comparison_warnings(left, right)
    assert warning.code == code and reason in warning.message
    assert set(warning.payload()) == {"code", "message", "recovery"}


def test_missing_full_identity_kind_is_not_assumed_git(monkeypatch):
    """G1-07: missing full provenance warns; compact targets and version order survive."""
    fault = os.environ.get("CUT_G1_TEST_MUTATION", "")
    if fault == "identity_old_kind":
        def old_source(value):
            if isinstance(value, identity.InstalledIdentity):
                return value.package_version, value.commit, value.commit_time, value.identity_kind
            return value.get("package_version"), value.get("commit"), value.get("commit_time"), value.get("identity_kind", "git")
        monkeypatch.setattr(identity, "_source", old_source)
    elif fault == "identity_no_compact":
        original = identity._source
        def no_compact(value):
            fields = original(value)
            return (*fields[:3], None) if isinstance(value, dict) and "identity_kind" not in value else fields
        monkeypatch.setattr(identity, "_source", no_compact)
    local = source(HASH_B, SOURCE_TIME + 10)
    for commit in (HASH_A, HASH_B):
        peer = peer_metadata(commit=commit)
        del peer["identity_kind"]
        assert identity.compare_install_identity(local, peer) == "unknown", "full peer missing kind was assumed git"
        (warning,) = identity.comparison_warnings(local, peer)
        assert warning.code == "identity_unknown" and "unrecognized identity metadata" in warning.message
        for version, expected in (("1.0.0", "local_newer"), ("1.0.2", "peer_newer")):
            unequal = {**peer, "package_version": version}
            assert identity.compare_install_identity(local, unequal) == expected
            assert identity.comparison_warnings(local, unequal) == (warning,)
    installed = identity.InstalledIdentity("1.0.1", HASH_B, SOURCE_TIME + 10, "git", "win32", "host", "b" * 32)
    compact = installed.source_payload()
    assert set(compact) == {"package_version", "commit", "commit_time"}
    assert identity.compare_install_identity(installed, compact) == "equal", "compact control target lost implicit git"
    assert identity.compare_install_identity(compact, {**compact, "commit": HASH_A, "commit_time": SOURCE_TIME}) == "local_newer"
    for kind in ("git", "future_kind", None, []):
        explicit = peer_metadata(identity_kind=kind)
        assert identity._source(explicit)[3] is kind


def load_fixture(monkeypatch, direct=None, stamp=None):
    monkeypatch.setattr(identity, "_read_direct_url", lambda: direct)
    monkeypatch.setattr(identity, "_read_embedded_stamp", lambda: stamp)
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "  Host-NAME  ")
    return identity._load_installed_identity()


def embedded(commit=HASH_A, stamp_time=SOURCE_TIME, kind="git", schema=1):
    return {"schema": schema, "commit": commit, "commit_time": stamp_time, "kind": kind}


def test_editable_overrides_stale_embedded_commit_without_reading_it(monkeypatch):
    monkeypatch.setattr(identity, "_read_direct_url", lambda: {"dir_info": {"editable": True}})
    def forbidden():
        raise AssertionError("editable must not load a stale packaged stamp")
    monkeypatch.setattr(identity, "_read_embedded_stamp", forbidden)
    item = identity._load_installed_identity()
    assert (item.commit, item.commit_time, item.identity_kind) == (None, None, "editable")
    assert item.warnings and item.package_version == "1.0.1"


def test_embedded_identity_wins_installer_disagreement(monkeypatch):
    item = load_fixture(monkeypatch, {"url": "https://credential-must-not-escape.invalid", "vcs_info": {"commit_id": HASH_B}}, embedded())
    assert (item.commit, item.commit_time, item.identity_kind) == (HASH_A, SOURCE_TIME, "git")
    assert [w.code for w in item.warnings] == ["identity_metadata_mismatch"]
    assert "credential-must-not-escape" not in repr(item)
    assert item.hostname == "host-name"
    assert len(item.instance_id) == 32
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.commit = HASH_B


@pytest.mark.parametrize(("direct", "stamp", "commit", "stamp_time", "kind"), [
    (None, None, None, None, "unknown"),
    ({"vcs_info": {"commit_id": HASH_A}}, None, HASH_A, None, "direct_url"),
    ({"vcs_info": {"commit_id": "bad"}}, None, None, None, "unknown"),
    (None, embedded(kind=[]), None, None, "unknown"),
    (None, embedded(schema=2), None, None, "unknown"),
    (None, embedded(stamp_time=-1), None, None, "unknown"),
    (None, embedded(stamp_time=True), None, None, "unknown"),
    (None, embedded(commit=None, stamp_time=None, kind="dirty"), None, None, "dirty"),
    (None, embedded(commit=None), None, None, "unknown"),
])
def test_missing_malformed_sourceless_and_direct_url_identity_warns_not_fails(monkeypatch, direct, stamp, commit, stamp_time, kind):
    item = load_fixture(monkeypatch, direct, stamp)
    assert (item.commit, item.commit_time, item.identity_kind) == (commit, stamp_time, kind)
    assert item.warnings


def test_additive_metadata_warns_and_keeps_known_source(monkeypatch):
    item = load_fixture(monkeypatch, {"future_field": {"unknown": True}}, embedded())
    assert item.commit == HASH_A
    assert item.warnings[0].code == "identity_unknown"


def test_metadata_read_failure_is_warning_not_startup_failure(monkeypatch):
    def broken():
        raise OSError("fixture metadata read failed")
    monkeypatch.setattr(identity, "_read_direct_url", broken)
    monkeypatch.setattr(identity, "_read_embedded_stamp", broken)
    item = identity.installed_identity()
    assert (item.commit, item.commit_time, item.identity_kind) == (None, None, "unknown")
    assert item.warnings


def test_snapshot_is_once_even_across_threads_no_runtime_git_or_server_import(monkeypatch):
    calls = []
    def direct():
        calls.append("direct")
        return None
    def stamp():
        calls.append("stamp")
        return embedded()
    monkeypatch.setattr(identity, "_read_direct_url", direct)
    monkeypatch.setattr(identity, "_read_embedded_stamp", stamp)
    def forbidden(*args, **kwargs):
        raise AssertionError("runtime identity must not execute a subprocess")
    monkeypatch.setattr(subprocess, "run", forbidden)
    results = []
    threads = [threading.Thread(target=lambda: results.append(identity.installed_identity())) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()
    assert len(results) == 20 and len({id(item) for item in results}) == 1
    assert calls == ["direct", "stamp"]
    assert identity.installed_identity() is results[0]
    source_text = (REPO / "src/workflowy_mcp/daemon_identity.py").read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    assert not any(isinstance(n, ast.ImportFrom) and n.module in {"server", "workflowy_mcp.server"} for n in ast.walk(tree))


def peer_metadata(**changes):
    return {
        "protocol": 1, **source(), "platform": "win32", "hostname": "host",
        "instance_id": "c" * 32, "phase": "running", "drain_timeout_s": 3600,
        "restart_path": "/mcp/_nexus/restart", "restart_id": None, **changes,
    }


@pytest.mark.parametrize(("platform", "host", "wsl", "changes", "expected"), [
    ("win32", "HOST", False, {}, True),
    ("win32", "HOST", False, {"platform": "win32"}, True),
    ("linux", "host", True, {}, False),
    ("linux", "host", True, {"platform": "linux"}, False),
    ("linux", "host", False, {"platform": "linux"}, True),
    ("darwin", "host", False, {"platform": "darwin"}, True),
    ("win32", "different", False, {}, False),
    ("win32", "", False, {}, False),
    ("win32", "host", False, {"hostname": None}, False),
    ("win32", "host", False, {"protocol": 2}, False),
    ("win32", "host", False, {"protocol": True}, False),
    ("win32", "host", False, {"restart_path": "//other/_nexus/restart"}, False),
    ("win32", "host", False, {"drain_timeout_s": float("nan")}, False),
    ("win32", "host", False, {"drain_timeout_s": 10**1000}, False),
    ("win32", "host", False, {"instance_id": "invalid"}, False),
    ("win32", "host", False, {"phase": []}, False),
    ("win32", "host", False, {"phase": "future-phase"}, False),
    ("win32", "host", False, {"restart_id": []}, False),
])
def test_replacement_capability_is_affinity_not_inherited_wsl_environment(monkeypatch, platform, host, wsl, changes, expected):
    monkeypatch.setenv("WSL_DISTRO_NAME", "fixture-wsl-inherited-even-by-windows")
    monkeypatch.setenv("WSL_INTEROP", "fixture")
    assert identity.can_replace_daemon(platform, host, wsl, peer_metadata(**changes)) is expected


def _command(args, root, env=None):
    return subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, timeout=SETUP_WAIT, check=True)


def _build_fixture(tmp_path):
    root = tmp_path / "source"
    package = root / "src" / "workflowy_mcp"
    package.mkdir(parents=True)
    for name in ("__init__.py", "daemon_identity.py"):
        shutil.copy2(REPO / "src" / "workflowy_mcp" / name, package / name)
    shutil.copy2(REPO / "hatch_build.py", root / "hatch_build.py")
    mutation = os.environ.get("N06_TEST_MUTATION", "")
    hook_path = root / "hatch_build.py"
    if mutation == "omit_stamp":
        hook_path.write_text(hook_path.read_text(encoding="utf-8").replace(
            'build_data.setdefault("force_include", {})[str(generated)] = destination',
            'pass  # fixture-only forbidden omission',
        ), encoding="utf-8")
    elif mutation == "stamp_build_time":
        hook_path.write_text(hook_path.read_text(encoding="utf-8").replace(
            'stamp_time = int(git("show", "-s", "--format=%ct", "HEAD"))',
            'stamp_time = 1900000000  # fixture-only forbidden build time',
        ), encoding="utf-8")
    (root / "pyproject.toml").write_text('''[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[project]
name = "workflowy-nexus-mcp-tools"
version = "1.0.1"
[tool.hatch.build.targets.wheel]
packages = ["src/workflowy_mcp"]
[tool.hatch.build.targets.sdist]
include = ["src/workflowy_mcp/**/*.py", "hatch_build.py"]
[tool.hatch.build.hooks.custom]
path = "hatch_build.py"
''', encoding="utf-8")
    _command(["git", "init", "-q"], root)
    _command(["git", "config", "user.name", "N06 fixture"], root)
    _command(["git", "config", "user.email", "n06@invalid.example"], root)
    _command(["git", "add", "src", "pyproject.toml", "hatch_build.py"], root)
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{SOURCE_TIME} +0000", "GIT_COMMITTER_DATE": f"@{SOURCE_TIME} +0000"}
    _command(["git", "commit", "-qm", "fixture source"], root, env)
    commit = _command(["git", "rev-parse", "HEAD"], root).stdout.strip()
    return root, commit


def _stamp_text(text):
    values = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def _build(root, target, destination):
    destination.mkdir(parents=True, exist_ok=True)
    code = "from hatchling.build import build_wheel, build_sdist; import sys; print((build_wheel if sys.argv[1]=='wheel' else build_sdist)(sys.argv[2]))"
    result = _command([sys.executable, "-Werror", "-c", code, target, str(destination)], root)
    return destination / result.stdout.strip().splitlines()[-1]


def test_actual_hook_wheel_sdist_and_no_git_rebuild_preserve_source_identity(tmp_path):
    root, commit = _build_fixture(tmp_path)
    expected = {"SOURCE_COMMIT": commit, "SOURCE_COMMIT_TIME": SOURCE_TIME, "SOURCE_IDENTITY_KIND": "git", "SOURCE_IDENTITY_SCHEMA": 1}
    wheel = _build(root, "wheel", tmp_path / "wheel")
    sdist = _build(root, "sdist", tmp_path / "sdist")
    with zipfile.ZipFile(wheel) as archive:
        assert _stamp_text(archive.read("workflowy_mcp/_build_identity.py").decode()) == expected
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        stamp_name = next(n for n in names if n.endswith("/src/workflowy_mcp/_build_identity.py"))
        assert _stamp_text(archive.extractfile(stamp_name).read().decode()) == expected
        assert any(n.endswith("/hatch_build.py") for n in names)
        unpacked = tmp_path / "unpacked"
        unpacked.mkdir()
        # The archive is our own fixture, never an untrusted download.
        if sys.version_info >= (3, 12):
            archive.extractall(unpacked, filter="data")
        else:
            archive.extractall(unpacked)
    extracted = next(unpacked.iterdir())
    assert not (extracted / ".git").exists()
    rebuilt = _build(extracted, "wheel", tmp_path / "rebuilt")
    with zipfile.ZipFile(rebuilt) as archive:
        assert _stamp_text(archive.read("workflowy_mcp/_build_identity.py").decode()) == expected
        installed = tmp_path / "installed"
        archive.extractall(installed)
    env = {**os.environ, "PYTHONPATH": str(installed)}
    code = "from workflowy_mcp.daemon_identity import installed_identity; import json,sys; i=installed_identity(); print(json.dumps([i.commit,i.commit_time,i.identity_kind,'workflowy_mcp.server' in sys.modules]))"
    result = _command([sys.executable, "-c", code], tmp_path, env)
    assert json.loads(result.stdout) == [commit, SOURCE_TIME, "git", False]
    assert not (root / "src/workflowy_mcp/_build_identity.py").exists()
    assert _command(["git", "status", "--porcelain", "--untracked-files=no"], root).stdout == ""


def test_actual_hook_dirty_and_sourceless_builds_are_honest_and_do_not_fail(tmp_path):
    root, _ = _build_fixture(tmp_path)
    (root / "src/workflowy_mcp/__init__.py").write_text('__version__ = "1.0.1"\n# fixture dirty\n', encoding="utf-8")
    wheel = _build(root, "wheel", tmp_path / "dirty")
    with zipfile.ZipFile(wheel) as archive:
        stamp = _stamp_text(archive.read("workflowy_mcp/_build_identity.py").decode())
    assert (stamp["SOURCE_COMMIT"], stamp["SOURCE_COMMIT_TIME"], stamp["SOURCE_IDENTITY_KIND"]) == (None, None, "dirty")
    shutil.rmtree(root / ".git")
    wheel = _build(root, "wheel", tmp_path / "sourceless")
    with zipfile.ZipFile(wheel) as archive:
        stamp = _stamp_text(archive.read("workflowy_mcp/_build_identity.py").decode())
    assert (stamp["SOURCE_COMMIT"], stamp["SOURCE_COMMIT_TIME"], stamp["SOURCE_IDENTITY_KIND"]) == (None, None, "unknown")


def _hook_module():
    spec = importlib.util.spec_from_file_location("n06_actual_hatch_hook", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_git_commands_have_explicit_90_second_bound_and_errors_warn(tmp_path, monkeypatch, capsys):
    hook = _hook_module()
    calls = []
    def failed_run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, 90)
    monkeypatch.setattr(hook.subprocess, "run", failed_run)
    stamp = hook._source_stamp(tmp_path)
    assert stamp["SOURCE_COMMIT"] is None and stamp["SOURCE_COMMIT_TIME"] is None
    assert stamp["SOURCE_IDENTITY_KIND"] == "unknown"
    assert calls and all(kwargs["timeout"] == 90 for _, kwargs in calls)
    assert "WARNING:" in capsys.readouterr().err


def test_hook_staging_and_finalization_failure_cannot_fail_build(monkeypatch, capsys, tmp_path):
    hook = _hook_module()
    monkeypatch.setattr(hook, "_source_stamp", lambda root: {"SOURCE_COMMIT": None})
    def unavailable(*args, **kwargs):
        raise OSError("fixture temp failure")
    monkeypatch.setattr(hook.tempfile, "TemporaryDirectory", unavailable)
    instance = hook.CustomBuildHook(str(tmp_path), {}, None, None, str(tmp_path), "wheel")
    data = {}
    instance.initialize("standard", data)
    assert data == {}
    instance.finalize("standard", data, "fixture.whl")
    assert "WARNING:" in capsys.readouterr().err


def test_actual_build_survives_hook_error_with_no_generated_identity(tmp_path):
    root, _ = _build_fixture(tmp_path)
    hook = root / "hatch_build.py"
    hook.write_text(hook.read_text(encoding="utf-8").replace(
        'values = _source_stamp(Path(self.root))',
        'raise OSError("fixture-only hook failure")',
    ), encoding="utf-8")
    wheel = _build(root, "wheel", tmp_path / "hook-error")
    with zipfile.ZipFile(wheel) as archive:
        assert "workflowy_mcp/daemon_identity.py" in archive.namelist()
        assert "workflowy_mcp/_build_identity.py" not in archive.namelist()
    assert not (root / "src/workflowy_mcp/_build_identity.py").exists()


def test_hook_editable_build_does_not_stage_an_old_commit(tmp_path):
    hook = _hook_module()
    instance = hook.CustomBuildHook(str(tmp_path), {}, None, None, str(tmp_path), "wheel")
    data = {}
    instance.initialize("editable", data)
    assert data == {} and instance._identity_temp is None
