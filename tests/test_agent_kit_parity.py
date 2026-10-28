"""U2 parity tests (spec 12.3): the kit's per-file tree, its slice documents
and its goldens against NEXUS's real repository view, on the fixture
repository F1 of spec 14.

House style: the NEXUS side is the production route — single-file
``map_codebase`` builds the map, cache rows carry it, a coherence-harness
client double serves it, and ``lightning_rod.render_tags`` with
``repository_view: true`` renders it through the real projection and
composer.  No network, no account.  The goldens are spec 11's bytes; each
one is additionally pinned by its SHA-256 from the spec's table.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
KIT_SCRIPTS = REPO_ROOT / "agent-kit" / ".claude" / "skills" / "code-map" / "scripts"
if str(KIT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(KIT_SCRIPTS))

import code_map as cm  # noqa: E402
import nexus_map_codebase as nmc  # noqa: E402
from coherence_harness import make_boundary_capable  # noqa: E402
from test_agent_kit_build import F1_FILES  # noqa: E402
from workflowy_mcp import server  # noqa: E402
from workflowy_mcp.client.api_client_nexus import WorkFlowyClientNexus  # noqa: E402
from workflowy_mcp.mcp_api_registry import dispatch_operation  # noqa: E402

SCRIPT = KIT_SCRIPTS / "code_map.py"
F1_ORDER = ["pkg/core.py", "web/app.ts", "db/schema.sql", "ops/build.sh", "config.yaml"]
ICONS = {
    ".py": "\U0001F40D",
    ".ts": "\U0001F7E6",
    ".sql": "\U0001F5D4",
    ".sh": "\U0001F41A",
    ".yaml": "\U0001F4C4",
}


# ---------------------------------------------------------------------------
# The fixture repository
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def build_fixture_repo(repo: Path, files: dict[str, str], commit: bool = True) -> Path:
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    if commit:
        _git("init", "-q", cwd=repo)
        _git("config", "user.email", "fixture@example.com", cwd=repo)
        _git("config", "user.name", "Fixture", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "fixture", cwd=repo)
    return repo


@pytest.fixture()
def f1_repo(tmp_path: Path) -> Path:
    return build_fixture_repo(tmp_path, F1_FILES)


def run_kit(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(repo), capture_output=True, timeout=300,
    )


# ---------------------------------------------------------------------------
# P1 — tree parity: the kit's per-file mapping equals production's
# ---------------------------------------------------------------------------


def _shape(nodes):
    out = []
    for node in nodes:
        out.append([node.get("name"), node.get("note"), _shape(node.get("children") or [])])
    return out


@pytest.mark.parametrize("rel", F1_ORDER)
def test_p1_tree_parity(f1_repo: Path, rel: str):
    if rel.endswith((".js", ".jsx", ".ts", ".tsx")):
        pytest.importorskip("tree_sitter_language_pack")
    repository = cm.Repository(str(f1_repo), [rel])
    kit = _shape(repository.children(rel))
    nexus = _shape(nmc.map_codebase(str(f1_repo / rel), repo_root=str(f1_repo))["children"])
    assert kit == nexus


# ---------------------------------------------------------------------------
# P2 — document parity through NEXUS's real public route, both modes
# ---------------------------------------------------------------------------


def build_cache_rows(repo: Path):
    """The map exactly as production keeps it: ROOT row, folder rows, file
    rows holding each file's single-file map_codebase children; cache rows
    with dashed ids, parent_id from nesting, priority = the sibling index."""
    rows: list[dict] = []
    counter = [0]

    def add(name: str, note: str, parent: str | None, priority: int) -> str:
        counter[0] += 1
        row_id = "d%06d-0000-4000-8000-%012d" % (counter[0], counter[0])
        rows.append(
            {"id": row_id, "name": name, "note": note, "parent_id": parent, "priority": priority}
        )
        return row_id

    def walk(node: dict, parent: str, index: int) -> None:
        node_id = add(str(node["name"]), str(node.get("note") or ""), parent, index)
        for child_index, child in enumerate(node.get("children") or []):
            walk(child, node_id, child_index)

    root_id = add("\U0001F55E Source Map: repo", f"Root: {repo}", None, 0)
    folders: dict[str, str] = {}
    for rel in sorted(F1_FILES):
        parts = rel.split("/")
        parent = root_id
        for depth in range(len(parts) - 1):
            key = "/".join(parts[: depth + 1])
            if key not in folders:
                folders[key] = add(
                    "\U0001F4C2 " + parts[depth], f"Path: {parts[depth]}", parent, 0
                )
            parent = folders[key]
        basename = parts[-1]
        text = (repo / rel).read_text(encoding="utf-8")
        file_id = add(
            f"{ICONS[os.path.splitext(rel)[1]]} {basename}",
            f"Path: {basename}\nLINE COUNT: {len(text.splitlines())}",
            parent,
            0,
        )
        children = nmc.map_codebase(str(repo / rel), repo_root=str(repo))["children"]
        for index, child in enumerate(children):
            walk(child, file_id, index)
    return rows, root_id


def nexus_render(monkeypatch, rows, root_id, tags, headings_only):
    """render_tags with repository_view through dispatch_operation, served by
    the client double (the harness of test_repository_view.setup).  The
    §6.1 in-memory payload — the one carrying the document — is the shared
    render tail's return; the capture wraps that call as the handler
    receives it, before the reply shaping."""
    client = make_boundary_capable(object.__new__(WorkFlowyClientNexus), nodes=rows)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "_rate_limiter", None)
    monkeypatch.setattr(
        server, "_resolve_note_session_config",
        AsyncMock(return_value=(["wfy-ns"], [], None, {})),
    )
    monkeypatch.setattr(
        server, "_resolve_slice_group_config",
        AsyncMock(return_value=([], None, {}, [], {})),
    )
    captured: dict = {}
    real_tail = server.render_stage2_filter_sort

    def capturing_tail(*args, **kwargs):
        result = real_tail(*args, **kwargs)
        captured["payload"] = result
        return result

    monkeypatch.setattr(server, "render_stage2_filter_sort", capturing_tail)
    asyncio.run(
        dispatch_operation(
            server.LIGHTNING_ROD_REGISTRY, "render_tags",
            {
                "root_handle": root_id,
                "tags": list(tags),
                "headings_only": headings_only,
                "repository_view": True,
            },
            operation_boundary=server._mcp_operation_boundary,
            mutation_admission=server._mcp_mutation_admission,
            exception_mapper=server._mcp_exception_mapper,
            result_finalizer=server._mcp_operation_result_finalizer,
            operation_invoker=server._mcp_archive_operation_invoker,
        )
    )
    return captured["payload"]


P2_TAG_SETS = [
    ["demo"], ["demo-cli"], ["demo--x"], ["demolition"], ["web"], ["demo", "web"], ["nope"],
]


@pytest.mark.parametrize("headings_only", [True, False])
@pytest.mark.parametrize("tags", P2_TAG_SETS)
def test_p2_document_parity(monkeypatch, f1_repo: Path, tags, headings_only):
    if "web" in tags:
        pytest.importorskip("tree_sitter_language_pack")
    rows, root_id = build_cache_rows(f1_repo)
    payload = nexus_render(monkeypatch, rows, root_id, tags, headings_only)
    assert payload.get("success") is True, payload
    document = payload["document"]
    assert isinstance(document, str)
    if tags != ["nope"]:
        assert payload["repository_view"] == {"left_out": 0, "ap_ids_removed": 0}
    kit = run_kit(
        f1_repo, "slice", *tags, *(["--headings"] if headings_only else [])
    )
    assert kit.stdout == document.encode("utf-8")
    if tags == ["nope"]:
        assert document == ""
        assert kit.returncode == 1
    else:
        assert kit.returncode == 0


# ---------------------------------------------------------------------------
# P3 — the goldens: spec 11's bytes, pinned twice (literal + SHA-256)
# ---------------------------------------------------------------------------

GOLDEN_DEMO_FULL = (
    "# `config.yaml`\n"
    "\n"
    "## region `service`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Service block\n"
    "\n"
    "```text\n"
    "Service settings.\n"
    "```\n"
    "\n"
    "```text\n"
    "service:\n"
    "  port: 8080\n"
    "```\n"
    "\n"
    "# `db/schema.sql`\n"
    "\n"
    "## region `customers`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Customer table\n"
    "\n"
    "```text\n"
    "Customer records.\n"
    "```\n"
    "\n"
    "```text\n"
    "CREATE TABLE customers (\n"
    "  id INTEGER PRIMARY KEY\n"
    ");\n"
    "```\n"
    "\n"
    "# `ops/build.sh`\n"
    "\n"
    "## region `build`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Build script\n"
    "\n"
    "```text\n"
    "!/bin/sh\n"
    "Build everything.\n"
    "```\n"
    "\n"
    "```text\n"
    "make all\n"
    "```\n"
    "\n"
    "# `pkg/core.py`\n"
    "\n"
    "## constant `LIMIT`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Largest batch, in items\n"
    "\n"
    "```text\n"
    "Constant\n"
    "```\n"
    "\n"
    "## class `Registry`\n"
    "\n"
    "```text\n"
    "Keeps every registered handler.\n"
    "\n"
    "Template <T> holds.\n"
    "    Path: x\n"
    "```\n"
    "\n"
    "### method `register(self, name: str, handler)`\n"
    "\n"
    "Labels: demo, demo-cli\n"
    "\n"
    "Add one handler\n"
    "\n"
    "```text\n"
    "Store the handler under its name.\n"
    "```\n"
    "\n"
    "## class `Store`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "The one store\n"
    "\n"
    "```text\n"
    "In-memory store.\n"
    "```\n"
    "\n"
    "```text\n"
    "Reads are cheap; writes take the lock.\n"
    "```\n"
    "\n"
    "## function `serve(port: int)`\n"
    "\n"
    "Labels: demo--x\n"
    "\n"
    "Serve requests\n"
    "\n"
    "```text\n"
    "Start serving.\n"
    "```\n"
    "\n"
    "### function `handle(request)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Handle one request\n"
    "\n"
    "```text\n"
    "Nested handler.\n"
    "```\n"
    "\n"
    "## async function `fetch(url: str)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Fetch one page\n"
    "\n"
    "```text\n"
    "Fetch it.\n"
    "```\n"
    "\n"
    "## region `limits`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Size limits\n"
    "\n"
    "```text\n"
    "Tunable caps.\n"
    "```\n"
    "\n"
    "```text\n"
    "# Tunable caps.\n"
    "MAX_A = 10\n"
    "MAX_B = 20\n"
    "```\n"
    "\n"
    "## constant `QUIET`\n"
    "\n"
    "```text\n"
    "Constant\n"
    "```\n"
    "\n"
    "### region `quiet`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Hidden region\n"
    "\n"
    "## class `L1`\n"
    "\n"
    "### class `L2`\n"
    "\n"
    "#### class `L3`\n"
    "\n"
    "##### class `L4`\n"
    "\n"
    "###### class `L5`\n"
    "\n"
    "###### method `deep(self)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Six levels down\n"
    "\n"
    "```text\n"
    "Deepest.\n"
    "```\n"
)

GOLDEN_DEMO_HEADINGS = (
    "# `config.yaml`\n"
    "\n"
    "## region `service`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Service block\n"
    "\n"
    "# `db/schema.sql`\n"
    "\n"
    "## region `customers`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Customer table\n"
    "\n"
    "# `ops/build.sh`\n"
    "\n"
    "## region `build`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Build script\n"
    "\n"
    "# `pkg/core.py`\n"
    "\n"
    "## constant `LIMIT`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Largest batch, in items\n"
    "\n"
    "## class `Registry`\n"
    "\n"
    "### method `register(self, name: str, handler)`\n"
    "\n"
    "Labels: demo, demo-cli\n"
    "\n"
    "Add one handler\n"
    "\n"
    "## class `Store`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "The one store\n"
    "\n"
    "## function `serve(port: int)`\n"
    "\n"
    "Labels: demo--x\n"
    "\n"
    "Serve requests\n"
    "\n"
    "### function `handle(request)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Handle one request\n"
    "\n"
    "## async function `fetch(url: str)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Fetch one page\n"
    "\n"
    "## region `limits`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Size limits\n"
    "\n"
    "## constant `QUIET`\n"
    "\n"
    "### region `quiet`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Hidden region\n"
    "\n"
    "## class `L1`\n"
    "\n"
    "### class `L2`\n"
    "\n"
    "#### class `L3`\n"
    "\n"
    "##### class `L4`\n"
    "\n"
    "###### class `L5`\n"
    "\n"
    "###### method `deep(self)`\n"
    "\n"
    "Labels: demo\n"
    "\n"
    "Six levels down\n"
)

GOLDEN_WEB_FULL = (
    "# `web/app.ts`\n"
    "\n"
    "## class `Greeter`\n"
    "\n"
    "Labels: web\n"
    "\n"
    "The greeter\n"
    "\n"
    "```text\n"
    "*\n"
    "* Greets people.\n"
    "```\n"
    "\n"
    "### method ``greet(name: string = `world`)``\n"
    "\n"
    "Labels: web\n"
    "\n"
    "Say hello\n"
    "\n"
    "## const `loadAll`\n"
    "\n"
    "Labels: web\n"
    "\n"
    "Load everything\n"
)

GOLDEN_WEB_HEADINGS = (
    "# `web/app.ts`\n"
    "\n"
    "## class `Greeter`\n"
    "\n"
    "Labels: web\n"
    "\n"
    "The greeter\n"
    "\n"
    "### method ``greet(name: string = `world`)``\n"
    "\n"
    "Labels: web\n"
    "\n"
    "Say hello\n"
    "\n"
    "## const `loadAll`\n"
    "\n"
    "Labels: web\n"
    "\n"
    "Load everything\n"
)

GOLDEN_DEMO_CLI_FULL = (
    "# `pkg/core.py`\n"
    "\n"
    "## class `Registry`\n"
    "\n"
    "```text\n"
    "Keeps every registered handler.\n"
    "\n"
    "Template <T> holds.\n"
    "    Path: x\n"
    "```\n"
    "\n"
    "### method `register(self, name: str, handler)`\n"
    "\n"
    "Labels: demo, demo-cli\n"
    "\n"
    "Add one handler\n"
    "\n"
    "```text\n"
    "Store the handler under its name.\n"
    "```\n"
)

GOLDEN_DEMO_CLI_HEADINGS = (
    "# `pkg/core.py`\n"
    "\n"
    "## class `Registry`\n"
    "\n"
    "### method `register(self, name: str, handler)`\n"
    "\n"
    "Labels: demo, demo-cli\n"
    "\n"
    "Add one handler\n"
)

GOLDENS = {
    ("demo", False): (
        GOLDEN_DEMO_FULL,
        "81c243c403befb8abe85e105fb5868b2eedaef25513c79598507ffcf11d22829",
    ),
    ("demo", True): (
        GOLDEN_DEMO_HEADINGS,
        "0e4b8a6a4e769f8f7eb4b3990db43e2562f494ea6fa8bccbe5219ec355a06e40",
    ),
    ("web", False): (
        GOLDEN_WEB_FULL,
        "841f9f18634b18e72c444b201f1527ad5b60121fd6b50ebbfe3a84d38240e25c",
    ),
    ("web", True): (
        GOLDEN_WEB_HEADINGS,
        "c9b83175142918555586d78b9f59f8db8526f908cc3c5a12d4f7acb4188e2937",
    ),
    ("demo-cli", False): (
        GOLDEN_DEMO_CLI_FULL,
        "a2776b2a27c80318049f9062f5d81422e7a5b8111a639e46472c990dab9c4d5d",
    ),
    ("demo-cli", True): (
        GOLDEN_DEMO_CLI_HEADINGS,
        "d94fac7cff37e379a80b2596a451f2487ec8b5ccce22b279f711f5c6b632d2dc",
    ),
}


@pytest.mark.parametrize("label", ["demo", "web", "demo-cli"])
@pytest.mark.parametrize("headings_only", [True, False])
def test_p3_golden_bytes(f1_repo: Path, label: str, headings_only: bool):
    if label == "web":
        pytest.importorskip("tree_sitter_language_pack")
    golden, sha = GOLDENS[(label, headings_only)]
    kit = run_kit(f1_repo, "slice", label, *(["--headings"] if headings_only else []))
    assert kit.returncode == 0, kit.stderr
    document = kit.stdout.decode("utf-8")
    assert document == golden
    assert hashlib.sha256(kit.stdout).hexdigest() == sha


def test_p3_labels_output(f1_repo: Path):
    kit = run_kit(f1_repo, "labels")
    assert kit.returncode == 0
    assert kit.stdout.decode("utf-8") == (
        "demo\t11\t(not in catalog)\n"
        "demo--x\t1\t(not in catalog)\n"
        "demo-cli\t1\t(not in catalog)\n"
        "demolition\t1\t(not in catalog)\n"
        "web\t3\t(not in catalog)\n"
    )
