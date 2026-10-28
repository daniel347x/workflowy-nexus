"""U3 tag/untag tests (spec 12.5): every behaviour of spec 10.11, on the
fixture repository F1 of spec 14 and on small purpose-built repositories.
The kit runs as a subprocess in the fixture repository; nothing touches an
account, and the only writes are the fixture's own source files.  After
every write the test runs ``slice`` fresh, so the change is shown through
the same mapping a reader would use.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
KIT_SCRIPTS = REPO_ROOT / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts"
if str(KIT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(KIT_SCRIPTS))

from test_agent_kit_build import F1_FILES  # noqa: E402
from test_agent_kit_parity import build_fixture_repo, run_kit  # noqa: E402

SCRIPT = KIT_SCRIPTS / "code_map.py"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    return build_fixture_repo(tmp_path, files)


@pytest.fixture()
def f1_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path, F1_FILES)


def write_raw(repo: Path, rel: str, data: bytes) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def read_bytes(repo: Path, rel: str) -> bytes:
    return (repo / rel).read_bytes()


def read_lines(repo: Path, rel: str) -> list[str]:
    return read_bytes(repo, rel).decode("utf-8").split("\n")


def replaced_exactly(before: list[str], after: list[str], expected: str) -> int:
    """One line changed in place, to exactly `expected`; returns its index."""
    assert len(before) == len(after), "a replace must not change the line count"
    changed = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
    assert len(changed) == 1, f"expected one changed line, got {changed}"
    assert after[changed[0]] == expected
    return changed[0]


def inserted_exactly(before: list[str], after: list[str], index: int, block: list[str]) -> None:
    """`block` (len(block) lines) inserted at `index`; every other byte's
    line unchanged."""
    assert after[:index] == before[:index]
    assert after[index : index + len(block)] == block
    assert after[index + len(block) :] == before[index:]


def py_block(bid: str, labels: str, comment: str, *, indent: str = "") -> list[str]:
    return [
        f"{indent}# @beacon[",
        f"{indent}#   id={bid},",
        f"{indent}#   slice_labels={labels},",
        f"{indent}#   kind=ast,",
        f"{indent}#   comment={comment},",
        f"{indent}# ]",
    ]


def slice_text(repo: Path, *labels: str) -> str:
    proc = run_kit(repo, "slice", *labels)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.decode("utf-8")


COLLISION_FILES = {
    "pkg/coll.py": (
        "# @beacon[\n"
        "#   id=demo@g,\n"
        "#   slice_labels=demo,\n"
        "#   kind=ast,\n"
        "#   comment=Stale id: names g but sits on h.\n"
        "# ]\n"
        "def h():\n"
        '    """H."""\n'
        "\n"
        "\n"
        "def g():\n"
        '    """G."""\n'
    ),
}

TWO_BLOCKS_FILES = {
    "pkg/two.py": (
        "# @beacon[\n"
        "#   id=one@f,\n"
        "#   slice_labels=one,\n"
        "#   kind=ast,\n"
        "#   comment=First.\n"
        "# ]\n"
        "# @beacon[\n"
        "#   id=two@f,\n"
        "#   slice_labels=two,\n"
        "#   kind=ast,\n"
        "#   comment=Second.\n"
        "# ]\n"
        "def f():\n"
        '    """F."""\n'
    ),
}

LAST_LABEL_FILES = {
    "pkg/one.py": (
        "# @beacon[\n"
        "#   id=solo@f,\n"
        "#   slice_labels=solo,\n"
        "#   kind=ast,\n"
        "#   comment=Only label.\n"
        "# ]\n"
        "def f():\n"
        '    """F."""\n'
    ),
}

TWO_HELPERS_FILES = {
    "pkg/one.py": 'def helper():\n    """One."""\n',
    "pkg/two.py": 'def helper():\n    """Two."""\n',
}

DECORATED_FILES = {
    "pkg/deco.py": (
        "@decorator\n"
        "def target(x: int = 3) -> None:\n"
        '    """Target."""\n'
        "\n"
        "\n"
        "def plain():\n"
        '    """Plain."""\n'
    ),
}


def crlf_bytes() -> bytes:
    lines = [
        "# @beacon[",
        "#   id=old@f,",
        "#   slice_labels=old,",
        "#   kind=ast,",
        "#   comment=Old.,",
        "# ]",
        "",
        "def f():",
        '    """F."""',
        "",
        "def g():",
        '    """G."""',
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# tag: creating a block
# ---------------------------------------------------------------------------


def test_tag_untagged_decorated_function(tmp_path: Path):
    repo = make_repo(tmp_path, DECORATED_FILES)
    before = read_lines(repo, "pkg/deco.py")
    index = before.index("@decorator")
    proc = run_kit(repo, "tag", "target", "fresh", "--comment", "Freshly tagged.")
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.decode().strip()
        == "tagged pkg/deco.py target: fresh (block fresh@target)"
    )
    after = read_lines(repo, "pkg/deco.py")
    inserted_exactly(
        before,
        after,
        index,
        py_block("fresh@target", "fresh", "Freshly tagged."),
    )
    # The block attaches on the re-map and the slice shows it.
    document = slice_text(repo, "fresh")
    assert "## function `target(x: int)`" in document
    assert "Labels: fresh" in document
    assert "Freshly tagged." in document


def test_tag_id_collision_gets_suffix(tmp_path: Path):
    repo = make_repo(tmp_path, COLLISION_FILES)
    before = read_lines(repo, "pkg/coll.py")
    index = before.index("def g():")
    proc = run_kit(repo, "tag", "g", "demo", "--comment", "Tag g.")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == "tagged pkg/coll.py g: demo (block demo@g-2)"
    after = read_lines(repo, "pkg/coll.py")
    inserted_exactly(before, after, index, py_block("demo@g-2", "demo", "Tag g."))
    document = slice_text(repo, "demo")
    assert "## function `h()`" in document  # the stale block's carrier
    assert "## function `g()`" in document  # the newly tagged symbol


# ---------------------------------------------------------------------------
# tag: updating an existing block
# ---------------------------------------------------------------------------


def test_tag_adds_label_existing_labels_first(f1_repo: Path):
    before = read_lines(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "Store", "demo2")
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.decode().strip()
        == "tagged pkg/core.py Store: demo, demo2 (block demo@Store)"
    )
    after = read_lines(f1_repo, "pkg/core.py")
    replaced_exactly(before, after, "#   slice_labels=demo,demo2,")
    document = slice_text(f1_repo, "demo2")
    assert "## class `Store`" in document
    assert "Labels: demo, demo2" in document
    assert "The one store" in document  # the comment is kept


def test_tag_comment_conflict_kept_with_warning(f1_repo: Path):
    before = read_lines(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "Store", "demo2", "--comment", "A different comment")
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr.decode()
    assert "The one store" in err and "A different comment" in err
    after = read_lines(f1_repo, "pkg/core.py")
    replaced_exactly(before, after, "#   slice_labels=demo,demo2,")
    document = slice_text(f1_repo, "demo2")
    assert "The one store" in document  # the disk comment survived


def test_tag_replace_comment(f1_repo: Path):
    before = read_lines(f1_repo, "pkg/core.py")
    proc = run_kit(
        f1_repo, "tag", "Store", "demo2", "--comment", "New words", "--replace-comment"
    )
    assert proc.returncode == 0, proc.stderr
    assert "kept" not in proc.stderr.decode()
    after = read_lines(f1_repo, "pkg/core.py")
    # Two lines change: the labels line and the comment line of Store's block.
    store_id = before.index("#   id=demo@Store,")
    changed = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
    assert len(before) == len(after) and changed == [store_id + 1, store_id + 3]
    assert after[store_id + 1] == "#   slice_labels=demo,demo2,"
    assert after[store_id + 3] == "#   comment=New words,"
    document = slice_text(f1_repo, "demo2")
    assert "New words" in document


def test_tag_equal_comment_is_unchanged(f1_repo: Path):
    data = read_bytes(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "Store", "demo", "--comment", "The one store")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().startswith("unchanged pkg/core.py Store:")
    assert read_bytes(f1_repo, "pkg/core.py") == data  # nothing was written


def test_tag_double_hyphen_label(f1_repo: Path):
    before = read_lines(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "fetch", "demo--x")
    assert proc.returncode == 0, proc.stderr
    after = read_lines(f1_repo, "pkg/core.py")
    replaced_exactly(before, after, "#   slice_labels=demo,demo--x,")
    document = slice_text(f1_repo, "demo--x")
    assert "## async function `fetch(url: str)`" in document
    assert "## function `serve(port: int)`" in document  # the native carrier


# ---------------------------------------------------------------------------
# tag: refusals
# ---------------------------------------------------------------------------


def test_tag_span_id_refused(f1_repo: Path):
    data = read_bytes(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "demo@limits", "demo")
    assert proc.returncode == 1
    assert "span blocks are edited by hand" in proc.stderr.decode()
    assert read_bytes(f1_repo, "pkg/core.py") == data


def test_tag_two_blocks_refused(tmp_path: Path):
    repo = make_repo(tmp_path, TWO_BLOCKS_FILES)
    data = read_bytes(repo, "pkg/two.py")
    proc = run_kit(repo, "tag", "f", "three")
    assert proc.returncode == 1
    assert "two beacon blocks attach to f" in proc.stderr.decode()
    assert read_bytes(repo, "pkg/two.py") == data
    proc = run_kit(repo, "untag", "f", "one")
    assert proc.returncode == 1
    assert "two beacon blocks attach to f" in proc.stderr.decode()
    assert read_bytes(repo, "pkg/two.py") == data


def test_tag_invalid_label_exit_2(f1_repo: Path):
    data = read_bytes(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "tag", "Store", "Demo")
    assert proc.returncode == 2
    assert "not a valid label: Demo" in proc.stderr.decode()
    proc = run_kit(f1_repo, "tag", "Store", "demo.cli")
    assert proc.returncode == 2
    assert read_bytes(f1_repo, "pkg/core.py") == data


def test_tag_replace_comment_requires_comment(f1_repo: Path):
    proc = run_kit(f1_repo, "tag", "Store", "demo2", "--replace-comment")
    assert proc.returncode == 2
    assert "--replace-comment needs --comment" in proc.stderr.decode()


def test_tag_unsafe_comment_exit_2(f1_repo: Path):
    proc = run_kit(f1_repo, "tag", "Store", "demo2", "--comment", "bad @beacon[ text")
    assert proc.returncode == 2
    assert "unsafe beacon comment" in proc.stderr.decode()


def test_tag_file_disambiguates(tmp_path: Path):
    repo = make_repo(tmp_path, TWO_HELPERS_FILES)
    one_before = read_bytes(repo, "pkg/one.py")
    two_before = read_bytes(repo, "pkg/two.py")
    proc = run_kit(repo, "tag", "helper", "demo", "--file", "pkg/one.py",
                   "--comment", "One helper.")
    assert proc.returncode == 0, proc.stderr
    assert "pkg/one.py helper: demo (block demo@helper)" in proc.stdout.decode()
    assert read_bytes(repo, "pkg/two.py") == two_before
    one_tagged = read_bytes(repo, "pkg/one.py")
    assert len(one_tagged.decode().split("\n")) == len(one_before.decode().split("\n")) + 6
    # untag with --file, and untag on a file with no block: refused
    proc = run_kit(repo, "untag", "helper", "demo", "--file", "pkg/two.py")
    assert proc.returncode == 1
    assert "has no beacon block" in proc.stderr.decode()
    assert read_bytes(repo, "pkg/one.py") == one_tagged  # the refusal wrote nothing


# ---------------------------------------------------------------------------
# untag
# ---------------------------------------------------------------------------


def test_untag_removes_one_label(f1_repo: Path):
    before = read_lines(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "untag", "Registry.register", "demo")
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.decode().strip()
        == "untagged pkg/core.py Registry.register: demo-cli "
        "(block demo@Registry.register)"
    )
    after = read_lines(f1_repo, "pkg/core.py")
    replaced_exactly(before, after, "    #   slice_labels=demo-cli,")
    document = slice_text(f1_repo, "demo-cli")
    assert "### method `register(self, name: str, handler)`" in document
    assert "Labels: demo-cli" in document


def test_untag_last_label_deletes_block(tmp_path: Path):
    repo = make_repo(tmp_path, LAST_LABEL_FILES)
    proc = run_kit(repo, "untag", "f", "solo")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == "untagged pkg/one.py f: removed block solo@f"
    assert read_bytes(repo, "pkg/one.py") == b'def f():\n    """F."""\n'
    proc = run_kit(repo, "slice", "solo")
    assert proc.returncode == 1  # nothing carries the label any more


def test_untag_label_not_present_is_unchanged(f1_repo: Path):
    data = read_bytes(f1_repo, "pkg/core.py")
    proc = run_kit(f1_repo, "untag", "Store", "nosuch")
    assert proc.returncode == 0, proc.stderr
    assert "unchanged pkg/core.py Store: no such label(s): nosuch" in proc.stdout.decode()
    assert read_bytes(f1_repo, "pkg/core.py") == data


# ---------------------------------------------------------------------------
# The writer: newline style and byte stability
# ---------------------------------------------------------------------------


def test_crlf_file_stays_crlf_through_a_full_cycle(tmp_path: Path):
    write_raw(tmp_path, "pkg/core.py", crlf_bytes())
    repo = make_repo(tmp_path, {})

    proc = run_kit(repo, "tag", "g", "extra", "--comment", "G tag.")
    assert proc.returncode == 0, proc.stderr
    tagged = read_bytes(repo, "pkg/core.py")
    assert tagged.count(b"\r\n") == tagged.count(b"\n")  # pure CRLF, no bare LF
    lines = tagged.decode("utf-8").split("\r\n")
    index = lines.index("def g():")
    assert lines[index - 6 : index] == py_block("extra@g", "extra", "G tag.")
    document = slice_text(repo, "extra")
    assert "## function `g()`" in document

    # f's only label goes: the whole block is deleted, still pure CRLF.
    proc = run_kit(repo, "untag", "f", "old")
    assert proc.returncode == 0, proc.stderr
    assert b"removed block old@f" in proc.stdout
    final = read_bytes(repo, "pkg/core.py")
    assert final.count(b"\r\n") == final.count(b"\n")
    # The cycle's net effect, byte for byte: f's block gone, g's block in
    # (the blank line that followed f's block stays).
    expected = [
        "",
        "def f():",
        '    """F."""',
        "",
        "# @beacon[",
        "#   id=extra@g,",
        "#   slice_labels=extra,",
        "#   kind=ast,",
        "#   comment=G tag.,",
        "# ]",
        "def g():",
        '    """G."""',
        "",
    ]
    assert final.decode("utf-8").split("\r\n") == expected


def test_tag_and_untag_touch_no_other_byte(f1_repo: Path):
    before = read_bytes(f1_repo, "pkg/core.py")
    # Store.get is untagged and indented (inside its class).
    proc = run_kit(f1_repo, "tag", "Store.get", "seasonal", "--comment", "Seasonal.")
    assert proc.returncode == 0, proc.stderr
    mid_lines = read_bytes(f1_repo, "pkg/core.py").decode("utf-8").split("\n")
    index = mid_lines.index("    def get(self, key: str):")
    assert mid_lines[index - 6 : index] == py_block(
        "seasonal@Store.get", "seasonal", "Seasonal.", indent="    "
    )
    proc = run_kit(f1_repo, "untag", "Store.get", "seasonal")
    assert proc.returncode == 0, proc.stderr
    assert read_bytes(f1_repo, "pkg/core.py") == before  # round trip restores


# ---------------------------------------------------------------------------
# JavaScript/TypeScript (parser present)
# ---------------------------------------------------------------------------


def test_tag_js_ts_method(f1_repo: Path):
    pytest.importorskip("tree_sitter_language_pack")
    before = read_lines(f1_repo, "web/app.ts")
    proc = run_kit(f1_repo, "tag", "Greeter.greet", "web2")
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.decode().strip()
        == "tagged web/app.ts Greeter.greet: web, web2 (block web@Greeter.greet)"
    )
    after = read_lines(f1_repo, "web/app.ts")
    changed = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
    greet_labels = before.index("  //   slice_labels=web,")  # the method's own block
    assert changed == [greet_labels]
    assert after[changed[0]] == "  //   slice_labels=web,web2,"
    document = slice_text(f1_repo, "web2")
    assert "### method ``greet(name: string = `world`)``" in document
    assert "Labels: web, web2" in document


# ---------------------------------------------------------------------------
# W2: a block id quoted in prose can neither redirect a write nor make the
# real attached id falsely ambiguous.
# ---------------------------------------------------------------------------

DECOY_PY = (
    "# @beacon[\n#   id=o@1,\n#   slice_labels=o,\n#   kind=ast,\n#   comment=Orphan,\n# ]\n"
    "x = 1\n\n\n"
    'def g():\n    """Documentation quoting another block:\n\n    id: o@1\n\n    """\n    pass\n'
)

ATTACHED_DECOY_PY = (
    "# @beacon[\n#   id=d@g,\n#   slice_labels=d,\n#   kind=ast,\n#   comment=The real one,\n# ]\n"
    'def g():\n    """The real carrier; the id is quoted again below.\n\n    id: d@g\n\n    """\n    pass\n\n\n'
    'def h():\n    """Only prose:\n\n    id: d@g\n\n    """\n    pass\n'
)


def test_w2_tag_or_untag_by_orphan_id_refused_and_write_nothing(tmp_path: Path):
    repo = make_repo(tmp_path, {"z.py": DECOY_PY})
    before = read_bytes(repo, "z.py")
    tagged = run_kit(repo, "tag", "o@1", "demo")
    assert tagged.returncode == 1, tagged.stdout
    assert b"attaches to no definition" in tagged.stderr
    assert read_bytes(repo, "z.py") == before
    untagged = run_kit(repo, "untag", "o@1", "demo")
    assert untagged.returncode == 1, untagged.stdout
    assert read_bytes(repo, "z.py") == before


def test_w2_tag_by_attached_id_edits_its_own_symbol_despite_decoy(tmp_path: Path):
    repo = make_repo(tmp_path, {"z.py": ATTACHED_DECOY_PY})
    before = read_lines(repo, "z.py")
    tagged = run_kit(repo, "tag", "d@g", "demo")
    assert tagged.returncode == 0, tagged.stderr
    after = read_lines(repo, "z.py")
    # the block's own labels line is the only line that changed
    changed = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
    assert changed == [2], (changed, after[2])
    assert after[2] == "#   slice_labels=d,demo,"
    # the prose in h is untouched
    assert "id: d@g" in "\n".join(after)
    shown = slice_text(repo, "demo")
    assert "## function `g()`" in shown
    assert "Labels: d, demo" in shown
    assert "def h" not in shown


# ---------------------------------------------------------------------------
# W4: creation, replacement and deletion change only the selected block's
# byte range; every byte outside it is exactly the original byte, including
# mixed LF/CRLF/CR terminators, final-newline presence and form-feed /
# U+2028 inside valid strings.
# ---------------------------------------------------------------------------

GREET = 'def greet(name):\n    """Doc."""\n    return name\n'


def _docstrings(data: bytes) -> dict:
    import ast

    tree = ast.parse(data.decode("utf-8"))
    return {
        node.name: ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _assert_insertion_byte_exact(after: bytes, before: bytes) -> None:
    """after == before[:k] + inserted + before[k:] for one contiguous
    inserted range; every byte outside it is the original byte."""
    p = 0
    while p < len(before) and p < len(after) and before[p] == after[p]:
        p += 1
    s = 0
    while (
        s < len(before) - p
        and s < len(after) - p
        and before[len(before) - 1 - s] == after[len(after) - 1 - s]
    ):
        s += 1
    assert after[:p] + after[len(after) - s:] == before, (
        "bytes outside the inserted block changed:\n"
        f"before={before!r}\nafter={after!r}"
    )


def _w4_repo(tmp_path: Path, source: bytes) -> Path:
    repo = tmp_path / "repo"
    build_fixture_repo(repo, {"pkg/doc.py": source.decode("utf-8")})
    return repo


def _w4_roundtrip(tmp_path: Path, source: bytes) -> None:
    repo = _w4_repo(tmp_path, source)
    before = read_bytes(repo, "pkg/doc.py")
    before_docs = _docstrings(before)

    tagged = run_kit(repo, "tag", "greet", "demo")
    assert tagged.returncode == 0, tagged.stderr
    after_tag = read_bytes(repo, "pkg/doc.py")
    _assert_insertion_byte_exact(after_tag, before)
    assert _docstrings(after_tag) == before_docs
    shown = run_kit(repo, "slice", "demo")
    assert shown.returncode == 0, shown.stderr
    assert "## function `greet(name)`" in shown.stdout.decode("utf-8")

    second = run_kit(repo, "tag", "greet", "demo2")
    assert second.returncode == 0, second.stderr
    after_second = read_bytes(repo, "pkg/doc.py")
    # the replacement changes exactly the block's labels segment
    assert after_second == after_tag.replace(
        b"slice_labels=demo,", b"slice_labels=demo,demo2,", 1
    )

    removed = run_kit(repo, "untag", "greet", "demo2")
    assert removed.returncode == 0, removed.stderr
    after_untag_label = read_bytes(repo, "pkg/doc.py")
    assert after_untag_label == after_tag

    deleted = run_kit(repo, "untag", "greet", "demo")
    assert deleted.returncode == 0, deleted.stderr
    assert read_bytes(repo, "pkg/doc.py") == before


def test_w4_byte_fidelity_matrix(tmp_path: Path):
    crlf = GREET.replace("\n", "\r\n")
    cases = {
        "lf_final": GREET.encode("utf-8"),
        "lf_no_final": GREET.encode("utf-8").rstrip(b"\n"),
        "crlf_final": crlf.encode("utf-8"),
        "crlf_no_final": crlf.encode("utf-8").rstrip(b"\r\n"),
        "mixed_lf_and_crlf": (
            'def greet(name):\n    """Doc."""\r\n    return name\n'
        ).encode("utf-8"),
        "cr_only_no_final": (
            'def greet(name):\r    """Doc."""\r    return name'
        ).encode("utf-8"),
        "cr_only_final": (
            'def greet(name):\r    """Doc."""\r    return name\r'
        ).encode("utf-8"),
        "form_feed_inside_docstring": (
            'def greet(name):\n    """First line\x0cSecond line."""\n    return name\n'
        ).encode("utf-8"),
        "u2028_inside_docstring": (
            'def greet(name):\n    """First line\u2028Second line."""\n    return name\n'
        ).encode("utf-8"),
    }
    for name, source in cases.items():
        sub = tmp_path / name
        sub.mkdir()
        _w4_roundtrip(sub, source)


def test_w4_forced_verify_failure_restores_original_bytes(tmp_path: Path, monkeypatch):
    import code_map as cm  # noqa: E402

    repo = _w4_repo(tmp_path, GREET.encode("utf-8"))
    before = read_bytes(repo, "pkg/doc.py")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cm, "_verify_written_block", lambda *a, **k: False)
    rc = cm.main(["tag", "greet", "demo"])
    assert rc == 1
    assert read_bytes(repo, "pkg/doc.py") == before


# ---------------------------------------------------------------------------
# r2 W2: insertion coordinates are translated to the original source's
# boundaries (WorkOrder [ap:J7STY1] W2).  The engine's own decorator-range
# authority decides the row; the tests assert the exact insertion position,
# not merely that a block attached.
# ---------------------------------------------------------------------------

WO_EXOTIC_FILES = {
    "pkg/exotic.py": (
        "def first(x):\n"
        "    \"\"\"Before\x0cAnd after\u2028still one string.\"\"\"\n"
        "    return x\n"
        "\n"
        "class Holder:\n"
        "    @decorator(\n"
        "        \"arg\",\n"
        "    )\n"
        "    def meth(self):\n"
        "        \"\"\"M.\"\"\"\n"
        "        return 1\n"
        "\n"
        "class Plain:\n"
        "    def bare(self):\n"
        "        return 0\n"
    ),
}

MULTILINE_DECORATOR_FILES = {
    "pkg/multi.py": (
        "class Holder:\n"
        "    @decorator(\n"
        "        \"arg\",\n"
        "    )\n"
        "    def meth(self):\n"
        "        \"\"\"M.\"\"\"\n"
        "        return 1\n"
        "\n"
        "class Plain:\n"
        "    def bare(self):\n"
        "        return 0\n"
    ),
}


def test_w2_exotic_separators_above_decorated_target_use_physical_rows(tmp_path: Path):
    """U01 §6.1: the outline and writer now agree on physical source rows.

    FF/U+2028 inside an earlier string no longer trigger the old verification
    refusal. The block lands above the decorator, changes no unrelated bytes,
    maps to the requested method, and can be removed to restore every byte.
    """
    import code_map  # noqa: F401  (KIT_SCRIPTS is on sys.path)

    repo = make_repo(tmp_path, WO_EXOTIC_FILES)
    before = read_bytes(repo, "pkg/exotic.py")
    proc = run_kit(repo, "tag", "Holder.meth", "demo")
    assert proc.returncode == 0, proc.stderr
    after = read_bytes(repo, "pkg/exotic.py")
    _assert_insertion_byte_exact(after, before)
    cut = before.index(b"    @decorator(")
    assert after[:cut] == before[:cut]
    assert after.endswith(before[cut:])
    shown = slice_text(repo, "demo")
    assert "## class `Holder`" in shown and "### method `meth(self)`" in shown
    removed = run_kit(repo, "untag", "Holder.meth", "demo")
    assert removed.returncode == 0, removed.stderr
    assert read_bytes(repo, "pkg/exotic.py") == before
    # The shared physical coordinates aim exactly at the decorator opener;
    # Unicode characters inside the earlier string create no extra edit rows.
    text = before.decode("utf-8")
    lines = text.split("\n")
    physical = text.split("\n").index("    def meth(self):") + 1
    assert code_map._python_insert_row(text, lines, {"orig_lineno_start_unused": physical}) == (
        lines.index("    @decorator(")
    )


def _u02_kit_source(language="python", ending="\n"):
    prefix = "# " if language == "python" else "// "
    rows = [prefix + value for value in ("@beacon[", "  id=old@target,", "  slice_labels=old,", "  kind=ast,", "  comment=Kept.,", "]")]
    code = ["def target():", "    return 1", "", "def safe():", "    return 2"] if language == "python" else ["function target() { return 1; }", "", "function safe() { return 2; }"]
    return (ending.join(rows) + ending).encode(), ending.join(code).encode()


@pytest.mark.parametrize("language,suffix", [("python", ".py"), ("js_ts", ".ts")])
@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
@pytest.mark.parametrize("operation", ["tag", "untag-label", "untag-last"])
def test_u02_kit_byte_table(tmp_path, language, suffix, regime, operation):
    """42131b6: private splitlines splices lose BOM or overshoot exotic rows;
    CR-only replacement also invents LF. The new result uses physical bytes.
    """
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "{controls}"' + ("\r\n" if regime == "mixed" else ending)).encode()
    block, tail = _u02_kit_source(language, ending)
    if operation == "untag-label":
        block = block.replace(b"slice_labels=old,", b"slice_labels=old,extra,")
    tail += ("" if regime == "no-eof" else ending * (4 if regime == "trailing" else 1)).encode()
    bom = b"\xef\xbb\xbf" if regime == "bom" else b""
    # BOM immediately before a first-row block is a separate delete witness.
    if regime == "bom":
        prefix = b""
    raw = bom + prefix + block + tail
    rel = "pkg/source" + suffix
    repo = make_repo(tmp_path, {rel: raw.decode()})
    path = repo / rel
    os.chmod(path, 0o744)
    args = ("tag", "target", "extra") if operation == "tag" else ("untag", "target", "extra" if operation == "untag-label" else "old")
    result = run_kit(repo, *args)
    assert result.returncode == 0, result.stderr
    replacement = block.replace(b"slice_labels=old,", b"slice_labels=old,extra,") if operation == "tag" else block.replace(b"slice_labels=old,extra,", b"slice_labels=old,") if operation == "untag-label" else b""
    assert path.read_bytes() == bom + prefix + replacement + tail
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


@pytest.mark.parametrize("language,suffix", [("python", ".py"), ("js_ts", ".ts")])
@pytest.mark.parametrize("regime", ["lf", "crlf", "cr", "mixed", "bom", "no-eof", "trailing", "controls"])
def test_u02_kit_create_byte_table(tmp_path, language, suffix, regime):
    """42131b6: BOM create and exotic-prefix geometry fail attachment; CR uses LF."""
    ending = {"crlf": "\r\n", "cr": "\r"}.get(regime, "\n")
    controls = "\x1b\f\v\x1c\x1d\x1e\x85\u2028\u2029" if regime == "controls" else "literal"
    prefix = (f'TEXT = "{controls}"' + ("\r\n" if regime == "mixed" else ending)).encode()
    _, tail = _u02_kit_source(language, ending)
    tail += ("" if regime == "no-eof" else ending * (4 if regime == "trailing" else 1)).encode()
    raw = (b"\xef\xbb\xbf" if regime == "bom" else b"") + prefix + tail
    rel = "a" + suffix
    repo = make_repo(tmp_path, {rel: raw.decode()})
    path = repo / rel
    os.chmod(path, 0o744)
    result = run_kit(repo, "tag", "target", "extra")
    assert result.returncode == 0, result.stderr
    _assert_insertion_byte_exact(path.read_bytes(), raw)
    assert (ending + "def target" if language == "python" else ending + "function target").encode() in path.read_bytes()
    removed = run_kit(repo, "untag", "target", "extra")
    assert removed.returncode == 0, removed.stderr
    assert path.read_bytes() == raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o744


@pytest.mark.parametrize("operation", ["tag", "untag"])
def test_u02_kit_fingerprints_the_bytes_text_decoded(tmp_path, monkeypatch, capsys, operation):
    """42131b6: the second original read silently accepts/overwrites a later save."""
    import code_map as cm
    block, tail = _u02_kit_source()
    raw = block + tail + b"\n"
    repo = make_repo(tmp_path, {"a.py": raw.decode()})
    path = repo / "a.py"
    external = raw + b"external_save = 3\r\n"
    read = cm.Repository.text
    state = {}
    def observe_then_save(self, rel):
        text = read(self, rel)
        if rel == "a.py" and not state:
            path.write_bytes(external)
            state["mtime"] = path.stat().st_mtime_ns
        return text
    monkeypatch.chdir(repo)
    with monkeypatch.context() as patch:
        patch.setattr(cm.Repository, "text", observe_then_save)
        assert cm.main([operation, "target", "extra" if operation == "tag" else "old"]) == 1
    assert "source_changed_since_plan" in capsys.readouterr().err
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (external, state["mtime"])
    assert cm.main(["tag", "safe", "fresh"]) == 0
    assert path.read_bytes().endswith(b"    return 2\nexternal_save = 3\r\n")


@pytest.mark.parametrize("operation", ["tag", "untag"])
@pytest.mark.parametrize("when", ["own-bytes", "before-restore", "during-restore"])
def test_u02_kit_postverify_restores_only_its_own_bytes(tmp_path, monkeypatch, capsys, operation, when):
    """42131b6: forced verify failure unconditionally restores over newer bytes."""
    import code_map as cm
    block, tail = _u02_kit_source()
    raw = block + tail + b"\n"
    repo = make_repo(tmp_path, {"a.py": raw.decode()})
    path = repo / "a.py"
    state = {"copies": 0}
    def save():
        state["external"] = path.read_bytes() + b"external_save = 3\r\n"
        path.write_bytes(state["external"])
        state["mtime"] = path.stat().st_mtime_ns
    def fail_verify(*args):
        assert path.read_bytes() != raw
        if when == "before-restore":
            save()
        return False
    monkeypatch.chdir(repo)
    with monkeypatch.context() as patch:
        patch.setattr(cm, "_verify_written_block", fail_verify)
        if when == "during-restore":
            copymode = shutil.copymode
            def copy_then_save(*args, **kwargs):
                copymode(*args, **kwargs)
                state["copies"] += 1
                if state["copies"] == 2:
                    save()
            patch.setattr(shutil, "copymode", copy_then_save)
        assert cm.main([operation, "target", "extra" if operation == "tag" else "old"]) == 1
    error = capsys.readouterr().err
    if when == "own-bytes":
        assert path.read_bytes() == raw
        assert "original file was restored" in error
    else:
        assert "restore conflict" in error
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (state["external"], state["mtime"])
    assert not list(repo.glob(".beacon_atomic_*"))
    assert cm.main(["tag", "safe", "fresh"]) == 0


@pytest.mark.parametrize("operation", ["tag", "untag"])
def test_u02_kit_proof_refuses_before_publication_and_safe_neighbour_works(tmp_path, monkeypatch, capsys, operation):
    """Inject an overshoot at the shared plan seam; proof refuses before IO.

    The kit no longer accepts a private attached-block extent to corrupt.
    The original 42131b6 wrong-range behavior is retained as U02's witness.
    """
    from dataclasses import replace
    import code_map as cm
    block, tail = _u02_kit_source()
    raw = block + tail + b"\n"
    repo = make_repo(tmp_path, {"a.py": raw.decode()})
    path = repo / "a.py"
    before = path.read_bytes(), path.stat().st_mtime_ns
    planner = cm._engine.plan_beacon_node_mutation
    def crossing(*args, **kwargs):
        mutation = planner(*args, **kwargs)
        plan = mutation["edit_plan"]
        end = plan.snapshot.raw_bytes.index(b"def target():") + len(b"def target():\n")
        mutation["edit_plan"] = replace(plan, edits=(replace(plan.edits[0], end_byte=end), *plan.edits[1:]))
        return mutation
    monkeypatch.chdir(repo)
    with monkeypatch.context() as patch:
        patch.setattr(cm._engine, "plan_beacon_node_mutation", crossing)
        assert cm.main([operation, "target", "extra" if operation == "tag" else "old"]) == 1
    assert "plan_mismatch" in capsys.readouterr().err
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert cm.main(["tag", "safe", "fresh"]) == 0
    assert path.read_bytes().startswith(block + b"def target():\n    return 1\n\n")


@pytest.mark.parametrize("operation", ["tag", "untag"])
def test_u02_kit_binary_temp_is_fsynced_and_failure_is_local(tmp_path, monkeypatch, capsys, operation):
    """42131b6: kit temp publication never fsyncs, so the injected fault is missed."""
    import code_map as cm
    block, tail = _u02_kit_source()
    raw = block + tail + b"\n"
    repo = make_repo(tmp_path, {"a.py": raw.decode()})
    path = repo / "a.py"
    before = path.read_bytes(), path.stat().st_mtime_ns
    fsynced = []
    def fail_fsync(fd):
        fsynced.append(fd)
        raise PermissionError("injected fsync failure")
    monkeypatch.chdir(repo)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_fsync)
        assert cm.main([operation, "target", "extra" if operation == "tag" else "old"]) == 1
    assert fsynced and "disk_write_failed" in capsys.readouterr().err
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(repo.glob(".beacon_atomic_*"))
    calls = []
    fsync, fdopen = os.fsync, os.fdopen
    def record_fsync(fd):
        calls.append("fsync")
        return fsync(fd)
    def windows_fdopen(fd, mode="r", *args, **kwargs):
        calls.append(mode)
        if "b" not in mode:
            kwargs["newline"] = "\r\n"
        return fdopen(fd, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", record_fsync)
        patch.setattr(os, "fdopen", windows_fdopen)
        assert cm.main(["tag", "safe", "fresh"]) == 0
    assert calls == ["wb", "fsync"]
    assert b"\r" not in path.read_bytes()


@pytest.mark.parametrize("language,filename,prefix,alpha,beta", [
    ("python", "copy.py", "#", ["def alpha():", "    pass"], ["def beta():", "    pass"]),
    ("js_ts", "copy.js", "//", ["function alpha() {}"], ["function beta() {}"]),
])
@pytest.mark.parametrize("operation", ["tag", "untag", "delete"])
def test_u03_kit_anchored_duplicate_edits_only_beta(tmp_path, language, filename, prefix, alpha, beta, operation):
    """004d1ba: _attached_block refuses a physically identified duplicate."""
    def block(labels):
        return [f"{prefix} @beacon[", f"{prefix} id=p@copy,", f"{prefix} slice_labels={labels},",
                f"{prefix} kind=ast,", f"{prefix} comment=Kept.,", f"{prefix} ]"]
    first = block("one") + alpha + [""]
    labels = "old" if operation == "delete" else "old,keep"
    second = block(labels) + beta
    raw = ("\n".join(first + second) + "\n").encode()
    repo = make_repo(tmp_path, {filename: raw.decode()})
    proc = run_kit(repo, "tag" if operation == "tag" else "untag", "beta", "new" if operation == "tag" else "old")
    assert proc.returncode == 0, proc.stderr
    prefix_bytes = ("\n".join(first) + "\n").encode()
    if operation == "delete":
        expected = prefix_bytes + ("\n".join(beta) + "\n").encode()
    else:
        tail = ("\n".join(second) + "\n").encode()
        replacement = b"slice_labels=old,keep,new," if operation == "tag" else b"slice_labels=keep,"
        expected = prefix_bytes + tail.replace(b"slice_labels=old,keep,", replacement)
    assert read_bytes(repo, filename) == expected


@pytest.mark.parametrize("operation", ["tag", "untag"])
def test_u03_kit_jsdoc_unknown_fields_and_snippets_survive(tmp_path, operation):
    """004d1ba rebuilds as // and drops the opaque field."""
    rows = ["/**", " * @beacon[", " * id=p@target,", " * slice_labels=old,keep,",
            " * kind=ast,", " * opaque=author-owned,", " * role=Deliberate role,", " * show_span=true,",
            " * start_snippet=first token,", " * end_snippet=last token,", " * comment=Kept.,",
            " * ]", " */", "function target() {}", "", "function safe() {}"]
    raw = ("\r\n".join(rows) + "\r\n").encode()
    repo = make_repo(tmp_path, {"doc.js": raw.decode()})
    # make_repo's text write may normalize on some hosts; the source bytes are
    # pinned after setup rather than relying on the fixture writer's newline.
    write_raw(repo, "doc.js", raw)
    proc = run_kit(repo, operation, "target", "new" if operation == "tag" else "old")
    assert proc.returncode == 0, proc.stderr
    replacement = b"slice_labels=old,keep,new," if operation == "tag" else b"slice_labels=keep,"
    assert read_bytes(repo, "doc.js") == raw.replace(b"slice_labels=old,keep,", replacement)
    assert read_bytes(repo, "doc.js").count(b"kind=ast,") == 1


