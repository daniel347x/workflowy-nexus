"""UnitSpec [ap:1T7JZ0] §9.1 — the host-aware tool-path resolver matrix.

Every host (windows | wsl | linux — simulated by monkeypatching the ONE
detection seam ``_detect_host_raw``) × every path-form class C1–C7 × both
directions (read, write); the canonical translations byte-exact and generic
over the drive letter; refusal messages carrying the teaching content
(offending path · daemon host · the remedy); the T2A reproduction at resolver
level (windows × '/tmp/opencode/x.md' refuses and writes NOTHING anywhere);
and real (unpatched) detection returning the test host's own family.

MUTATION SENSITIVITY (§9.3): against the pre-fix tree this file fails at
IMPORT — ``workflowy_mcp.tool_path_resolution`` does not exist there.
"""

from __future__ import annotations

import builtins
import io
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workflowy_mcp import tool_path_resolution as tpr  # noqa: E402
from workflowy_mcp.tool_path_resolution import (  # noqa: E402
    HOST_LINUX,
    HOST_WSL,
    HOST_WINDOWS,
    PathResolution,
    ToolPathError,
    detect_host,
    read_tool_text_file,
    resolve_tool_path,
    write_tool_text_file,
)


@pytest.fixture
def host(monkeypatch):
    """Simulate the daemon's host family via the ONE monkeypatch seam."""

    def _set(name: str) -> str:
        assert name in (HOST_WINDOWS, HOST_WSL, HOST_LINUX)
        monkeypatch.setattr(tpr, "_detect_host_raw", lambda: name)
        return name

    return _set


# ---------------------------------------------------------------------------
# The §6.2 form classes, one or more sample paths each.
# ---------------------------------------------------------------------------

SAMPLES: dict[str, list[str]] = {
    "C1": ["\\\\server\\share\\x.md", "//server/share/x.md"],
    "C2": ["E:\\a\\b.md", "E:/a/b.md"],
    "C3": ["C:foo", "C:"],
    "C4": ["/mnt/e/a/b.md", "/mnt/e"],
    "C5": ["/tmp/opencode/x.md", "/home/dan/x.md", "/mnt/ab/x.md"],
    "C6": ["~/x.md", "~"],
    "C7": ["notes/today.md", "x.md"],
}

# The §6.3 action table (host × class): "pass" (verbatim, translated False),
# "translate" (the canonical prefix rewrite), or "refuse".
ACTION = {
    HOST_WINDOWS: {
        "C1": "pass", "C2": "pass", "C3": "refuse", "C4": "translate",
        "C5": "refuse", "C6": "refuse", "C7": "refuse",
    },
    HOST_WSL: {
        "C1": "refuse", "C2": "translate", "C3": "refuse", "C4": "pass",
        "C5": "pass", "C6": "refuse", "C7": "refuse",
    },
    HOST_LINUX: {
        "C1": "refuse", "C2": "refuse", "C3": "refuse", "C4": "pass",
        "C5": "pass", "C6": "refuse", "C7": "refuse",
    },
}


def _expected_translation(host_name: str, cls: str, raw: str) -> str:
    """The canonical byte-exact translation for a 'translate' cell."""
    if host_name == HOST_WINDOWS and cls == "C4":
        # /mnt/<letter>[/rest] -> <UPPER>:\rest with '\' separators.
        m = tpr._MNT_DRIVE_RE.match(raw)
        letter = m.group(1).upper()
        rest = m.group(2) or ""
        return f"{letter}:\\" + rest.replace("/", "\\")
    if host_name == HOST_WSL and cls == "C2":
        # X:\rest / X:/rest -> /mnt/<lower(letter)>/rest with '/' separators.
        m = tpr._DRIVE_ABS_RE.match(raw)
        letter = m.group(1).lower()
        return f"/mnt/{letter}/" + m.group(2).replace("\\", "/")
    raise AssertionError(f"no translation expected for {host_name} × {cls}")


