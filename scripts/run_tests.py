#!/usr/bin/env python3
"""Fast, receipt-verifiable runner for the workflowy-nexus-mcp-tools test suite.

Why this exists
---------------
Running from an ext4 clone avoids the Windows / DrvFs filesystem penalty and
keeps each worktree's imports isolated. The suite has grown well beyond the
original ~30 s xdist run: full-tier performance witnesses still run serially,
so targeted --files checks are the normal development loop.

What it does
------------
  * WSL  -> rsync the working tree to an ext4 clone, run pytest there (`-n 8`).
  * Windows -> hand the run to WSL by default (`--via-wsl`), so it gets the same
    ext4 clone path (~3x faster than in place on NTFS); `--no-via-wsl` (or
    `--in-place`) runs in place with the Windows interpreter, which is the only
    way to produce WINDOWS-environment evidence.
  * `--in-place` forces in-place on WSL (the DrvFs path) for comparison.
  * `--files` partitions its targets by extension: `.py` (and anything else
    non-`.js`) through pytest as before, each `.js` through `node <harness>.js`
    with its exit code captured. Both fold into the SAME receipt — JS results
    are exit-code accounting (0 = pass, nonzero = fail and named in
    `failed_tests`), never pytest summary parsing.
It writes a commit+tree-pinned `tests/.last_run.json` receipt after every run
(red or green). `--verify` checks that receipt against HEAD + the working tree
in about a second, so a green suite is verified by receipt, never by rerun.

`--verify` REFUSES a receipt written from a dirty tree (`dirty: true`) unless
`--allow-dirty` is given: a PASS therefore means exactly one thing — the
committed tree at HEAD is green. A dirty receipt is a mid-work artifact ("these
uncommitted bytes are green") and is never a claim about a commit; `--allow-dirty`
asks that narrower question explicitly. Because `tree_hash` is a real git tree
object (temporary index + `git write-tree`), committing the same bytes leaves it
unchanged: `git rev-parse <commit>^{tree}` == the receipt's `tree_hash` is the
bridge from a receipt to a commit.

The receipt is signed with a SHA-256 over its own contents, so hand-editing the
counts to look green is detected even though the receipt is gitignored.

Every run is also archived into `tests/.runs/` — newest `--keep N` kept (default
10), older ones unlinked, nothing ever rewritten — with the full pytest output
alongside in a matching `.log`. `tests/.last_run.json` stays as a mirror of the
newest, and a red run records `failed_tests:` so it can say what failed without
anyone re-reading a pipe.

Every pytest pass also records all rounded setup/call/teardown durations in the
log and in `test_durations_s` / `file_durations_s`, grouped by pass. File totals
are inclusive worker-seconds, NOT the parallel pass's elapsed wall time; the
existing `duration_parallel_s` / `duration_tail_s` fields measure that. The five
slowest files in each pass are printed before the unchanged final receipt line.

The ext4 clone mirrors the repo by construction: its file list comes from
`git ls-files -co --exclude-standard` — the same set the receipt's `tree_hash`
covers — and a prune removes anything the repo no longer has, so the clone
cannot drift from the hashed tree. The clone is flock-guarded (`<clone>/.lock`)
so two concurrent runs never share it, and a clone path that is the repo itself
or is not under `~/.cache` is refused (exit 2) rather than trusting `--delete`.

Stdlib only; runnable by both the Windows and WSL venvs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # POSIX only; the ext4 clone path exists only on WSL
except ImportError:  # pragma: no cover - Windows
    fcntl = None

SCHEMA = 1
# Every artifact a run writes INTO the repo must be gitignored (see .gitignore):
# `tree_hash` covers tracked + untracked-unignored files, so an unignored
# artifact would change the very tree hash the receipt has just pinned, and
# `--verify` would refuse its own run. New run outputs go on this list AND in
# .gitignore, together.
RECEIPT_REL = Path("tests") / ".last_run.json"
RUNS_DIR_REL = Path("tests") / ".runs"       # rolling window of the newest N runs
LOG_REL = Path("tests") / ".last_run.log"    # full pytest output of the newest run
DEFAULT_N = 8
DEFAULT_KEEP = 10
DEFAULT_DIST = "loadfile"
# `-p no:cacheprovider` keeps `.pytest_cache` out of the repo; `-q` keeps the
# progress compact. Keep every phase duration in the archived log so receipts
# can name slow tests/files without another run (pytest rounds these to 0.01s).
BASE_PYTEST_ARGS = [
    "-p", "no:cacheprovider", "-q", "--color=no", "--durations=0", "--durations-min=0",
]
# Marker registered in pyproject.toml [tool.pytest.ini_options].
TIMING_MARKER = "timing"
# Fields excluded from the receipt's own integrity digest.
_DIGEST_EXCLUDED = ("receipt_sha256",)
# Clone-local infrastructure that is never repository content and is preserved
# across the sync/prune: the ext4 clone's own venv, git's dir (if a clone ever
# has one), and the run lock. `uv.lock` is re-seeded from the repo after each
# sync instead (it is gitignored, so outside the git file set).
CLONE_LOCAL_DIRS = {".venv", ".git"}
CLONE_LOCAL_FILES = {".lock"}


# ---------------------------------------------------------------------------
# Repo / git helpers
# ---------------------------------------------------------------------------
def repo_root() -> Path:
    """The repository root (this script lives in <repo>/scripts/)."""
    return Path(__file__).resolve().parent.parent


def compute_commit(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def head_tree_hash(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def compute_tree_hash(repo: Path) -> str:
    """Hash the working tree EXACTLY as it will be synced and run.

    A temporary index (`GIT_INDEX_FILE`) gets `git add -A` so untracked-but-not-
    ignored files count and ignored files (`.gitignore`) do not, then
    `git write-tree` emits the tree hash.
    """
    fd, tmp = tempfile.mkstemp(prefix="wnmt-index-", suffix=".idx")
    os.close(fd)
    os.unlink(tmp)  # git must create it fresh
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = tmp
    try:
        add = subprocess.run(
            ["git", "add", "-A"], cwd=str(repo),
            capture_output=True, text=True, env=env,
        )
        if add.returncode != 0:
            raise RuntimeError(f"git add -A failed: {add.stderr.strip()}")
        out = subprocess.run(
            ["git", "write-tree"], cwd=str(repo),
            capture_output=True, text=True, env=env, check=True,
        )
        return out.stdout.strip()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Receipt: schema, signing, read/write
# ---------------------------------------------------------------------------
def receipt_digest(receipt: dict) -> str:
    payload = {k: v for k, v in receipt.items() if k not in _DIGEST_EXCLUDED}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def sign_receipt(receipt: dict) -> dict:
    signed = dict(receipt)
    signed["receipt_sha256"] = receipt_digest(signed)
    return signed


def write_receipt(repo: Path, receipt: dict) -> Path:
    path = repo / RECEIPT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sign_receipt(receipt), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_receipt(repo: Path) -> dict | None:
    path = repo / RECEIPT_REL
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def runs_dir(repo: Path) -> Path:
    return repo / RUNS_DIR_REL


def receipt_name(receipt: dict) -> str:
    """`<utc>-<commit8>-<tier>` — lexical order is chronological order."""
    try:
        dt = datetime.fromisoformat(str(receipt.get("timestamp_utc") or ""))
    except ValueError:
        dt = datetime.now(timezone.utc)
    stamp = dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    commit = str(receipt.get("commit") or "nocommit")[:8]
    tier = str(receipt.get("tier") or "unknown")
    return f"{stamp}-{commit}-{tier}"


def archive_run(
    repo: Path, receipt: dict, log_text: str = "", keep: int = DEFAULT_KEEP
) -> Path:
    """Write this run into the rolling window and refresh `.last_run.json`.

    The window is what makes a receipt citable later: a `--files` mutation check
    run after the final full run adds a file instead of destroying the one a
    conductor needs. Pruning is by name (== by time), newest N kept; nothing is
    ever rewritten in place.
    """
    window = runs_dir(repo)
    window.mkdir(parents=True, exist_ok=True)
    stem = receipt_name(receipt)
    json_path = window / f"{stem}.json"
    json_path.write_text(
        json.dumps(sign_receipt(receipt), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if log_text:
        (window / f"{stem}.log").write_text(log_text, encoding="utf-8")
    kept = 0
    for old in sorted(window.glob("*.json"), key=lambda p: p.name, reverse=True):
        kept += 1
        if kept > keep:
            old.unlink()
            sibling = old.with_suffix(".log")
            if sibling.exists():
                sibling.unlink()
    write_receipt(repo, receipt)
    return json_path


def receipt_candidates(repo: Path) -> list[tuple[str, dict]]:
    """Every receipt worth checking, newest first: the window, then the mirror."""
    found: list[tuple[str, dict]] = []
    window = runs_dir(repo)
    if window.is_dir():
        for path in sorted(window.glob("*.json"), key=lambda p: p.name, reverse=True):
            try:
                found.append(
                    (path.relative_to(repo).as_posix(),
                     json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, ValueError):
                continue
    mirror = repo / RECEIPT_REL
    if mirror.is_file():
        try:
            found.append(
                (mirror.relative_to(repo).as_posix(),
                 json.loads(mirror.read_text(encoding="utf-8")))
            )
        except (OSError, ValueError):
            pass
    return found


# ---------------------------------------------------------------------------
# --verify
# ---------------------------------------------------------------------------
def _check_receipt(
    receipt: dict, now_commit: str, now_tree: str,
    required_tier: str, allow_dirty: bool,
) -> tuple[bool, str]:
    """One receipt against the live commit/tree/tier. Unchanged checks."""
    if receipt.get("schema") != SCHEMA:
        return False, f"unsupported receipt schema {receipt.get('schema')!r}"
    stored = receipt.get("receipt_sha256")
    if stored != receipt_digest(receipt):
        return False, "receipt integrity check failed — receipt contents were edited"
    if receipt.get("commit") != now_commit:
        return False, (
            f"commit mismatch — receipt {receipt.get('commit')}, now {now_commit}"
        )
    if receipt.get("tree_hash") != now_tree:
        return False, (
            f"tree mismatch — receipt {receipt.get('tree_hash')}, now {now_tree} "
            "(a tracked/untracked-unignored file changed since the run)"
        )
    if receipt.get("dirty") and not allow_dirty:
        return False, (
            f"dirty receipt — the run at {str(now_commit)[:8]} tested an UNCOMMITTED tree "
            f"(tree {str(receipt.get('tree_hash'))[:8]} != HEAD^{{tree}}); this is not a claim "
            "about the commit. Commit and rerun, or pass --allow-dirty to ask only "
            "whether these uncommitted bytes are green"
        )
    if receipt.get("tier") != required_tier:
        return False, (
            f"tier mismatch — receipt {receipt.get('tier')!r}, required {required_tier!r}"
        )
    if receipt.get("failed") or receipt.get("errors"):
        return False, (
            f"red receipt — failed={receipt.get('failed')}, errors={receipt.get('errors')}"
            + (
                f" · {', '.join(receipt['failed_tests'][:5])}"
                if receipt.get("failed_tests") else ""
            )
        )
    if receipt.get("pytest_exit") != 0:
        return False, f"pytest_exit={receipt.get('pytest_exit')}"
    state = "DIRTY tree (uncommitted bytes; not a claim about the commit)" if receipt.get("dirty") else "clean tree"
    return True, (
        f"green {receipt.get('env')} run at {str(now_commit)[:8]} "
        f"tree {str(now_tree)[:8]} | {state} "
        f"(tier {receipt.get('tier')}, "
        f"{receipt.get('passed')} passed / {receipt.get('skipped')} skipped)"
    )


def verify(
    repo: Path, required_tier: str = "full", allow_dirty: bool = False
) -> tuple[bool, str]:
    """Check the newest QUALIFYING receipt against HEAD + the working tree.

    A PASS means some receipt covers exactly the bytes in front of you AND (by
    default) those bytes are the committed tree at HEAD. Candidates are scanned
    newest-first across `tests/.runs/` and `tests/.last_run.json`, so a later
    `--files` or fast-tier run cannot invalidate an earlier citable full-tier
    receipt. A receipt written from a dirty tree is refused unless `allow_dirty`
    is set — it attests uncommitted bytes, never a commit.
    """
    candidates = receipt_candidates(repo)
    if not candidates:
        return False, (
            "no receipt at tests/.runs/ or tests/.last_run.json "
            "(run scripts/run_tests.py first)"
        )
    try:
        now_commit = compute_commit(repo)
        now_tree = compute_tree_hash(repo)
    except Exception as exc:  # pragma: no cover - environmental
        return False, f"cannot recompute commit/tree: {exc}"
    newest_reason = ""
    for relpath, receipt in candidates:
        ok, reason = _check_receipt(
            receipt, now_commit, now_tree, required_tier, allow_dirty
        )
        if ok:
            return True, f"{reason} · receipt {relpath}"
        if not newest_reason:
            newest_reason = reason
    count = len(candidates)
    return False, (
        f"{newest_reason} · newest of {count} receipt{'' if count == 1 else 's'} "
        f"checked, none qualifying (commit + tree must match, "
        f"{'clean tree, ' if not allow_dirty else ''}tier {required_tier!r}, "
        "zero failures)"
    )


# ---------------------------------------------------------------------------
# Environment detection and clone preparation
# ---------------------------------------------------------------------------
def detect_host(repo: Path) -> str:
    """'windows' | 'wsl' | 'other'."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            text = ""
        if "microsoft" in text or str(repo).startswith("/mnt/"):
            return "wsl"
    return "other"