def test_u03_kit_python_implicit_kind_and_opaque_field_are_not_materialized(tmp_path):
    rows = ["# @beacon[", "# id=p@target,", "# slice_labels=old,", "# opaque=kept,",
            "# comment=Keep, punctuation = and ] too.,", "# ]", "def target():", "    pass"]
    raw = ("\n".join(rows) + "\n").encode()
    repo = make_repo(tmp_path, {"implicit.py": raw.decode()})
    proc = run_kit(repo, "tag", "target", "new")
    assert proc.returncode == 0, proc.stderr
    assert read_bytes(repo, "implicit.py") == raw.replace(b"slice_labels=old,", b"slice_labels=old,new,")


def test_u03_kit_ambiguous_candidates_are_local_and_safe_symbol_writes(tmp_path):
    rows = ["def twin():", "    pass", "", "def twin():", "    pass", "", "def safe():", "    pass"]
    raw = ("\n".join(rows) + "\n").encode()
    repo = make_repo(tmp_path, {"twins.py": raw.decode()})
    path = repo / "twins.py"
    stamp = path.stat().st_mtime_ns
    proc = run_kit(repo, "tag", "twin", "new")
    assert proc.returncode == 1
    assert "twins.py:1" in proc.stderr.decode() and "twins.py:4" in proc.stderr.decode()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (raw, stamp)
    assert run_kit(repo, "tag", "safe", "new").returncode == 0
    assert path.read_bytes().startswith(raw[:raw.index(b"def safe():")])