_MATRIX = [
    (host_name, cls, raw, direction)
    for host_name in (HOST_WINDOWS, HOST_WSL, HOST_LINUX)
    for cls, raws in SAMPLES.items()
    for raw in raws
    for direction in ("read", "write")
]


@pytest.mark.parametrize("host_name,cls,raw,direction", _MATRIX)
def test_action_matrix(host, host_name, cls, raw, direction):
    """§6.3: every host × class × direction cell behaves as specified."""
    host(host_name)
    action = ACTION[host_name][cls]
    if action == "refuse":
        with pytest.raises(ToolPathError) as ei:
            resolve_tool_path(raw, param="output_file", context="t", direction=direction)
        msg = str(ei.value)
        # The teaching contract: the offending path and the daemon's host are
        # always named (a ValueError is a ToolPathError; no other error type).
        assert raw in msg
        assert host_name in msg
        # On windows/wsl every refusal also carries the '/mnt/<drive>' remedy.
        if host_name in (HOST_WINDOWS, HOST_WSL):
            assert "'/mnt/<drive>/…'" in msg
    else:
        res = resolve_tool_path(raw, param="output_file", context="t", direction=direction)
        assert isinstance(res, PathResolution)
        assert res.requested == raw
        assert res.host == host_name
        if action == "pass":
            # Byte-identical pass-through (I4).
            assert res.resolved == raw
            assert res.translated is False
        else:
            assert res.resolved == _expected_translation(host_name, cls, raw)
            assert res.translated is True


# ---------------------------------------------------------------------------
# Canonical translations, byte-exact (§9.1) — and generic over the letter.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host_name,raw,expected",
    [
        (HOST_WINDOWS, "/mnt/e/a/b", "E:\\a\\b"),
        (HOST_WINDOWS, "/mnt/e", "E:\\"),
        (HOST_WINDOWS, "/mnt/e/", "E:\\"),
        (HOST_WINDOWS, "E:\\a\\b", "E:\\a\\b"),          # pass, byte-identical
        (HOST_WSL, "E:\\a\\b", "/mnt/e/a/b"),
        (HOST_WSL, "E:/a/b", "/mnt/e/a/b"),
        (HOST_WSL, "E:\\", "/mnt/e/"),
        (HOST_WSL, "/mnt/e/a/b", "/mnt/e/a/b"),          # pass, byte-identical
    ],
)
def test_canonical_translations_byte_exact(host, host_name, raw, expected):
    host(host_name)
    res = resolve_tool_path(raw, param="output_file", context="t")
    assert res.resolved == expected
    assert res.translated is (expected != raw)


@pytest.mark.parametrize("letter", ["q", "z", "c", "w", "e"])
def test_translation_is_generic_over_drive_letter(host, letter):
    """Never hard-coded to E: any single drive letter maps mechanically."""
    host(HOST_WINDOWS)
    res = resolve_tool_path(f"/mnt/{letter}/x/y.md", param="output_file", context="t")
    assert res.resolved == f"{letter.upper()}:\\x\\y.md"
    assert res.translated is True
    host(HOST_WSL)
    res = resolve_tool_path(f"{letter.upper()}:\\x\\y.md", param="output_file", context="t")
    assert res.resolved == f"/mnt/{letter.lower()}/x/y.md"
    assert res.translated is True


@pytest.mark.parametrize(
    "host_name,raw,expected",
    [
        # The remainder's bytes are untouched — only the prefix is rewritten
        # and separators canonicalized on the translated side (I4).
        (HOST_WINDOWS, "/mnt/e/a b/c&D é中.md", "E:\\a b\\c&D é中.md"),
        (HOST_WINDOWS, "/mnt/e/..weird//double//slash", "E:\\..weird\\\\double\\\\slash"),
        (HOST_WSL, "E:\\a b\\c&D é中.md", "/mnt/e/a b/c&D é中.md"),
        (HOST_WSL, "E:\\no\\normpath\\..\\kept", "/mnt/e/no/normpath/../kept"),
    ],
)
def test_translation_is_byte_faithful_in_the_tail(host, host_name, raw, expected):
    host(host_name)
    res = resolve_tool_path(raw, param="output_file", context="t")
    assert res.resolved == expected
    assert res.translated is True