class ClonePathError(Exception):
    """A clone path that would make the sync/prune unsafe."""


def clone_dir_for(repo: Path) -> Path:
    return Path.home() / ".cache" / "wnmt-tests" / repo.name


def resolve_clone_dir(repo: Path, requested: str | None = None) -> Path:
    """Resolve and validate the ext4 clone path.

    The prune is safe by check, not by luck: refuse the repository itself (or a
    path inside/around it), and refuse anything that is not under `~/.cache`.
    """
    raw = Path(requested).expanduser() if requested else clone_dir_for(repo)
    try:
        resolved = raw.resolve()
        repo_resolved = repo.resolve()
        cache_resolved = (Path.home() / ".cache").resolve()
    except OSError as exc:  # pragma: no cover - environmental
        raise ClonePathError(f"cannot resolve clone path {raw}: {exc}") from exc
    if (
        resolved == repo_resolved
        or repo_resolved in resolved.parents
        or resolved in repo_resolved.parents
    ):
        raise ClonePathError(
            f"clone path {resolved} is the repository itself ({repo_resolved})"
        )
    if resolved != cache_resolved and cache_resolved not in resolved.parents:
        raise ClonePathError(
            f"clone path {resolved} is not under {cache_resolved}"
        )
    return resolved


def git_file_list(repo: Path) -> list[str]:
    """The content set the clone must mirror: tracked + untracked-not-ignored.

    This is exactly the set `compute_tree_hash` is computed over, so syncing it
    makes the clone equal the hashed tree by construction — the audit's
    "clone != tree" check becomes impossible rather than merely unobserved.
    """
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=str(repo), capture_output=True, check=True,
    )
    text = out.stdout.decode("utf-8", "surrogateescape")
    return [name for name in text.split("\0") if name]


