"""S04 permanent AST fences (§6.2/H3 + H1(a)).

- **Raw-appender fence:** production calls to ``append_journal_transaction``
  are permitted ONLY from the S04 sealing authority (one journal-sealing
  door, H3).  Any residual direct production caller is migrated, never
  allowlisted; future sessions call the sealer.
- **Dirty-state fence (H1(a)):** direct reads of ``manifest.dirty_status``
  are permitted only in the exact function-scoped state-management entries
  (schema validation/serialization, S02 clearing/cut computation, S04
  barrier publication and the boundary-status authority).  Every boundary
  hard-stop DECISION goes through ``hard_stop_state()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "workflowy_mcp"
ROOT_MODULES = [
    REPO / "nexus_map_codebase.py",
    REPO / "beacon_obtain_code_snippet.py",
    REPO / "markdown_roundtrip.py",
    REPO / "glimpse_deploy_trampoline.py",
    REPO / "beacon_mutation_contract.py",
]

# H3: the ONLY permitted production call sites of append_journal_transaction.
# (The definition site itself is excluded structurally.)
RAW_APPENDER_ALLOWLIST = {
    ("src/workflowy_mcp/coherence/barrier.py", "seal_verified_journal_transaction"),
}

# H1(a): exact function-scoped state-management entries permitted to read
# ``.dirty_status`` directly, with the reason each is sanctioned.
DIRTY_STATUS_READ_ALLOWLIST = {
    ("src/workflowy_mcp/coherence/schema.py", "CoherenceManifest.__post_init__"): "schema validation",
    ("src/workflowy_mcp/coherence/schema.py", "CoherenceManifest.to_dict"): "serialization",
    ("src/workflowy_mcp/coherence/barrier.py", "_compose_boundary_status"): "the boundary-status authority",
    ("src/workflowy_mcp/coherence/barrier.py", "_raise_global_dirty_barrier_locked"): "barrier publication (the ONE locked body, both facades)",
    ("src/workflowy_mcp/coherence/epoch_publisher.py", "publish_epoch"): "S02 clearing + A3 publication-time carry-forward",
    ("src/workflowy_mcp/coherence/refresh_authority.py", "_required_generations"): "S02 cut computation",
    ("src/workflowy_mcp/coherence/refresh_authority.py", "_acquire_or_join"): "S02 cut minting",
}


def _production_files() -> list[Path]:
    files = [p for p in SRC.rglob("*.py") if p.is_file()]
    files.extend(p for p in ROOT_MODULES if p.is_file())
    return files


class _FunctionMapper(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.map: dict[int, str] = {}

    def _visit_fn(self, node: ast.AST) -> None:
        name = getattr(node, "name", "<fn>")
        self.stack.append(name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self.stack.pop()

    visit_FunctionDef = _visit_fn
    visit_AsyncFunctionDef = _visit_fn
    visit_ClassDef = _visit_fn

    def generic_visit(self, node: ast.AST) -> None:
        lineno = getattr(node, "lineno", None)
        if lineno is not None and self.stack:
            self.map[lineno] = ".".join(self.stack)
        super().generic_visit(node)


def _scan_raw_appender(rel: str, text: str) -> list[str]:
    tree = ast.parse(text, filename=rel)
    mapper = _FunctionMapper()
    mapper.visit(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else ""
        )
        if name != "append_journal_transaction":
            continue
        fn = mapper.map.get(node.lineno, "<module>")
        if (rel, fn) in RAW_APPENDER_ALLOWLIST:
            continue
        violations.append(f"{rel}:{node.lineno} in {fn}")
    return violations


def _scan_dirty_status(rel: str, text: str) -> list[str]:
    tree = ast.parse(text, filename=rel)
    mapper = _FunctionMapper()
    mapper.visit(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "dirty_status":
            continue
        fn = mapper.map.get(node.lineno, "<module>")
        if (rel, fn) in DIRTY_STATUS_READ_ALLOWLIST:
            continue
        violations.append(f"{rel}:{node.lineno} in {fn}")
    return violations


def _violations(scanner) -> list[str]:
    out: list[str] = []
    for path in _production_files():
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        out.extend(scanner(rel, path.read_text(encoding="utf-8")))
    return out


def test_no_production_raw_appender_bypass():
    """H3: the sealer is the ONLY production journal-publication door."""

    assert _violations(_scan_raw_appender) == []


def test_raw_appender_fence_catches_injected_bypass(tmp_path):
    fixture = tmp_path / "rogue.py"
    fixture.write_text(
        "def rogue(paths, **kwargs):\n"
        "    return append_journal_transaction(paths, **kwargs)\n",
        encoding="utf-8",
    )
    violations = _scan_raw_appender("src/workflowy_mcp/rogue.py", fixture.read_text(encoding="utf-8"))
    assert violations, "raw-appender fence missed an injected bypass"


def test_no_unregistered_dirty_status_direct_reads():
    """H1(a): every hard-stop decision goes through the ONE predicate."""

    assert _violations(_scan_dirty_status) == []


def test_dirty_status_fence_catches_injected_direct_read(tmp_path):
    fixture = tmp_path / "rogue.py"
    fixture.write_text(
        "def rogue(manifest):\n"
        "    return manifest.dirty_status.active\n",
        encoding="utf-8",
    )
    violations = _scan_dirty_status("src/workflowy_mcp/rogue.py", fixture.read_text(encoding="utf-8"))
    assert violations, "dirty-status fence missed an injected direct read"


# ---------------------------------------------------------------------------
# H4(a) closure — exact write_manifest caller + CoherenceManifest construction
# registries (S04 Remediation Chain 3, work-order item 3)
# ---------------------------------------------------------------------------
#
# The S02 fence (test_fence_single_epoch_writer) scans only
# manifest_with_updates calls carrying lineage kwargs; a DIRECT
# CoherenceManifest / write_manifest writer evades it.  This closure records
# every legitimate write_manifest caller by file + function + permitted
# field role, and every legitimate direct CoherenceManifest construction
# site, so any new caller/construction fails until explicitly classified.
# Cheap AST scanning only — deliberately NO interprocedural taint analysis:
# the field-role check is enforced on manifest_with_updates kwargs inside
# each registered function (the mechanical proxy for the permitted role).

#: Every legitimate production write_manifest caller, with its permitted
#: field role.  ``permitted_fields=None`` marks the sole epoch-lineage
#: publisher (unrestricted).  Any other caller is a violation; a registered
#: entry with no call site is a stale-registry violation.
WRITE_MANIFEST_CALLER_REGISTRY = {
    (
        "src/workflowy_mcp/coherence/barrier.py",
        "_raise_global_dirty_barrier_locked",
    ): {
        "role": "barrier-only write (the ONE locked body shared by both facades)",
        "permitted_fields": frozenset({"dirty_status"}),
    },
    (
        "src/workflowy_mcp/coherence/journal_store.py",
        "append_journal_transaction",
    ): {
        "role": "journal-head-only write",
        "permitted_fields": frozenset({"journal_head_seq"}),
    },
    (
        "src/workflowy_mcp/coherence/journal_store.py",
        "_recover_orphans_locked",
    ): {
        "role": "journal-head-only write",
        "permitted_fields": frozenset({"journal_head_seq"}),
    },
    (
        "src/workflowy_mcp/coherence/epoch_publisher.py",
        "publish_epoch",
    ): {
        "role": "the sole epoch-lineage publisher",
        "permitted_fields": None,
    },
}

#: Every legitimate direct CoherenceManifest construction site.  Bootstrap
#: is the narrowly documented legitimate lineage construction; the schema
#: internals are deserialization / the validated pure updater.
COHERENCE_MANIFEST_CONSTRUCTION_REGISTRY = {
    (
        "src/workflowy_mcp/coherence/schema.py",
        "new_bootstrap_manifest",
    ): "bootstrap construction",
    (
        "src/workflowy_mcp/coherence/refresh_authority.py",
        "bootstrap_coherence_from_legacy",
    ): "bootstrap-from-legacy construction (epoch-0 transition lineage)",
    (
        "src/workflowy_mcp/coherence/schema.py",
        "CoherenceManifest.from_dict",
    ): "deserialization of existing durable state",
    (
        "src/workflowy_mcp/coherence/schema.py",
        "manifest_with_updates",
    ): "the validated pure updater",
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _scan_write_manifest_calls(rel: str, text: str, seen: set | None = None) -> list[str]:
    """Every production write_manifest call must be a registry member, and a
    member's manifest_with_updates kwargs must stay inside its permitted
    field role."""
    tree = ast.parse(text, filename=rel)
    mapper = _FunctionMapper()
    mapper.visit(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "write_manifest":
            fn = mapper.map.get(node.lineno, "<module>")
            entry = WRITE_MANIFEST_CALLER_REGISTRY.get((rel, fn))
            if entry is None:
                violations.append(
                    f"{rel}:{node.lineno} in {fn}: unregistered write_manifest caller"
                )
            elif seen is not None:
                seen.add((rel, fn))
        elif name == "manifest_with_updates":
            fn = mapper.map.get(node.lineno, "<module>")
            entry = WRITE_MANIFEST_CALLER_REGISTRY.get((rel, fn))
            if entry is None:
                continue  # non-writer functions are the S02 fence's concern
            permitted = entry["permitted_fields"]
            if permitted is None:
                continue  # the sole epoch-lineage publisher is unrestricted
            used = {kw.arg for kw in node.keywords if kw.arg is not None}
            overreach = sorted(used - permitted)
            if overreach:
                violations.append(
                    f"{rel}:{node.lineno} in {fn}: {entry['role']} may not write "
                    f"fields {overreach} (permitted: {sorted(permitted)})"
                )
    return violations


def _scan_manifest_constructions(rel: str, text: str, seen: set | None = None) -> list[str]:
    """Every direct CoherenceManifest construction (Name or cls(...) inside
    the CoherenceManifest class) must be a registry member."""
    tree = ast.parse(text, filename=rel)
    mapper = _FunctionMapper()
    mapper.visit(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        fn = mapper.map.get(node.lineno, "<module>")
        is_construction = name == "CoherenceManifest" or (
            name == "cls" and fn.startswith("CoherenceManifest.")
        )
        if not is_construction:
            continue
        if (rel, fn) in COHERENCE_MANIFEST_CONSTRUCTION_REGISTRY:
            if seen is not None:
                seen.add((rel, fn))
            continue
        violations.append(
            f"{rel}:{node.lineno} in {fn}: unregistered CoherenceManifest "
            "construction (bootstrap is the only legitimate lineage construction)"
        )
    return violations


def test_write_manifest_caller_closure_current_tree():
    """Required test (a): the current production tree has EXACTLY the
    expected write_manifest caller set (and every registered caller is
    still present — no stale registry entries), with field roles honored."""
    seen: set = set()
    violations: list[str] = []
    for path in _production_files():
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        violations.extend(
            _scan_write_manifest_calls(rel, path.read_text(encoding="utf-8"), seen)
        )
    assert violations == []
    missing = set(WRITE_MANIFEST_CALLER_REGISTRY) - seen
    assert missing == set(), f"stale write_manifest registry entries: {missing}"


def test_manifest_construction_registry_current_tree():
    """Companion to (a): the current production tree has EXACTLY the
    expected CoherenceManifest construction set."""
    seen: set = set()
    violations: list[str] = []
    for path in _production_files():
        rel = str(path.relative_to(REPO)).replace("\\", "/")
        violations.extend(
            _scan_manifest_constructions(rel, path.read_text(encoding="utf-8"), seen)
        )
    assert violations == []
    missing = set(COHERENCE_MANIFEST_CONSTRUCTION_REGISTRY) - seen
    assert missing == set(), f"stale construction registry entries: {missing}"


def test_write_manifest_fence_catches_injected_rogue_writer(tmp_path):
    """Required test (b): an injected rogue write_manifest epoch writer is
    detected."""
    fixture = tmp_path / "rogue.py"
    fixture.write_text(
        "def rogue(paths, manifest):\n"
        "    updated = manifest_with_updates(manifest, epoch_number=7, epoch_id='f' * 32)\n"
        "    return write_manifest(paths, updated)\n",
        encoding="utf-8",
    )
    violations = _scan_write_manifest_calls(
        "src/workflowy_mcp/rogue.py", fixture.read_text(encoding="utf-8")
    )
    assert violations, "write_manifest caller closure missed an injected rogue writer"


def test_write_manifest_fence_catches_field_role_violation(tmp_path):
    """A REGISTERED caller exceeding its permitted field role (a barrier-only
    writer touching journal lineage) is detected."""
    fixture = tmp_path / "barrier.py"
    fixture.write_text(
        "def raise_global_dirty_barrier(paths, manifest):\n"
        "    updated = manifest_with_updates(manifest, journal_head_seq=9)\n"
        "    return write_manifest(paths, updated)\n",
        encoding="utf-8",
    )
    violations = _scan_write_manifest_calls(
        "src/workflowy_mcp/coherence/barrier.py", fixture.read_text(encoding="utf-8")
    )
    assert violations, "field-role violation inside a registered caller went undetected"


def test_manifest_construction_fence_catches_injected_rogue_construction(tmp_path):
    """Required test (c): an injected direct CoherenceManifest epoch
    construction outside bootstrap is detected."""
    fixture = tmp_path / "rogue.py"
    fixture.write_text(
        "def rogue(account):\n"
        "    return CoherenceManifest(\n"
        "        schema_version=1, account_scope_id=account, epoch_number=3,\n"
        "        epoch_id='e' * 32,\n"
        "    )\n",
        encoding="utf-8",
    )
    violations = _scan_manifest_constructions(
        "src/workflowy_mcp/rogue.py", fixture.read_text(encoding="utf-8")
    )
    assert violations, "construction registry missed an injected rogue construction"


def _legacy_lineage_kwarg_scan(rel: str, text: str) -> list[str]:
    """The S02 single-epoch-writer scan, verbatim (retained in
    test_coherence_s02_carto_lineage.py): manifest_with_updates lineage
    kwargs outside epoch_publisher.py."""
    lineage_kwargs = {
        "epoch_number",
        "epoch_id",
        "base_snapshot_path",
        "base_incorporated_through_seq",
        "journal_floor_seq",
    }
    violations: list[str] = []
    tree = ast.parse(text, filename=rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "manifest_with_updates":
            continue
        used = {kw.arg for kw in node.keywords if kw.arg in lineage_kwargs}
        if used and not rel.endswith("coherence/epoch_publisher.py"):
            violations.append(f"{rel}: manifest_with_updates({sorted(used)})")
    return violations


def test_old_lineage_scan_alone_misses_rogue_write_manifest(tmp_path):
    """Required test (d) — mutation sensitivity: the OLD scanner alone
    misses at least one rogue form (a direct write_manifest caller whose
    manifest was NOT built via manifest_with_updates); the new closure
    catches it."""
    fixture = tmp_path / "rogue.py"
    fixture.write_text(
        "def rogue(paths, manifest):\n"
        "    return write_manifest(paths, manifest)\n",
        encoding="utf-8",
    )
    rel = "src/workflowy_mcp/rogue.py"
    text = fixture.read_text(encoding="utf-8")
    assert _legacy_lineage_kwarg_scan(rel, text) == [], (
        "the old lineage-kwarg scan unexpectedly caught the rogue form"
    )
    assert _scan_write_manifest_calls(rel, text), (
        "the caller closure missed the rogue form the old scan let through"
    )