def test_input_is_stripped_and_requested_echoes_the_stripped_form(host):
    host(HOST_WINDOWS)
    res = resolve_tool_path("  /mnt/e/a.md  ", param="output_file", context="t")
    assert res.requested == "/mnt/e/a.md"
    assert res.resolved == "E:\\a.md"


# ---------------------------------------------------------------------------
# The path_record payload (§5): exact keys, exact order.
# ---------------------------------------------------------------------------

def test_path_record_shape_and_key_order(host):
    host(HOST_WINDOWS)
    res = resolve_tool_path("/mnt/e/x.md", param="output_file", context="glimpse.run")
    record = res.as_dict()
    assert list(record.keys()) == ["requested", "resolved", "translated", "host"]
    assert record == {
        "requested": "/mnt/e/x.md",
        "resolved": "E:\\x.md",
        "translated": True,
        "host": "windows",
    }
    # frozen dataclass: the record structure is immutable.
    with pytest.raises(Exception):
        res.resolved = "tampered"  # type: ignore[misc]


def test_direction_selects_only_the_message_verb_never_the_action(host):
    """§5: read vs write refuse/pass identically; only the verb wording moves."""
    host(HOST_WINDOWS)
    for direction, verb in (("write", "written to"), ("read", "read from")):
        with pytest.raises(ToolPathError) as ei:
            resolve_tool_path(
                "/tmp/opencode/x.md", param="output_file", context="node.read",
                direction=direction,
            )
        assert verb in str(ei.value)
    # ... and the action table is direction-independent (C4 translates both ways).
    for direction in ("write", "read"):
        res = resolve_tool_path(
            "/mnt/e/x.md", param="p", context="c", direction=direction
        )
        assert res.resolved == "E:\\x.md" and res.translated is True


# ---------------------------------------------------------------------------
# Blank / None input (§6.2: blank → refuse) — every host.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", None])
@pytest.mark.parametrize("host_name", [HOST_WINDOWS, HOST_WSL, HOST_LINUX])
def test_blank_path_is_refused_loudly(host, host_name, blank):
    host(host_name)
    with pytest.raises(ToolPathError) as ei:
        resolve_tool_path(blank, param="note_file", context="node.edit")
    assert "empty path" in str(ei.value)
    assert "ABSOLUTE" in str(ei.value)


# ---------------------------------------------------------------------------
# The T2A reproduction at resolver level (§9.1) — the finding that armed
# this program: [ap:RM73MO] / [ap:8OZQHR].
# ---------------------------------------------------------------------------

def test_t2a_windows_tmp_refuses_and_writes_nothing_anywhere(monkeypatch, host, tmp_path):
    """Windows host × '/tmp/opencode/x.md' → refusal, and write_tool_text_file
    writes NOTHING: no open() at all, no file at any interpreted location
    under the test's tmp roots (the pre-fix daemon silently wrote this to
    C:\\tmp\\opencode\\x.md — the T2A finding)."""
    host(HOST_WINDOWS)
    monkeypatch.chdir(tmp_path)

    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    # Inject into the MODULE's globals (the resolver's own open() lookups).
    monkeypatch.setattr(tpr, "open", spy_open, raising=False)

    with pytest.raises(ToolPathError) as ei:
        resolve_tool_path(
            "/tmp/opencode/x.md", param="output_file", context="glimpse.run"
        )
    msg = str(ei.value)
    assert "/tmp/opencode/x.md" in msg          # names the offending path
    assert "windows" in msg                      # names the daemon's host
    assert "'/mnt/<drive>/…'" in msg             # the remedy

    with pytest.raises(ToolPathError):
        write_tool_text_file(
            "/tmp/opencode/x.md", "# content\n", param="output_file", context="node.read"
        )

    # ZERO side effects: nothing was opened, and nothing exists anywhere
    # under the test's tmp roots at any interpreted location.
    assert opened == []
    assert list(tmp_path.rglob("*")) == []