def sync_to_clone(repo: Path, clone: Path) -> None:
    """Make the ext4 clone's content identical to the git file set.

    `--files-from` sends exactly the listed files, so a gitignored extra cannot
    be dragged in. Naming individual files means rsync's own `--delete` cannot
    remove files the repo has since renamed or deleted, so `prune_clone` closes
    that gap explicitly. `uv.lock` (gitignored, outside the set) is re-seeded
    afterwards so the clone venv resolves reproducibly.
    """
    clone.parent.mkdir(parents=True, exist_ok=True)
    files = git_file_list(repo)
    cmd = [
        "rsync", "-a", "--from0", "--files-from=-",
        str(repo) + "/", str(clone) + "/",
    ]
    subprocess.run(
        cmd, input="\0".join(files) + "\0", cwd=str(repo), text=True, check=True,
    )
    prune_clone(clone, files)
    seed_lockfile(repo, clone)


def prune_clone(clone: Path, keep: list[str]) -> None:
    """Delete clone files that are not in the git file set.

    `--files-from` names individual files, and rsync only deletes extraneous
    entries from directories it was asked to synchronize wholesale — so its
    `--delete` is a no-op here. This walk is the difference between "the clone
    was correct once" and "the clone is correct by construction".
    """
    keep_set = set(keep)
    for dirpath, dirnames, filenames in os.walk(clone, topdown=True):
        rel = Path(dirpath).relative_to(clone)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d not in CLONE_LOCAL_DIRS]
        for name in filenames:
            if rel == Path(".") and name in CLONE_LOCAL_FILES:
                continue
            relname = name if rel == Path(".") else (rel / name).as_posix()
            if relname not in keep_set:
                (Path(dirpath) / name).unlink()
    for dirpath, _dirnames, _filenames in os.walk(clone, topdown=False):
        rel = Path(dirpath).relative_to(clone)
        if rel == Path(".") or rel.parts[0] in CLONE_LOCAL_DIRS:
            continue
        try:
            os.rmdir(dirpath)  # only succeeds once the prune has emptied it
        except OSError:
            pass