def test_u02_kit_damaged_neighbour_does_not_block_safe_tag(tmp_path, monkeypatch):
    """A malformed opener is not a file-wide veto in the shared proof."""
    import code_map as cm
    raw = b"# @beacon[\n# id=broken,\n# kind=ast,\nvalue = 1\n\ndef safe():\n    return 2\n"
    repo = make_repo(tmp_path, {"a.py": raw.decode()})
    monkeypatch.chdir(repo)
    assert cm.main(["tag", "safe", "fresh"]) == 0
    after = (repo / "a.py").read_bytes()
    assert after.startswith(raw[:raw.index(b"def safe")])
    assert cm.main(["untag", "safe", "fresh"]) == 0
    assert (repo / "a.py").read_bytes() == raw


def test_w2_multiline_decorator_places_block_above_opener(tmp_path: Path):
    """Ordinary multiline decorator, no boundary characters above: the block
    lands directly above `    @decorator(`, with the definition's
    indentation, and the change is shown through `slice`."""
    repo = make_repo(tmp_path, MULTILINE_DECORATOR_FILES)
    before = read_lines(repo, "pkg/multi.py")
    index = before.index("    @decorator(")
    proc = run_kit(repo, "tag", "Holder.meth", "demo", "--comment", "Tagged.")
    assert proc.returncode == 0, proc.stderr
    after = read_lines(repo, "pkg/multi.py")
    inserted_exactly(
        before,
        after,
        index,
        py_block("demo@Holder.meth", "demo", "Tagged.", indent="    "),
    )
    document = slice_text(repo, "demo")
    assert "## method `meth(self)`" in document
    assert "Labels: demo" in document
    removed = run_kit(repo, "untag", "Holder.meth", "demo")
    assert removed.returncode == 0, removed.stderr
    assert read_lines(repo, "pkg/multi.py") == before


def test_w2_undecorated_target_places_block_above_definition(tmp_path: Path):
    """The undecorated control: the block lands directly above the
    definition, exactly, and the round trip restores the original bytes."""
    repo = make_repo(tmp_path, MULTILINE_DECORATOR_FILES)
    before = read_lines(repo, "pkg/multi.py")
    index = before.index("    def bare(self):")
    proc = run_kit(repo, "tag", "Plain.bare", "solo", "--comment", "Bare.")
    assert proc.returncode == 0, proc.stderr
    after = read_lines(repo, "pkg/multi.py")
    inserted_exactly(
        before,
        after,
        index,
        py_block("solo@Plain.bare", "solo", "Bare.", indent="    "),
    )
    document = slice_text(repo, "solo")
    assert "## method `bare(self)`" in document
    removed = run_kit(repo, "untag", "Plain.bare", "solo")
    assert removed.returncode == 0, removed.stderr
    assert read_lines(repo, "pkg/multi.py") == before