def test_t2a_windows_mnt_path_translates(host):
    host(HOST_WINDOWS)
    res = resolve_tool_path(
        "/mnt/e/__daniel347x/scratch/x.md", param="output_file", context="node.read"
    )
    assert res.translated is True
    assert res.resolved == "E:\\__daniel347x\\scratch\\x.md"
    assert res.host == "windows"


def test_t2a_wsl_windows_path_translates(host):
    host(HOST_WSL)
    res = resolve_tool_path("E:\\x", param="output_file", context="t")
    assert res.resolved == "/mnt/e/x"
    assert res.translated is True


@pytest.mark.parametrize("host_name", [HOST_WINDOWS, HOST_WSL, HOST_LINUX])
def test_t2a_relative_refused_everywhere(host, host_name):
    host(host_name)
    with pytest.raises(ToolPathError) as ei:
        resolve_tool_path("relative/x.md", param="nodes_file", context="node.etch")
    assert "ABSOLUTE" in str(ei.value)


def test_t2a_linux_refuses_windows_forms(host):
    host(HOST_LINUX)
    for raw in ("E:\\x", "E:/x", "\\\\server\\share"):
        with pytest.raises(ToolPathError) as ei:
            resolve_tool_path(raw, param="output_file", context="t")
        msg = str(ei.value)
        assert raw in msg
        assert "linux" in msg
        assert "Windows path" in msg


# ---------------------------------------------------------------------------
# Real (unpatched) detection — the test host's own family (§9.1).
# ---------------------------------------------------------------------------

def _expected_family_from_probes() -> str:
    """Mirror §6.1's probe order independently of the module under test."""
    if os.name == "nt" or sys.platform == "win32":
        return HOST_WINDOWS
    try:
        with open("/proc/sys/kernel/osrelease", "r", encoding="utf-8", errors="replace") as f:
            osrelease = f.read()
    except OSError:
        osrelease = ""
    if "microsoft" in osrelease.lower():
        return HOST_WSL
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return HOST_WSL
    return HOST_LINUX


def test_real_detection_returns_the_test_hosts_own_family():
    """Unpatched detect_host() agrees with the probes, computed independently."""
    assert detect_host() == _expected_family_from_probes()
    assert detect_host() in (HOST_WINDOWS, HOST_WSL, HOST_LINUX)
    # Stateless: repeated calls are stable and re-read the seam.
    assert detect_host() == detect_host()


# ---------------------------------------------------------------------------
# The shared I/O shapes (§5): read_tool_text_file / write_tool_text_file /
# _write_resolved_text_file.
# ---------------------------------------------------------------------------

def test_read_tool_text_file_reads_strict_utf8_lf(host, tmp_path):
    host(HOST_LINUX)
    p = tmp_path / "note.md"
    p.write_bytes(b"line one\r\nline two\rline three\n")
    res, text = read_tool_text_file(str(p), param="note_file", context="node.edit")
    assert text == "line one\nline two\nline three\n"  # universal newlines
    assert res.translated is False and res.resolved == str(p)
    assert res.host == HOST_LINUX


def test_read_tool_text_file_missing_or_directory_is_a_loud_refusal(host, tmp_path):
    host(HOST_LINUX)
    missing = tmp_path / "nope.md"
    with pytest.raises(ToolPathError, match="not an existing file"):
        read_tool_text_file(str(missing), param="note_file", context="node.edit")
    with pytest.raises(ToolPathError, match="not an existing file"):
        read_tool_text_file(str(tmp_path), param="note_file", context="node.edit")