def seed_lockfile(repo: Path, clone: Path) -> None:
    """Copy the repo's `uv.lock` beside the clone venv.

    The lockfile is gitignored, so it is not in the git file set; without it
    `uv sync` re-resolves and can install different dependency versions than the
    repo pins. Seeding it keeps the clone's environment reproducible.
    """
    src = repo / "uv.lock"
    if src.is_file():
        shutil.copy2(src, clone / "uv.lock")


def acquire_clone_lock(clone: Path):
    """Take an exclusive flock on `<clone>/.lock` for the whole run.

    A second concurrent caller blocks here and says so on stderr, so two runs
    on the same repository can never share one clone mid-run. No-op on Windows
    (there is no clone there) and wherever `fcntl` is unavailable.
    """
    if fcntl is None:  # pragma: no cover - Windows
        return None
    clone.mkdir(parents=True, exist_ok=True)
    lock_path = clone / ".lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"==> another run_tests.py holds {lock_path}; waiting for it …",
            file=sys.stderr, flush=True,
        )
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def ensure_clone_venv(clone: Path) -> Path:
    """Ensure the clone has a venv; return its python. `uv sync` is cheap when
    unchanged. `UV_PROJECT_ENVIRONMENT` is pinned so a globally exported value
    (the WSL `.venv-wsl` fix) cannot redirect the clone's venv."""
    py = clone / ".venv" / "bin" / "python"
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = ".venv"
    subprocess.run(
        ["uv", "sync", "--extra", "dev"],
        cwd=str(clone), env=env, check=True,
    )
    if not py.exists():
        raise RuntimeError(f"uv sync did not produce {py}")
    return py