def test_read_tool_text_file_non_utf8_propagates_the_decode_error(host, tmp_path):
    host(HOST_LINUX)
    p = tmp_path / "binary.bin"
    p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    with pytest.raises(UnicodeDecodeError):
        read_tool_text_file(str(p), param="note_file", context="node.edit")


def test_read_tool_text_file_opens_the_resolved_path_under_a_translating_host(
    monkeypatch, host
):
    """The translating host is exercised with an open spy: the RESOLVED
    string is what reaches isfile()/open(), never the requested one."""
    host(HOST_WINDOWS)
    isfile_calls: list[str] = []
    opened: list[str] = []

    monkeypatch.setattr(tpr.os.path, "isfile", lambda p: isfile_calls.append(p) or True)
    monkeypatch.setattr(
        tpr, "open", lambda file, *a, **k: opened.append(file) or io.StringIO("body"),
        raising=False,
    )
    res, text = read_tool_text_file(
        "/mnt/e/scratch/n.md", param="note_file", context="node.edit"
    )
    assert text == "body"
    assert res.resolved == "E:\\scratch\\n.md" and res.translated is True
    assert isfile_calls == ["E:\\scratch\\n.md"]
    assert opened == ["E:\\scratch\\n.md"]


def test_write_tool_text_file_writes_the_resolved_path_under_a_translating_host(
    monkeypatch, host
):
    host(HOST_WINDOWS)
    opened: list[tuple] = []

    class _FakeFile(io.StringIO):
        pass

    def spy_open(file, *args, **kwargs):
        opened.append((file, kwargs.get("newline", "MISSING")))
        return _FakeFile()

    monkeypatch.setattr(tpr, "open", spy_open, raising=False)
    res, nbytes = write_tool_text_file(
        "/mnt/e/out/x.md", "# doc\n", param="output_file", context="render"
    )
    assert res.resolved == "E:\\out\\x.md" and res.translated is True
    assert nbytes == len("# doc\n".encode("utf-8"))
    # The exact-byte contract: opened with newline="" (no CRLF translation).
    assert opened == [("E:\\out\\x.md", "")]


def test_write_tool_text_file_real_write_is_byte_exact(host, tmp_path):
    host(HOST_LINUX)
    dest = tmp_path / "out.md"
    text = "line one\nline two — é中\n"
    res, nbytes = write_tool_text_file(
        str(dest), text, param="output_file", context="render"
    )
    data = dest.read_bytes()
    assert nbytes == len(data) == len(text.encode("utf-8"))
    assert data == text.encode("utf-8")
    assert b"\r" not in data
    assert res.as_dict() == {
        "requested": str(dest), "resolved": str(dest),
        "translated": False, "host": HOST_LINUX,
    }


def test_write_refusal_creates_no_file(host, tmp_path):
    """A refused write path writes zero bytes anywhere (I3)."""
    host(HOST_WINDOWS)
    with pytest.raises(ToolPathError):
        write_tool_text_file(
            str(tmp_path / "x.md").replace(str(tmp_path), "/home/agent"),
            "content", param="output_file", context="render",
        )
    assert list(tmp_path.rglob("*")) == []


def test_write_resolved_text_file_is_the_exact_byte_writer(tmp_path):
    """_write_resolved_text_file: no resolution of its own, exact bytes."""
    dest = tmp_path / "direct.md"
    n = tpr._write_resolved_text_file(str(dest), "a\nb\n")
    assert n == 4
    assert dest.read_bytes() == b"a\nb\n"


# ---------------------------------------------------------------------------
# ToolPathError is a ValueError (established surface classes keep working).
# ---------------------------------------------------------------------------

def test_tool_path_error_is_a_value_error(host):
    host(HOST_WINDOWS)
    with pytest.raises(ValueError):
        resolve_tool_path("/tmp/x", param="output_file", context="t")