def py_version(py: Path | str) -> str:
    try:
        out = subprocess.run(
            [str(py), "-c", "import platform;print(platform.python_version())"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:  # pragma: no cover - environmental
        return platform.python_version()


# ---------------------------------------------------------------------------
# Windows -> WSL routing
# ---------------------------------------------------------------------------
_WIN_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def wsl_repo_path(repo: Path) -> str | None:
    """`E:\\a\\b` -> `/mnt/e/a/b`. None for anything WSL cannot reach."""
    match = _WIN_PATH_RE.match(str(repo))
    if not match:
        return None
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/").rstrip("/")
    return f"/mnt/{drive}/{rest}"


def wsl_available() -> bool:
    """A cheap probe, so an unreachable WSL falls back instead of failing."""
    try:
        probe = subprocess.run(
            ["wsl.exe", "-e", "true"], capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def compose_wsl_command(wsl_repo: str, argv: list[str]) -> str:
    """The bash line the inner WSL call runs. Pure; tested.

    Built from a LIST, never from a `{}` template: `str.format` silently drops
    surplus arguments, and this exact line once lost every passthrough flag that
    way (Session-023). A list has no placeholders to fall out of step with.
    Windows-shaped paths (`--files tests\\x.py`) are normalised, because bash
    would eat the backslashes and pytest would look for a literal `tests\\x.py`.
    """
    parts = ["cd", shlex.quote(wsl_repo), "&&", "python3", "scripts/run_tests.py"]
    parts.extend(shlex.quote(arg.replace("\\", "/")) for arg in argv)
    return " ".join(parts)


def run_via_wsl(repo: Path, argv: list[str]) -> int | None:
    """Run this script inside WSL so the run gets the ext4 clone path.

    Returns the inner exit code, or None when WSL cannot be used (the caller
    then runs in place). The inner call detects `wsl` and takes the clone path,
    so this never recurses. `--verify` never comes here: it recomputes a git
    tree hash, which is OS-independent, and answers in about a second anyway.
    """
    wsl_repo = wsl_repo_path(repo)
    if wsl_repo is None:
        print(
            "==> --via-wsl: this repo path cannot be mapped into WSL; running in place",
            file=sys.stderr, flush=True,
        )
        return None
    if not wsl_available():
        print(
            "==> --via-wsl: wsl.exe is unavailable; running in place on NTFS "
            "(~3x slower, and this is the WINDOWS-environment path)",
            file=sys.stderr, flush=True,
        )
        return None
    inner = compose_wsl_command(wsl_repo, argv)
    cmd = ["wsl.exe", "-e", "bash", "-lc", inner]
    print(f"==> via WSL (ext4 clone path): {inner}", flush=True)
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------------------
# Running + parsing pytest
# ---------------------------------------------------------------------------
_SUMMARY_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)\b"
)


def parse_counts(output: str) -> dict:
    """Parse pytest's final one-line summary (`-q` prints it plainly)."""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "xfailed": 0}
    summary = None
    for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
        if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line) or "no tests ran" in line:
            summary = line
            break
    if summary is None:
        return counts
    for number, word in _SUMMARY_RE.findall(summary):
        key = "errors" if word in ("error", "errors") else word
        if key in counts:
            counts[key] = int(number)
    return counts


_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")


def parse_failed_tests(output: str) -> list[str]:
    """Node ids from pytest's `short test summary info` block, in order, deduped."""
    ids: list[str] = []
    for line in output.splitlines():
        match = _FAILED_LINE_RE.match(line.strip())
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


_DURATIONS_HEADER_RE = re.compile(r"^=+\s+slowest(?:\s+\d+)?\s+durations\s+=+$")
_DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)s\s+(?:setup|call|teardown)\s+(.+)$")


# @beacon[
#   id=wnmt-tests@parse_durations,
#   slice_labels=mci-harness,
#   kind=ast,
#   comment=Aggregates pytest's final duration table by test and file for citable per-pass hot lists; earlier captured tables are ignored.,
# ]
def parse_durations(output: str) -> tuple[dict[str, float], dict[str, float]]:
    """Summed setup/call/teardown wall seconds, by test and file.

    These are pytest's 0.01s-rounded phase measurements, not the pass's wall
    time: concurrent workers' durations add together. Only read the LAST
    terminal duration table, never duration-shaped text in captured output.
    """
    lines = output.splitlines()
    headers = [i for i, line in enumerate(lines) if _DURATIONS_HEADER_RE.match(line.strip())]
    tests: dict[str, float] = {}
    files: dict[str, float] = {}
    if headers:
        for line in lines[headers[-1] + 1:]:
            match = _DURATION_RE.match(line.strip())
            if not match:
                break
            seconds, nodeid = float(match.group(1)), match.group(2)
            tests[nodeid] = tests.get(nodeid, 0.0) + seconds
            path = nodeid.split("::", 1)[0]
            files[path] = files.get(path, 0.0) + seconds
    return (
        {name: round(seconds, 2) for name, seconds in tests.items()},
        {name: round(seconds, 2) for name, seconds in files.items()},
    )


def format_run_log(
    env_name: str, tier: str, parallel_cmd: str | None, out_parallel: str,
    tail_cmd: str | None, out_tail: str,
    js_runs: list[tuple[str, str]] | None = None,
) -> str:
    """The full output of a run (pytest passes + JS harnesses), so a red
    receipt can be diagnosed. A pass that did not run (cmd None) is omitted —
    a `.js`-only `--files` run never starts pytest."""
    parts = [f"# run_tests.py · env={env_name} · tier={tier}"]
    if parallel_cmd:
        parts += [f"# pass 1 (parallel): {parallel_cmd}", out_parallel.rstrip("\n")]
    if tail_cmd:
        parts += [f"# pass 2 (serial timing tail): {tail_cmd}", out_tail.rstrip("\n")]
    for js_cmd, js_out in js_runs or []:
        parts += [f"# js harness: {js_cmd}", js_out.rstrip("\n")]
    return "\n".join(parts) + "\n"


def run_pytest(
    py: Path | str,
    args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, float]:
    cmd = [str(py), "-m", "pytest", *args]
    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)
    duration = time.monotonic() - start
    combined = proc.stdout + proc.stderr
    sys.stdout.write(combined)
    sys.stdout.flush()
    return proc.returncode, combined, duration


# ---------------------------------------------------------------------------
# Running the JS harnesses (`--files` only)
# ---------------------------------------------------------------------------
NODE_NOT_FOUND_EXIT = 127  # the shell convention for "command not found"


def partition_file_targets(files: list[str]) -> tuple[list[str], list[str]]:
    """Split `--files` targets into (pytest_targets, js_targets). Pure; tested.

    The harnesses under `tests/js/` are dependency-free node scripts, not
    pytest modules — pytest only collects `test_*.py`, so a `.js` path handed
    to pytest is a usage error and the JS harnesses get no mutation check
    through this wrapper ([ap:SFK1C9]). Everything that is not `.js` stays on
    the pytest side exactly as before — including pytest node ids such as
    `tests/test_x.py::test_a`, whose final-component suffix is not `.js`.
    """
    pytest_targets: list[str] = []
    js_targets: list[str] = []
    for item in files:
        if Path(item).suffix.lower() == ".js":
            js_targets.append(item)
        else:
            pytest_targets.append(item)
    return pytest_targets, js_targets


def run_js_harness(node_bin: str, js_path: str, cwd: Path) -> tuple[int, str, float]:
    """Run ONE JS harness under node; same contract as run_pytest.

    The harnesses' own convention (tests/js/README.md): exit 0 = all pass,
    nonzero = failure. Output is echoed live and returned for the run log.
    """
    cmd = [node_bin, js_path]
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    duration = time.monotonic() - start
    combined = proc.stdout + proc.stderr
    sys.stdout.write(combined)
    sys.stdout.flush()
    return proc.returncode, combined, duration


def fold_js_results(
    counts: dict, failed_tests: list[str], js_results: list[tuple[str, int]]
) -> int:
    """Fold JS harness (path, exit_code) results into the pytest-shaped fields.

    JS results are EXIT-CODE ACCOUNTING, not summary parsing — `parse_counts`
    is pytest-shaped and never sees node output. Each harness counts as one
    passed/failed unit, so the receipt's red/green logic (`failed` > 0 or
    `pytest_exit` != 0) covers JS unchanged, and every failing harness is
    NAMED in `failed_tests`. Returns the run's JS exit: the first nonzero
    harness code, else 0.
    """
    js_exit = 0
    for path, code in js_results:
        if code == 0:
            counts["passed"] += 1
        else:
            counts["failed"] += 1
            failed_tests.append(f"{path} (node exit {code})")
            if js_exit == 0:
                js_exit = code
    return js_exit


def has_timing_marker(repo: Path) -> bool:
    try:
        return TIMING_MARKER in (repo / "pyproject.toml").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description=(
            "Fast, receipt-verifiable test runner. On WSL it rsyncs the working "
            "tree to an ext4 clone and runs pytest there with xdist; on Windows "
            "it runs in place. Writes tests/.last_run.json on every run."
        ),
    )
    parser.add_argument(
        "--tier", choices=["full", "fast"], default="full",
        help=(
            "full (default): parallel pass + serial `timing` tail. "
            "fast: parallel pass only, excluding the `timing` marker. "
            "For --verify, the required receipt tier (default: full)."
        ),
    )
    parser.add_argument(
        "--files", nargs="+", metavar="PATH",
        help=(
            "run only these test files (mutation-sensitivity mode); sets "
            "tier='files'. Targets partition by extension: .py through pytest, "
            "each .js through `node <path>` (exit-code accounting, same receipt)"
        ),
    )
    parser.add_argument(
        "-n", type=int, default=DEFAULT_N, metavar="N",
        help=f"xdist workers (default {DEFAULT_N}; never `auto`)",
    )
    parser.add_argument(
        "--serial", action="store_true", help="disable xdist (run serially)",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="force in-place on WSL (the DrvFs path) instead of the ext4 clone",
    )
    parser.add_argument(
        "--clone-dir", default=None, metavar="DIR",
        help=(
            "ext4 clone location (default ~/.cache/wnmt-tests/<repo-dir-name>); "
            "refused unless it is under ~/.cache and is not the repo"
        ),
    )
    parser.add_argument(
        "--verify", action="store_true",
        help=(
            "check tests/.last_run.json against HEAD + the working tree; no rerun. "
            "PASS means the COMMITTED tree at HEAD is green; a receipt from a dirty "
            "tree is refused unless --allow-dirty is given"
        ),
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help=(
            "with --verify: accept a receipt written from an uncommitted tree — asks only "
            "'are these exact bytes green?', never 'is the commit green?'"
        ),
    )
    parser.add_argument(
        "--via-wsl", dest="via_wsl", action="store_true", default=True,
        help=(
            "on Windows: hand the run to WSL so it gets the ext4 clone path "
            "(~3x faster than in place). This is the DEFAULT"
        ),
    )
    parser.add_argument(
        "--no-via-wsl", dest="via_wsl", action="store_false",
        help=(
            "on Windows: run in place on NTFS instead — the path that produces "
            "WINDOWS-environment evidence, which a Linux run cannot"
        ),
    )
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, metavar="N",
        help=f"how many runs to keep in tests/.runs/ (default {DEFAULT_KEEP})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = repo_root()

    if args.verify:
        if args.files:
            print("FAIL: --verify does not take --files")
            return 1
        ok, reason = verify(
            repo, required_tier=args.tier, allow_dirty=args.allow_dirty
        )
        print(("PASS: " if ok else "FAIL: ") + reason)
        return 0 if ok else 1

    host = detect_host(repo)

    # On Windows the default is to hand the run to WSL, which gets the ext4
    # clone path (~3x faster than in place on NTFS). The inner call detects
    # `wsl` and takes the clone path itself, so this never recurses.
    if host == "windows" and args.via_wsl and not args.in_place:
        inner_argv = [
            a for a in sys.argv[1:]
            if a not in ("--via-wsl", "--no-via-wsl", "--in-place")
        ]
        code = run_via_wsl(repo, inner_argv)
        if code is not None:
            return code
        # wsl unreachable: fall through and run in place on NTFS

    # Decide where to run. Compute the tree hash in the repo immediately before
    # the sync/run, so the receipt describes the tree that actually ran.
    commit = compute_commit(repo)
    tree_hash = compute_tree_hash(repo)
    dirty = tree_hash != head_tree_hash(repo)

    if args.in_place or host != "wsl":
        target = repo
        interpreter = Path(sys.executable)
        env_name = {"windows": "windows", "wsl": "wsl-drvfs"}.get(
            host, platform.system().lower()
        )
        lock_fd = None
    else:
        try:
            clone = resolve_clone_dir(repo, args.clone_dir)
        except ClonePathError as exc:
            print(f"refusing to run: {exc}", file=sys.stderr)
            return 2
        # Held for the whole run (the fd stays referenced until main returns).
        lock_fd = acquire_clone_lock(clone)
        sync_to_clone(repo, clone)
        interpreter = ensure_clone_venv(clone)
        target = clone
        env_name = "wsl-ext4"

    marker = has_timing_marker(repo) and not args.serial

    js_targets: list[str] = []
    if args.files:
        tier = "files"
        # `.js` harnesses ride node; everything else stays on the pytest side.
        targets, js_targets = partition_file_targets(list(args.files))
        split_timing = False
    else:
        tier = args.tier
        targets = []
        split_timing = marker

    # A `--files` run with ONLY `.js` targets must not start pytest at all:
    # an empty pytest target list means "collect the whole suite".
    run_pytest_pass = bool(targets) or not args.files

    common = list(BASE_PYTEST_ARGS)
    if not args.serial:
        common += ["-n", str(args.n), "--dist", DEFAULT_DIST]

    # Pass 1 — parallel (timing tests excluded when the marker is active).
    parallel_cmd: str | None = None
    code_parallel, out_parallel, dur_parallel = 0, "", 0.0
    if run_pytest_pass:
        parallel_args = list(common)
        if split_timing:
            parallel_args += ["-m", f"not {TIMING_MARKER}"]
        parallel_args += targets
        parallel_cmd = " ".join(shlex.quote(c) for c in [str(interpreter), "-m", "pytest", *parallel_args])

    print(f"==> run_tests.py: env={env_name} tier={tier} n={'serial' if args.serial else args.n}")
    if run_pytest_pass:
        print(f"==> {parallel_cmd}")
        code_parallel, out_parallel, dur_parallel = run_pytest(interpreter, parallel_args, target)
    counts = parse_counts(out_parallel)

    # Pass 2 — serial `timing` tail (full tier only; never drops coverage).
    code_tail = 0
    dur_tail = 0.0
    out_tail = ""
    tail_cmd: str | None = None
    command = parallel_cmd or ""
    if split_timing and tier == "full":
        tail_args = list(BASE_PYTEST_ARGS) + ["-m", TIMING_MARKER] + targets
        tail_cmd = " ".join(shlex.quote(c) for c in [str(interpreter), "-m", "pytest", *tail_args])
        # Serial AND hash-seed-pinned: the quarantined tests are wall-clock/
        # ordering sensitive (one of them is PYTHONHASHSEED-dependent), so a
        # fixed seed makes the tail reproducible instead of occasionally red.
        command = f"{parallel_cmd} ; PYTHONHASHSEED=0 {tail_cmd}"
        print(f"==> PYTHONHASHSEED=0 {tail_cmd}")
        code_tail, out_tail, dur_tail = run_pytest(
            interpreter, tail_args, target, extra_env={"PYTHONHASHSEED": "0"}
        )
        if code_tail == 5:  # pytest exit 5 == NO_TESTS_COLLECTED: empty tail is fine
            code_tail = 0
        tail_counts = parse_counts(out_tail)
        for key in counts:
            counts[key] += tail_counts[key]

    # Pass 3 — the JS harnesses (only `--files` can request them): node,
    # serial, one process per harness, exit codes captured. Their output never
    # reaches parse_counts (pytest-shaped) — see fold_js_results.
    js_results: list[tuple[str, int]] = []
    js_logs: list[tuple[str, str]] = []
    dur_js = 0.0
    if js_targets:
        node_bin = shutil.which("node")
        if node_bin is None:
            print(
                f"==> ERROR: `node` not found on PATH — cannot run "
                f"{len(js_targets)} JS harness(es); counted as failures "
                f"(exit {NODE_NOT_FOUND_EXIT})",
                file=sys.stderr, flush=True,
            )
            for js_path in js_targets:
                js_results.append((js_path, NODE_NOT_FOUND_EXIT))
                js_logs.append((f"node {js_path}", "node: not found on PATH\n"))
        else:
            for js_path in js_targets:
                js_cmd = " ".join(shlex.quote(c) for c in [node_bin, js_path])
                print(f"==> {js_cmd}", flush=True)
                code_js, out_js, dur_one = run_js_harness(node_bin, js_path, target)
                dur_js += dur_one
                js_results.append((js_path, code_js))
                js_logs.append((js_cmd, out_js))
    if js_logs:
        js_cmds = " ; ".join(cmd for cmd, _ in js_logs)
        command = f"{command} ; {js_cmds}" if command else js_cmds

    pytest_exit = code_parallel if code_parallel != 0 else code_tail
    duration_s = round(dur_parallel + dur_tail + dur_js, 2)

    failed_tests = parse_failed_tests(out_parallel)
    for node in parse_failed_tests(out_tail):
        if node not in failed_tests:
            failed_tests.append(node)
    # Exit-code accounting folds into the SAME fields: counts, failed_tests
    # (naming failing harnesses), and pytest_exit — so the receipt's red/green
    # logic and --verify cover JS runs unchanged.
    js_exit = fold_js_results(counts, failed_tests, js_results)
    if pytest_exit == 0:
        pytest_exit = js_exit

    test_durations_s, file_durations_s = {}, {}
    for label, output in (("parallel", out_parallel), ("timing", out_tail)):
        test_durations_s[label], file_durations_s[label] = parse_durations(output)

    receipt = {
        "schema": SCHEMA,
        "commit": commit,
        "tree_hash": tree_hash,
        "dirty": dirty,
        "env": env_name,
        "python": py_version(interpreter),
        "platform": platform.platform(),
        "n": 0 if args.serial else args.n,
        "dist": "" if args.serial else DEFAULT_DIST,
        "tier": tier,
        "files": list(args.files) if args.files else [],
        "command": command,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "errors": counts["errors"],
        "xfailed": counts["xfailed"],
        "duration_s": duration_s,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "pytest_exit": pytest_exit,
        "failed_tests": failed_tests,
        "duration_parallel_s": round(dur_parallel, 2),
        "duration_tail_s": round(dur_tail, 2),
        # Inclusive worker-seconds from pytest's rounded phase tables. Separate
        # pass maps retain the serial/parallel distinction as markers evolve.
        "test_durations_s": test_durations_s,
        "file_durations_s": file_durations_s,
    }
    log_text = format_run_log(
        env_name, tier, parallel_cmd, out_parallel,
        tail_cmd if (split_timing and tier == "full") else None, out_tail,
        js_runs=js_logs,
    )
    log_path = repo / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_text, encoding="utf-8")
    receipt_path = archive_run(repo, receipt, log_text, keep=args.keep)
    for label, durations in file_durations_s.items():
        if durations:
            slowest = sorted(durations.items(), key=lambda item: (-item[1], item[0]))[:5]
            print(f"==> slowest files ({label}, summed phase seconds): " + "; ".join(
                f"{path} {seconds:.2f}s" for path, seconds in slowest
            ))
    print(
        f"==> receipt {repo / RECEIPT_REL} | window {receipt_path.name} | "
        f"{counts['passed']} passed, "
        f"{counts['failed']} failed, {counts['errors']} errors, "
        f"{counts['skipped']} skipped | exit {pytest_exit} | {duration_s}s"
    )
    if lock_fd is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return 0 if pytest_exit == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
