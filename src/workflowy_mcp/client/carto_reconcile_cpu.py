"""Data-only H reconciliation decisions, advanced by the owning loop driver.

No client, publisher, registry, task or arbitrary callback lives in this module.
The generator is exclusively owned and advanced sequentially in empty-Context
default-executor calls. CREATE consumes verified order; later phase policies
and the dry-run plan shape remain unchanged.
"""
from __future__ import annotations

from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterable, Union


Node = Dict


@dataclass(frozen=True)
class ReconcileCreateRun:
    parent_uuid: str
    items: tuple[dict, ...]
    placement: str
    predecessor_uuid: str | None = None
    parent_created: bool = False


@dataclass(frozen=True)
class ReconcileCreateResult:
    created_ids: tuple[str, ...]
    physical_placements: tuple[str, ...]
    ordered_child_ids: tuple[str, ...] = ()
    priority_by_id: dict = field(default_factory=dict)
    reorder_required: bool = False
    outcome: str = "v1_boundary"


# @beacon[
#   id=mci-n04@adopt-create-order,
#   slice_labels=ra-reconcile,
#   kind=ast,
#   comment=Adopts whole verified sibling order or actual v1 effects as each run-prefix row enters the private maps; imports no GUI content and publishes no predicted priorities.,
# ]
def _adopt_create_order(target, children, parent, result):
    """Adopt observed order or actual boundary effects for the installed run prefix."""
    tracked = children[parent]
    if result.ordered_child_ids:
        # Do not import GUI additions as tracked content/delete candidates.
        order = [identifier for identifier in result.ordered_child_ids if identifier in tracked]
        order.extend(identifier for identifier in tracked if identifier not in order)
    else:
        priorities = {}
        for identifier in tracked:
            value = target[identifier].get("priority")
            if value is None:
                value = (target[identifier].get("data") or {}).get("priority")
            try:
                priorities[identifier] = int(value)
            except (TypeError, ValueError):
                priorities[identifier] = 0
        order = sorted(tracked, key=priorities.get)
        for identifier, placement in zip(result.created_ids, result.physical_placements):
            if identifier not in order:
                # A run's verified prefix is installed one source row at a time.
                continue
            order.remove(identifier)
            order.insert(0 if placement == "top" else len(order), identifier)
    tracked[:] = order
    # Private ranks only; never server priority predictions or cache writes.
    for rank, identifier in enumerate(order):
        target[identifier] = {**dict(target[identifier]), "priority": rank}


@dataclass(frozen=True)
class ReconcileRequest:
    kind: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReconcileStep:
    next_request: ReconcileRequest | None = None
    diagnostics: tuple = ()
    done: bool = False
    result: Any = None


@dataclass(frozen=True)
class CartoDiagnosticSink:
    """Explicit paths and an owned trace buffer; no callback or live job/cache.

    A daemon-wide debug_path (``logs/reconcile_debug.log``) is written through
    the shared 10 MiB/five-generation rotation gate; job/file paths retain the
    original bytes, including each event's unconditional trailing newline.
    R-a buffers only this file's trace, while journal entries remain immediate;
    the joined owner keeps that trace only for changed/failed/interrupted work.
    """

    debug_path: Path | None = None
    journal_path: Path | None = None
    trace_buffer: list[str] | None = None

    def append_batch(self, events):
        # Return diagnostic failure as data so the original generator handler
        # sees it without latching a best-effort journal failure as job failure.
        try:
            self._append_batch(events)
        except Exception as exc:
            return exc
        return None

    def _append_batch(self, events):
        from ..debug_log_rotation import append_debug_line
        import json
        for kind, value, timestamp in events:
            if kind == "log" and self.debug_path is not None:
                line = f"[{timestamp}] {value}\n"
                if self.trace_buffer is None:
                    append_debug_line(self.debug_path, line)
                else:
                    self.trace_buffer.append(line)
            elif kind == "journal" and self.journal_path is not None:
                with open(self.journal_path, encoding="utf-8") as stream:
                    journal = json.load(stream)
                journal.setdefault("entries", []).append(value)
                with open(self.journal_path, "w", encoding="utf-8") as stream:
                    json.dump(journal, stream, indent=2, ensure_ascii=False)

    def finish_trace(self, keep):
        """Join one file's buffered trace once, or drop it; failures stay data."""
        from ..debug_log_rotation import append_debug_line
        try:
            if keep and self.trace_buffer and self.debug_path is not None:
                append_debug_line(self.debug_path, "".join(self.trace_buffer))
        except Exception as exc:
            return exc
        finally:
            if self.trace_buffer is not None:
                self.trace_buffer.clear()
        return None


def _reset_reconcile_debug(path):
    from ..debug_log_rotation import reset_debug_log
    # Preserve legacy truncate-before-first-log semantics, but off-loop; the
    # shared gate truncates a daemon-wide log under the rotation lock.
    reset_debug_log(path)


# @beacon[
#   id=carto-runtime@advance-reconcile,
#   slice_labels=ra-reconcile,ra-carto-jobs,
#   kind=ast,
#   comment=Advances one exclusively owned data generator by send or throw off-loop and packages StopIteration without leaking it through an executor Future; diagnostic events and the next typed request return to the original loop driver in H order.,
# ]
def _advance_reconcile_steps(steps, response=None, error=None):
    try:
        request = steps.throw(error) if error is not None else steps.send(response)
    except StopIteration as done:
        return ReconcileStep(done=True, result=done.value)
    if request.kind == "diagnostic":
        return ReconcileStep(diagnostics=(request.args,))
    return ReconcileStep(next_request=request)


def _reconcile_steps(
    source_json: Union[List[Node], Dict], parent_uuid: str,
    import_policy: str = 'strict', dry_run: bool = False,
    max_delete_threshold: Optional[int] = None, force: bool = False,
    skip_delete_bulk_export_wait: bool = False, allow_pure_reorder: bool = False,
    cancel_check_interval: int = 25, *, export_nodes: bool = False,
    cancel_callback: bool = False, log_weave_entry: bool = False,
):
    """H's data/decision body; the last three flags indicate capability only."""
    # Record start time of this WEAVE for rate-limit aware phases (e.g., DELETE)
    weave_start_time = datetime.now()

    def log(msg):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        yield ReconcileRequest("diagnostic", ("log", msg, timestamp))

    def journal(entry: Dict[str, Any]) -> None:
        """Write a single per-node weave journal entry (best-effort).

        Each entry carries a resumability_safe flag so that if the process is
        interrupted between operations, the last line clearly indicates whether
        the last *completed* node-level operation left the weave in a state that
        is safe to resume.
        """
        if not log_weave_entry:
            return
        try:
            e = dict(entry)
            e.setdefault("timestamp", datetime.now().isoformat())
            yield ReconcileRequest("diagnostic", ("journal", e, None))
        except Exception as e2:  # noqa: BLE001
            # Journal failures must never break reconciliation; log and continue.
            (yield from log(f"   [WARN] Failed to append weave journal entry: {type(e2).__name__}: {e2}"))

    # ------------------ cancellation (best-effort, co-operative) ------------------
    # For long WEAVEs / CARTO_REFRESH refreshes, the caller may provide a
    # cancel_callback (e.g. reading a CARTO job JSON that can be toggled to
    # status='cancelled'). We must check this callback not only between files,
    # but also during the long CREATE/MOVE/REORDER/UPDATE/DELETE apply loops.

    _cancel_check_counter = 0
    _cancel_requested = False

    # @beacon[
    #   id=carto-cancel@reconcile_tree._check_cancel,
    #   role=reconcile_tree._check_cancel,
    #   slice_labels=ra-carto-jobs,ra-reconcile,
    #   kind=ast,
    #   comment=Preserves the original counter force and named phase probes while yielding each actual user-cancel callback request to the owning loop between sends; no callback or job control enters the data generator.,
    # ]
    def _check_cancel(*, force: bool = False, where: str = "") -> bool:
        """Return True if cancellation is requested.

        Cancellation is cooperative/best-effort:
        - We check at a configurable interval to avoid excessive IO (job JSON reads).
        - If the callback raises, we fail-open (treat as not cancelled) and keep going.
        """
        nonlocal _cancel_check_counter, _cancel_requested

        if _cancel_requested:
            return True
        if not cancel_callback:
            return False

        _cancel_check_counter += 1
        if (not force) and cancel_check_interval and (
            _cancel_check_counter % int(cancel_check_interval) != 0
        ):
            return False

        try:
            # @beacon[
            #   id=carto-cancel@reconcile_tree.cancel_callback_invoke,
            #   role=reconcile_tree data probe yields the loop-side cancel request,
            #   slice_labels=ra-carto-jobs,ra-reconcile,
            #   kind=span,
            # ]
            if (yield ReconcileRequest("cancel_check")):
                _cancel_requested = True
                # @beacon-close[
                #   id=carto-cancel@reconcile_tree.cancel_callback_invoke,
                # ]
                msg = "Cancellation requested; stopping reconcile_tree early"
                if where:
                    msg += f" ({where})"
                (yield from log(f"   [CANCEL] {msg}"))
                (yield from journal(
                    {
                        "op": "cancel",
                        "status": "requested",
                        "where": where,
                        "resumability_safe": False,
                        "message": msg,
                    }
                ))
                return True
        except Exception as e:  # noqa: BLE001
            # Fail-open: cancellation is best-effort. If the callback errors,
            # log but proceed rather than risking a partial apply due to a
            # transient read error.
            (yield from log(f"   [WARN] cancel_callback raised: {type(e).__name__}: {e}"))

        return False

    # Planning containers for summary and dry-run plan
    planned_creates: List[Dict[str, Any]] = []
    planned_moves: List[Dict[str, Any]] = []
    planned_reorders: List[Dict[str, Any]] = []
    planned_updates: List[Dict[str, Any]] = []
    planned_delete_roots: List[str] = []

    def build_plan(summary_only: bool = False) -> Dict[str, Any]:
        plan = {
            'settings': {
                'import_policy': import_policy,
                'dry_run': dry_run,
                'parent_uuid': parent_uuid,
                'intended_parent': intended_parent,
                'max_delete_threshold': max_delete_threshold,
                'force': force,
                'option_b_enabled': option_b_enabled,
                'original_ids_seen_count': len(original_ids_seen),
            },
            'counts': {
                'creates': len(planned_creates),
                'moves': len(planned_moves),
                'reorders': len(planned_reorders),
                'updates': len(planned_updates),
                'deletes': len(planned_delete_roots),
            },
            'protected_parent_refs': sorted(list(parent_refs)) if 'parent_refs' in locals() else [],
        }
        if not summary_only:
            plan.update({
                'creates': planned_creates,
                'moves': planned_moves,
                'reorders': planned_reorders,
                'updates': planned_updates,
                'delete_roots': planned_delete_roots,
            })
        return plan

    def log_summary_and_close(note: Optional[str] = None) -> Optional[Dict[str, Any]]:
        plan = build_plan()
        (yield from log("\n[SUMMARY]"))
        if note:
            (yield from log(f"   Note: {note}"))
        (yield from log(f"   Policy: {import_policy}, Dry-run: {dry_run}, Force: {force}"))
        (yield from log(f"   Parent (effective): {parent_uuid} | Intended: {intended_parent}"))
        (yield from log(f"   Protected parent refs: {plan['protected_parent_refs']}"))
        for k, v in plan['counts'].items():
            (yield from log(f"   {k.capitalize()}: {v}"))
        return plan if dry_run else None

    # Normalize input: accept either list of nodes or dict with metadata + nodes
    source_nodes: List[Node]
    file_root: Optional[str] = None

    # Option B deletion metadata: track node IDs that were present in the
    # "original" source JSON before edits. This enables a visible-then-removed
    # distinction under truncated parents. When not provided, we fall back to
    # the conservative Option A behavior.
    original_ids_seen = set()
    option_b_enabled = False
    explicitly_preserved_ids = set()

    if isinstance(source_json, dict):
        file_root = source_json.get('export_root_id') or source_json.get('root_id') or source_json.get('parent_id')
        source_nodes = source_json.get('nodes', [])

        # Root-level children_status metadata (analogous to node.children_status).
        # When non-'complete', the reconciliation root is treated as truncated.
        root_children_status = source_json.get('export_root_children_status')

        # Optional Option B deletion ledger - IDs that were present in the
        # original source JSON before edits. This lets us distinguish
        # "visible-then-removed" from "never loaded" under truncated parents.
        original_ids_seen = set(source_json.get('original_ids_seen', []))
        option_b_enabled = len(original_ids_seen) > 0

        # Optional identity-seeding ambiguity protection: node IDs that
        # Cartographer identity seeding could not match unambiguously (and, for
        # preserved subtrees, all their descendants). These IDs – and their
        # ancestors – must never be deleted even though they are missing from
        # the source nodes list.
        explicitly_preserved_ids = set(source_json.get('explicitly_preserved_ids', []) or [])

        (yield from log(f"Detected source document with export_root_id={file_root}"))
        (yield from log(f"   Root children_status: {root_children_status!r}"))
        if option_b_enabled:
            (yield from log(f"   Option B deletion ledger: {len(original_ids_seen)} originally-seen node IDs"))
        else:
            (yield from log("   No original_ids_seen ledger; using conservative Option A deletion semantics"))
        if explicitly_preserved_ids:
            (yield from log(f"   explicitly_preserved_ids present: {len(explicitly_preserved_ids)} IDs marked as identity-seeded ambiguity protection"))
        else:
            (yield from log("   No explicitly_preserved_ids present; DELETE phase will treat all missing nodes via Option A/B only"))
    else:
        source_nodes = source_json
        root_children_status = 'complete'
        (yield from log("Detected bare source node list (no document metadata)"))

    # Infer intended parent if not explicitly provided in file
    def infer_intended_parent(nodes: List[Node]) -> Optional[str]:
        parents = {n.get('parent_id') or n.get('parentId') for n in nodes if isinstance(n, dict) and 'parent_id' in n or 'parentId' in n}
        parents = {p for p in parents if p is not None}
        return next(iter(parents)) if len(parents) == 1 else None

    intended_parent = file_root or infer_intended_parent(source_nodes)
    if intended_parent:
        if intended_parent != parent_uuid:
            (yield from log(f"PARENT MISMATCH detected: intended_parent={intended_parent} (from file), provided parent_uuid={parent_uuid}"))
            if import_policy == 'strict':
                (yield from log("Aborting due to import_policy='strict'. No changes made."))
                return (yield from log_summary_and_close(note="Strict parent mismatch abort"))
            elif import_policy == 'rebase':
                (yield from log("Rebasing to file's export root (import_policy='rebase'). Overriding parent_uuid."))
                parent_uuid = intended_parent
            elif import_policy == 'clone':
                (yield from log("Clone mode: will strip IDs and skip DELETE phase (import_policy='clone')."))
            else:
                (yield from log(f"Unknown import_policy '{import_policy}', defaulting to 'strict' abort."))
                return (yield from log_summary_and_close(note="Unknown policy; abort"))
    else:
        (yield from log("No explicit export_root_id detected; proceeding with provided parent_uuid."))

    # In clone mode: strip IDs from source and mark skip_delete
    skip_delete = (import_policy == 'clone')

    def strip_ids(nodes: List[Node]):
        for n in nodes:
            if 'id' in n:
                del n['id']
            strip_ids(n.get('children', []))

    if skip_delete:
        strip_ids(source_nodes)

    # ------------- helpers -------------
    # @beacon[
    #   id=auto-beacon@reconcile_tree.index_source-jaao,
    #   role=reconcile_tree.index_source,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def index_source(nodes: List[Node], parent_id: Optional[str] = None, order_acc: Optional[List[Node]] = None) -> List[Node]:
        # Preorder traversal; annotate desired parent and index
        if order_acc is None:
            order_acc = []
        for i, n in enumerate(nodes):
            n['_desired_parent'] = parent_id
            n['_desired_index'] = i
            order_acc.append(n)
            index_source(n.get('children', []), n.get('id'), order_acc)
        return order_acc

    # @beacon[
    #   id=auto-beacon@reconcile_tree.snapshot_target-es3t,
    #   role=reconcile_tree.snapshot_target,
    #   comment=Mechanically retained H decision body now advanced only in the data worker; typed requests return original loop-side results or exceptions in order.,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def snapshot_target(root_parent: str, use_efficient_traversal: bool = False, export_func = None):
        # Snapshot existing subtree under root_parent to map nodes
        Map_T: Dict[str, Node] = {}
        Parent_T: Dict[str, Optional[str]] = {}
        Children_T: Dict[str, List[str]] = defaultdict(list)

        if use_efficient_traversal:
            # BFS traversal using list_nodes (CAUSES RATE LIMITS - avoid!)
            q: deque[str] = deque([root_parent])
            visited_parents: set[str] = set()
            while q:
                p = q.popleft()
                if p in visited_parents:
                    continue
                visited_parents.add(p)
                children = (yield ReconcileRequest('list', (p,), {}))
                for ch in children:
                    cid = ch['id']
                    Map_T[cid] = ch
                    Parent_T[cid] = p
                    Children_T[p].append(cid)
                    q.append(cid)
        else:
            # RECOMMENDED: Single bulk export (empirically better, avoids rate limits)
            (yield from log(f"   Using bulk export for snapshot (avoids rate limit issues)"))

            if not export_func:
                (yield from log(f"   WARNING: export_nodes not provided, falling back to BFS list_nodes (slow!)"))
                q: deque[str] = deque([root_parent])
                visited_parents: set[str] = set()
                while q:
                    p = q.popleft()
                    if p in visited_parents:
                        continue
                    visited_parents.add(p)
                    children = (yield ReconcileRequest('list', (p,), {}))
                    for ch in children:
                        cid = ch['id']
                        Map_T[cid] = ch
                        Parent_T[cid] = p
                        Children_T[p].append(cid)
                        q.append(cid)
            else:
                # Use bulk export - single API call!
                (yield from log(f"   Calling export_nodes({root_parent})..."))
                raw_data = (yield ReconcileRequest('export', (root_parent,), {}))
                all_nodes = raw_data.get('nodes', [])
                (yield from log(f"   Bulk export returned {len(all_nodes)} nodes"))

                # Build parent/children maps from flat list, EXCLUDING the root itself
                for node in all_nodes:
                    nid = node['id']
                    if nid == root_parent:
                        continue  # never include the reconciliation root in snapshot maps
                    Map_T[nid] = node
                    parent_id = node.get('parent_id') or node.get('parentId')
                    Parent_T[nid] = parent_id
                    if parent_id is not None:
                        Children_T[parent_id].append(nid)

        # IMPORTANT: Workflowy /nodes-export does not guarantee that the flat
        # node list is ordered by on-screen sibling order. The visual ordering
        # is carried by each node's priority field.
        #
        # If we do not sort Children_T by priority here, reconcile_tree may
        # mis-detect "pure reorders" and spam MOVE operations on every run,
        # even when the only real change is a simple UPDATE.
        # @beacon[
        #   id=auto-beacon@reconcile_tree.snapshot_target._priority_of-ysdx,
        #   role=reconcile_tree.snapshot_target._priority_of,
        #   slice_labels=ra-reconcile,
        #   kind=ast,
        # ]
        def _priority_of(cid: str) -> int:
            node = Map_T.get(cid) or {}
            p = node.get("priority")
            if p is None:
                # Some snapshots use short keys; be defensive.
                p = node.get("pr")
            try:
                return int(p) if p is not None else 10**12
            except Exception:
                return 10**12

        for pid, kids in list(Children_T.items()):
            pos_map = {cid: i for i, cid in enumerate(kids)}
            kids.sort(key=lambda cid, _pos=pos_map: (_priority_of(cid), _pos.get(cid, 10**12)))
            Children_T[pid] = kids

        return Map_T, Parent_T, Children_T

    # @beacon[
    #   id=auto-beacon@reconcile_tree.desired_maps-sss8,
    #   role=reconcile_tree.desired_maps,
    #   comment=Mechanically retained H decision body now advanced only in the data worker; typed requests return original loop-side results or exceptions in order.,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def desired_maps(source_nodes_local: List[Node], root_parent: str):
        Map_S: Dict[Optional[str], Node] = {}
        Parent_S: Dict[str, Optional[str]] = {}
        Order_S: Dict[Optional[str], List[Optional[str]]] = defaultdict(list)

        # Set desired parents for top-level to root_parent
        for i, n in enumerate(source_nodes_local):
            n['_desired_parent'] = root_parent
            n['_desired_index'] = i

        seq = index_source(source_nodes_local, root_parent)  # fills _desired_parent/_desired_index recursively

        # Defensive: source JSON must never contain duplicate Workflowy UUIDs.
        # However, certain Cartographer refresh edge cases can accidentally
        # duplicate an existing UUID with a different name/note (e.g. when
        # identity matching is ambiguous). If we allow duplicates into Order_S,
        # the REORDER phase will spam MOVE operations (often moving the same node
        # multiple times).
        seen_ids: set[str] = set()

        for n in seq:
            nid = n.get('id')
            p = n.get('_desired_parent')

            # Always record the node in Map_S (last write wins) so UPDATE phase
            # has access to the latest desired content.
            Map_S[nid] = n  # nid can be None for new nodes

            if nid is not None:
                nid_str = str(nid)
                if nid_str in seen_ids:
                    # Skip duplicate ID for ordering/moves; keep only first occurrence.
                    (yield from log(f"   WARNING: duplicate UUID in source JSON: {nid_str} (skipping duplicate for ordering)"))
                    continue
                seen_ids.add(nid_str)
                Parent_S[nid_str] = p

            if p is not None:
                Order_S[p].append(nid)

        return Map_S, Parent_S, Order_S

    # @beacon[
    #   id=auto-beacon@reconcile_tree.compute_delete_roots-sn31,
    #   role=reconcile_tree.compute_delete_roots,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def compute_delete_roots(to_delete: Iterable[str], Parent_T_map: Dict[str, Optional[str]]):
        to_delete_set = set(to_delete)
        roots: set[str] = set()
        for d in to_delete_set:
            cur = Parent_T_map.get(d)
            is_root = True
            while cur is not None:
                if cur in to_delete_set:
                    is_root = False
                    break
                cur = Parent_T_map.get(cur)
            if is_root:
                roots.add(d)
        return roots

    def normalize_html_entities(s: str | None) -> str | None:
        """Decode a small set of HTML entities for semantic comparison.

        This is the counterpart to the escaping performed by
        _validate_name_field/_validate_note_field and the Workflowy GUI:

        - &amp;  → &
        - &lt;  → <
        - &gt;  → >
        - &quot; → "

        By decoding prior to comparison, we treat source/target notes as equal
        when they differ only by whether characters have been escaped ("->"
        vs "-&gt;", etc.), preventing spurious UPDATE operations.
        """
        if s is None:
            return None
        # Decode the small set of entities we know Workflowy uses.
        html_entity_map = {
            "&amp;": "&",
            "&lt;": "<",
            "&gt;": ">",
            "&quot;": '"',
        }
        for ent, ch in html_entity_map.items():
            if ent in s:
                s = s.replace(ent, ch)
        return s

    # @beacon[
    #   id=auto-beacon@reconcile_tree.node_data_equal-n4rn,
    #   role=reconcile_tree.node_data_equal,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def node_data_equal(src: Optional[Node], tgt: Optional[Node]) -> bool:
        if src is None or tgt is None:
            return False

        # Normalize names via same HTML entity escaping used in _validate_name_field
        src_name = normalize_html_entities(src.get('name'))
        tgt_name = normalize_html_entities(tgt.get('name'))

        # Trim leading/trailing whitespace so harmless spaces do not trigger updates
        def _strip_or_empty(s: str | None) -> str:
            return (s or "").strip()

        same_name = _strip_or_empty(src_name) == _strip_or_empty(tgt_name)

        # Normalize notes via same HTML entity escaping used in _validate_note_field
        src_note = normalize_html_entities(src.get('note'))
        tgt_note = normalize_html_entities(tgt.get('note'))

        def _strip_or_none(s: str | None) -> Optional[str]:
            if s is None:
                return None
            stripped = s.strip()
            return stripped if stripped != "" else None

        same_note = _strip_or_none(src_note) == _strip_or_none(tgt_note)

        same_completed = bool(src.get('completed', False)) == bool(tgt.get('completed', False))

        # Ignore data entirely for equality (layoutMode is invisible to user, not required)
        # If we later use data for other purposes, we can strip layoutMode and compare residual.
        return same_name and same_note and same_completed

    # ------------- phase 0: build maps -------------
    (yield from log(f"\n[PHASE 0] START - Building maps for parent {parent_uuid}"))
    Map_T, Parent_T, Children_T = (yield from snapshot_target(parent_uuid, use_efficient_traversal=False, export_func=export_nodes))
    (yield from log(f"   Target snapshot: {len(Map_T)} nodes found in Workflowy (excluding root)"))
    (yield from log(f"   Target UUIDs: {len(Map_T)} (list suppressed)"))
    Map_S, Parent_S, Order_S = (yield from desired_maps(source_nodes, parent_uuid))
    (yield from log(f"   Source JSON: {len(Map_S)} nodes expected"))
    (yield from log(f"   Source UUIDs: {list(Map_S.keys())}"))
    (yield from log(f"   Source UUIDs that are NOT None: {[k for k in Map_S.keys() if k is not None]}"))
    (yield from log(f"   UUIDs in BOTH: {set(Map_T.keys()) & set(k for k in Map_S.keys() if k is not None)}"))
    uuids_only_in_target = set(Map_T.keys()) - set(k for k in Map_S.keys() if k is not None)
    (yield from log(f"   UUIDs ONLY in Target: {len(uuids_only_in_target)} (list suppressed)"))
    (yield from log(f"   UUIDs ONLY in Source: {set(k for k in Map_S.keys() if k is not None) - set(Map_T.keys())}"))

    # Collect parent_refs to protect during deletion (all parent_id that appear in source)
    # @beacon[
    #   id=auto-beacon@reconcile_tree.preorder-ay4u,
    #   role=reconcile_tree.preorder,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def preorder(nodes: List[Node]):
        for n in nodes:
            yield n
            for c in preorder(n.get('children', [])):
                yield c

    parent_refs: set[str] = set()
    for n in preorder(source_nodes):
        p = n.get('parent_id') or n.get('parentId')
        if p:
            parent_refs.add(p)
    # Always protect the reconciliation root
    parent_refs.add(parent_uuid)
    (yield from log(f"   Protected parent refs (will not delete): {parent_refs}"))

    # Detect parents whose children are not fully loaded in the source JSON.
    #
    # Truncation semantics are carried explicitly by children_status and by
    # subtree shell markers; we no longer infer truncation from numeric counts
    truncated_parents: set[str] = set()
    for nid, node in Map_S.items():
        if nid is None:
            continue

        # children_status is an epistemic flag: if missing, we FAIL-CLOSE and treat as truncated.
        status = node.get('children_status')
        children = node.get('children') or []

        if status is None or status != 'complete':
            truncated_parents.add(nid)
            continue

        # NOTE (Dec 2025): subtree_mode='shell' is an EDITING-SCOPE marker, not an
        # epistemic completeness marker.
        #
        # Deletion safety MUST be governed by children_status + original_ids_seen
        # (Option B) + explicit preservation guardrails.
        #
        # Therefore, WE DO NOT treat subtree_mode='shell' as truncated for deletion
        # gating. If a shell is actually epistemically incomplete, it must carry
        # children_status != 'complete' (or be missing => fail-closed).
        #
        # (Shell may still be used by upstream tooling to render/preview node-only
        # editing slices; WEAVE should not infer completeness from it.)

    # Treat the reconciliation root as truncated when its export_root_children_status
    # is non-'complete', so its direct children obey the same Option B semantics
    # as other truncated parents, without moving the root into the nodes[] array.
    if root_children_status is not None and root_children_status != 'complete':
        truncated_parents.add(parent_uuid)

    if truncated_parents:
        (yield from log(f"   Truncated parents (children not fully loaded in source OR opaque shells): {truncated_parents}"))
    else:
        (yield from log("   No truncated parents detected (all children fully loaded in source, no opaque shells)"))

    # ------------- phase 1: CREATE (top-down) -------------
    create_count = 0

    # @beacon[
    #   id=auto-beacon@reconcile_tree.ensure_created-u4k9,
    #   role=reconcile_tree.ensure_created,
    #   comment=Plans maximal eligible sibling after-runs including long tails before bare boundaries; accounts each verified node and adopts whole observed order without authority in the worker.,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def ensure_created(nodes: List[Node], desired_parent: Optional[str], source_path_prefix: List[int] | None = None):
        """Emit maximal after-runs before bare boundaries, then descend to children."""
        nonlocal create_count
        if source_path_prefix is None:
            source_path_prefix = []

        new_indices = {i for i, node in enumerate(nodes) if not node.get('id') or node['id'] not in Map_T}
        ordinary = lambda node: ((node.get('data') or {}).get('layoutMode') in (None, 'bullets')
                                 and not node.get('_position_hint'))
        after_indices = set()
        for i in sorted(new_indices):
            if not ordinary(nodes[i]):
                continue
            start = i
            while start > 0 and start - 1 in new_indices and ordinary(nodes[start - 1]):
                start -= 1
            before = nodes[start - 1].get('id') if start else None
            later_existing = any(j not in new_indices for j in range(i + 1, len(nodes)))
            end = i + 1
            while end < len(nodes) and end in new_indices and ordinary(nodes[end]):
                end += 1
            long_tail = end == len(nodes) and end - start >= 3
            if before in Map_T and Parent_T.get(before) == desired_parent and (later_existing or long_tail):
                after_indices.add(i)
        # A3(a): one eligible segment precedes every bare-v1 create.
        indices = sorted(after_indices) + [i for i in range(len(nodes)) if i not in after_indices]
        parent_created = desired_parent in created_in_pass
        parent_had_children = bool(Children_T.get(desired_parent))
        leading = []
        pending_results = {}
        if not dry_run:
            for i in range(len(nodes)):
                if i not in new_indices or not (nodes[i].get('_position_hint') == 'top'
                        or (ordinary(nodes[i]) and not parent_created and parent_had_children)):
                    break
                leading.append(i)
            indices = [i for i in indices if i not in leading] + list(reversed(leading))
        for idx in indices:
            n = nodes[idx]
            # @beacon[
            #   id=carto-cancel@reconcile_tree.create_loop_cancel_check,
            #   role=reconcile_tree create loop cancellation check,
            #   slice_labels=ra-carto-jobs,ra-reconcile,
            #   kind=span,
            # ]
            if idx not in pending_results and (yield from _check_cancel(where="create")):
                return
            # @beacon-close[
            #   id=carto-cancel@reconcile_tree.create_loop_cancel_check,
            # ]
            nid = n.get('id')
            node_name = n.get('name', 'unnamed')
            current_path = source_path_prefix + [idx]
            (yield from log(f"   Checking if exists: {nid} (name: {node_name})"))
            if not nid or nid not in Map_T:
                create_count += 1
                pending_create_error = None
                if dry_run:
                    new_id = nid or f"DRYRUN-CREATE-{create_count}"
                    (yield from log(f"      [DRY-RUN] Would CREATE '{node_name}' under parent {desired_parent} -> {new_id}"))
                else:
                    (yield from log(f"      >>> CREATING new node '{node_name}' under parent {desired_parent}"))
                    payload = {
                        'name': n.get('name'),
                        'note': n.get('note'),
                        'data': n.get('data'),
                        'completed': bool(n.get('completed', False)),
                        '_position_hint': n.get('_position_hint'),  # Pass through if present
                        'source_path': tuple(current_path),
                    }
                    (yield from log(f"      >>> Payload prepared: name={payload.get('name')}, note_length={len(payload.get('note') or '')}, data={payload.get('data')}"))

                    if desired_parent is None:
                        (yield from log(f"      >>> ERROR: desired_parent is None for node '{node_name}'"))
                        (yield from log(f"      >>> Node structure: {n}"))
                        raise ValueError(f"Cannot create node '{node_name}' - desired_parent is None. Check parent_id in JSON.")

                    # Journal: node-level CREATE operation started.
                    if idx not in pending_results:
                        (yield from journal(
                        {
                            "op": "create",
                            "status": "started",
                            "source_path": current_path,
                            "node_name": node_name,
                            "node_id": None,
                            "resumability_safe": False,
                            "message": (
                                f"node operation CREATE started for path {current_path} - "
                                "RESUMABILITY UNSAFE"
                            ),
                        }
                        ))

                    try:
                        predecessor = nodes[idx - 1].get('id') if idx else None
                        placement = 'after' if (idx in after_indices and predecessor in Map_T
                            and Parent_T.get(predecessor) == desired_parent and not parent_created) else 'bottom'
                        if payload.get('_position_hint') == 'top':
                            placement = 'top'
                        elif idx in leading:
                            placement = 'top'
                        if idx not in pending_results:
                            run_indices, items = [idx], [payload]
                            if placement == 'after':
                                end = idx + 1
                                while end in after_indices:
                                    sibling = nodes[end]
                                    sibling_path = source_path_prefix + [end]
                                    items.append({'name': sibling.get('name'), 'note': sibling.get('note'),
                                        'data': sibling.get('data'), 'completed': bool(sibling.get('completed', False)),
                                        '_position_hint': sibling.get('_position_hint'), 'source_path': tuple(sibling_path)})
                                    run_indices.append(end)
                                    (yield from journal({'op': 'create', 'status': 'started',
                                        'source_path': sibling_path, 'node_name': sibling.get('name', 'unnamed'),
                                        'node_id': None, 'resumability_safe': False,
                                        'message': f"node operation CREATE started for path {sibling_path} - RESUMABILITY UNSAFE"}))
                                    end += 1
                            run = ReconcileCreateRun(desired_parent, tuple(items), placement,
                                                     predecessor if placement == 'after' else None, parent_created)
                            run_error = None
                            try:
                                created_result = (yield ReconcileRequest('create', (run,), {}))
                            except BaseException as error:
                                created_result = getattr(error, 'reconcile_create_result', None)
                                if created_result is None or not created_result.created_ids:
                                    raise
                                run_error = error
                            for offset, identifier in enumerate(created_result.created_ids):
                                pending_results[run_indices[offset]] = (identifier, created_result,
                                    run_error if offset == len(created_result.created_ids) - 1 else None)
                        new_id, created_result, pending_create_error = pending_results.pop(idx)
                        (yield from log(f"      >>> CREATED with new UUID: {new_id}"))

                        # Journal: CREATE completed.
                        (yield from journal(
                            {
                                "op": "create",
                                "status": "completed",
                                "source_path": current_path,
                                "node_name": node_name,
                                "node_id": new_id,
                                "resumability_safe": True,
                                "message": (
                                    f"node operation CREATE completed for node {new_id} at path {current_path} - "
                                    "RESUMABILITY SAFE"
                                ),
                            }
                        ))

                        n['id'] = new_id
                    except Exception as e:
                        (yield from log(f"      >>> CREATE FAILED for '{node_name}': {type(e).__name__}: {str(e)}"))
                        (yield from log(f"      >>> Node details: id={nid}, desired_parent={desired_parent}"))
                        (yield from log(f"      >>> Full node structure: {n}"))
                        raise

                    nid = new_id
                    # reflect in target maps immediately
                    #
                    Map_T[nid] = {
                        'id': nid,
                        'parent_id': desired_parent,
                        'priority': len(Children_T[desired_parent]),
                        **payload,
                    }
                    Parent_T[nid] = desired_parent
                    if desired_parent is not None:
                        Children_T[desired_parent].append(nid)
                    _adopt_create_order(Map_T, Children_T, desired_parent, created_result)
                    created_in_pass.add(nid)

                # Simulate ID and maps so later phases can plan (both dry-run and real for summary)
                n['id'] = new_id
                planned_creates.append({
                    'id': new_id,
                    'name': node_name,
                    'parent': desired_parent,
                    'source_path': current_path,
                })
                if dry_run:
                    Map_T[n['id']] = {
                        'id': n['id'],
                        'parent_id': desired_parent,
                        'priority': len(Children_T[desired_parent]),
                        'name': n.get('name'),
                        'note': n.get('note'),
                        'data': n.get('data'),
                        'completed': bool(n.get('completed', False)),
                    }
                    Parent_T[n['id']] = desired_parent
                    if desired_parent is not None:
                        Children_T[desired_parent].append(n['id'])
                    _adopt_create_order(Map_T, Children_T, desired_parent,
                        ReconcileCreateResult((new_id,), ('top' if n.get('_position_hint') == 'top' else 'bottom',)))
                    created_in_pass.add(new_id)
                if pending_create_error is not None:
                    raise pending_create_error
            else:
                (yield from log(f"      (already exists, skipping create)"))
        if new_indices and not dry_run:
            yield ReconcileRequest('create_parent_done', (desired_parent,), {})
        # Direct siblings and their parent transaction are complete first.
        for idx, n in enumerate(nodes):
            current_path = source_path_prefix + [idx]
            (yield from ensure_created(n.get('children', []), n.get('id'), current_path))

    (yield from log(f"\n[PHASE 1] START - CREATE (top-down)"))
    if (yield from _check_cancel(force=True, where="before_create_phase")):
        return (yield from log_summary_and_close(note="Cancelled before CREATE phase"))

    created_in_pass = set()
    (yield from ensure_created(source_nodes, parent_uuid))

    if (yield from _check_cancel(force=True, where="after_create_phase")):
        return (yield from log_summary_and_close(note="Cancelled during CREATE phase"))

    (yield from log(f"   CREATE phase complete - created {create_count} new nodes"))

    # Recompute desired maps now that new nodes have IDs
    Map_S, Parent_S, Order_S = (yield from desired_maps(source_nodes, parent_uuid))

    # ------------- phase 2: MOVE (root-to-leaf following source) -------------
    (yield from log(f"\n[PHASE 2] START - MOVE (root-to-leaf)"))

    move_count = 0
    for n in preorder(source_nodes):
        if (yield from _check_cancel(where="move")):
            return (yield from log_summary_and_close(note="Cancelled during MOVE phase"))
        nid = n['id']
        desired_parent = n.get('_desired_parent')
        current_parent = Parent_T.get(nid)
        (yield from log(f"   Checking node {nid} (name: {n.get('name')})"))
        (yield from log(f"      Current parent: {current_parent}"))
        (yield from log(f"      Desired parent: {desired_parent}"))
        if desired_parent != current_parent:
            move_count += 1
            if dry_run:
                (yield from log(f"      [DRY-RUN] Would MOVE {nid} from {current_parent} to {desired_parent}"))
            else:
                # Journal: MOVE operation started (resumability is UNSAFE until
                # the move completes, because the node's parent/position may be
                # mid-transition).
                (yield from journal(
                    {
                        "op": "move",
                        "status": "started",
                        "node_id": nid,
                        "from_parent": current_parent,
                        "to_parent": desired_parent,
                        "resumability_safe": False,
                        "message": (
                            f"node operation MOVE started for node {nid} from {current_parent} "
                            f"to {desired_parent} - RESUMABILITY UNSAFE until move completes"
                        ),
                    }
                ))
                try:
                    (yield from log(f"      >>> MOVING {nid} from {current_parent} to {desired_parent}"))
                    (yield ReconcileRequest('move', (nid, desired_parent), {'position': "bottom"}))
                    (yield from log(f"      >>> MOVE SUCCESS for {nid}"))
                    (yield from journal(
                        {
                            "op": "move",
                            "status": "completed",
                            "node_id": nid,
                            "from_parent": current_parent,
                            "to_parent": desired_parent,
                            "resumability_safe": True,
                            "message": (
                                f"node operation MOVE completed for node {nid} to parent {desired_parent} - "
                                "RESUMABILITY SAFE"
                            ),
                        }
                    ))
                except Exception as e:
                    (yield from log(f"      >>> MOVE FAILED for {nid}: {type(e).__name__}: {str(e)}"))
                    (yield from log(f"      >>> Node name: {n.get('name')}"))
                    raise
            planned_moves.append({'id': nid, 'from': current_parent, 'to': desired_parent})
            # simulate/update maps
            if current_parent and nid in Children_T[current_parent]:
                Children_T[current_parent].remove(nid)
            Parent_T[nid] = desired_parent
            if desired_parent is not None:
                Children_T[desired_parent].append(nid)
        else:
            (yield from log(f"      (no move needed)"))
    (yield from log(f"   MOVE phase complete - moved {move_count} nodes"))

    # ------------- phase 3: REORDER siblings per parent -------------
    (yield from log(f"\n[PHASE 3] START - REORDER siblings"))

    # Optimization: for large WEAVEs (allow_pure_reorder=False), we can safely
    # skip the REORDER phase entirely when there were no creates and no moves in
    # this run, because sibling order is already correct relative to the source
    # JSON. For F12 per-file refresh (allow_pure_reorder=True), we always allow
    # pure reorders when source vs target order differ, even if there were no
    # creates or moves.
    if not allow_pure_reorder and create_count == 0 and move_count == 0:
        (yield from log("   REORDER phase skipped: no creates or moves; sibling order assumed correct (allow_pure_reorder=False)"))
    else:
        reorder_count = 0
        for p, desired in Order_S.items():
            if (yield from _check_cancel(where="reorder")):
                return (yield from log_summary_and_close(note="Cancelled during REORDER phase"))
            if p is None:
                continue
            desired_ids = [x for x in desired if x is not None]
            if not desired_ids:
                continue

            # In full WEAVE mode (allow_pure_reorder=False), we respect
            # truncated_parents and do not attempt to selectively reorder
            # potentially-hidden children. For F12 per-file refresh
            # (allow_pure_reorder=True), we assume Cartographer has a complete
            # view of the file subtree and ignore truncation for ordering.
            if not allow_pure_reorder and p in truncated_parents:
                (yield from log(
                    f"   Parent {p}: children_status != 'complete' in source; "
                    "skipping REORDER to avoid touching hidden children",
                ))
                continue

            # Compare current order vs desired order. If they already match, skip
            # emitting no-op move operations that would just burn rate limit.
            current_children = Children_T.get(p, [])

            # @beacon[
            #   id=auto-beacon@reconcile_tree._priority_of-i27e,
            #   role=reconcile_tree._priority_of,
            #   slice_labels=ra-reconcile,
            #   kind=ast,
            # ]
            def _priority_of(nid: str) -> int:
                node = Map_T.get(nid, {})
                prio = node.get("priority")
                data = node.get("data") or {}
                if prio is None and isinstance(data, dict):
                    prio = data.get("priority")
                # Workflowy priorities are integers; treat anything else as 0
                try:
                    return int(prio)
                except (TypeError, ValueError):
                    return 0

            # Sort children by Workflowy priority so current_ids reflects
            # true visual order in the UI rather than raw export order.
            sorted_child_ids = sorted(current_children, key=_priority_of)
            current_ids = [cid for cid in sorted_child_ids if cid in desired_ids]

            if current_ids == desired_ids:
                (yield from log(f"   Parent {p}: children already in desired order; skipping REORDER"))
                continue

            # ORDER MISMATCH diagnostics (permanent): log both current and
            # desired UUID sequences so we can understand unexpected reorders.
            (yield from log(
                f"   Parent {p}: ORDER MISMATCH – "
                f"current_ids={current_ids}, desired_ids={desired_ids}",
            ))

            (yield from log(f"   Parent {p}: Reordering {len(desired_ids)} children"))
            (yield from log(f"      Desired order (reversed for TOP positioning): {list(reversed(desired_ids))}"))
            for idx, cid in enumerate(reversed(desired_ids)):
                if (yield from _check_cancel(where="reorder")):
                    return (yield from log_summary_and_close(note="Cancelled during REORDER phase"))
                reorder_count += 1
                if dry_run:
                    (yield from log(f"      [DRY-RUN] Would REORDER: move {cid} to TOP of parent {p}"))
                else:
                    try:
                        (yield from log(f"      >>> REORDER: Moving {cid} to TOP of parent {p}"))
                        (yield ReconcileRequest('move', (cid, p), {'position': "top"}))
                        (yield from log(f"      >>> REORDER SUCCESS for {cid}"))
                    except Exception as e:
                        (yield from log(f"      >>> REORDER FAILED for {cid}: {type(e).__name__}: {str(e)}"))
                        raise
                planned_reorders.append({'id': cid, 'parent': p, 'position': 'top', 'sequence_index': idx})
        (yield from log(f"   REORDER phase complete - processed {len(Order_S)} parents, {reorder_count} reorder moves"))

    # ------------- phase 4: UPDATE content -------------
    (yield from log(f"\n[PHASE 4] START - UPDATE content"))
    update_count = 0
    for nid, tgt in list(Map_T.items()):
        if (yield from _check_cancel(where="update")):
            return (yield from log_summary_and_close(note="Cancelled during UPDATE phase"))
        src = Map_S.get(nid)
        if src:
            if not node_data_equal(src, tgt):
                update_count += 1
                payload = {
                    'name': src.get('name'),
                    'note': src.get('note'),
                    'data': src.get('data'),
                    'completed': bool(src.get('completed', False)),
                }

                # DEBUG: Log precise src vs tgt differences driving this UPDATE
                # (data is no longer compared, so we don't log it)
                try:
                    (yield from log(f"      [DEBUG] UPDATE diff for {nid}:"))
                    (yield from log(
                        f"         src.name={src.get('name')!r} | "
                        f"tgt.name={tgt.get('name')!r}"
                    ))
                    (yield from log(
                        f"         src.note={repr(src.get('note'))} | "
                        f"tgt.note={repr(tgt.get('note'))}"
                    ))
                    (yield from log(
                        f"         src.completed={src.get('completed', False)} | "
                        f"tgt.completed={tgt.get('completed', False)}"
                    ))
                except Exception as e:
                    (yield from log(f"      [DEBUG] Failed to log UPDATE diff for {nid}: {type(e).__name__}: {e}"))

                if dry_run:
                    (yield from log(f"      [DRY-RUN] Would UPDATE {nid} -> {payload}"))
                else:
                    (yield from journal(
                        {
                            "op": "update",
                            "status": "started",
                            "node_id": nid,
                            "resumability_safe": False,
                            "message": (
                                f"node operation UPDATE started for node {nid} - "
                                "RESUMABILITY UNSAFE until update completes"
                            ),
                        }
                    ))
                    try:
                        (yield from log(f"      >>> UPDATING node {nid}: name={payload.get('name')}"))
                        (yield ReconcileRequest('update', (nid, payload), {}))
                        (yield from log(f"      >>> UPDATE SUCCESS for {nid}"))
                        (yield from journal(
                            {
                                "op": "update",
                                "status": "completed",
                                "node_id": nid,
                                "resumability_safe": True,
                                "message": (
                                    f"node operation UPDATE completed for node {nid} - "
                                    "RESUMABILITY SAFE"
                                ),
                            }
                        ))
                    except Exception as e:
                        (yield from log(f"      >>> UPDATE FAILED for {nid}: {type(e).__name__}: {str(e)}"))
                        (yield from log(f"      >>> Payload: {payload}"))
                        raise
                planned_updates.append({'id': nid, 'payload': payload})
    (yield from log(f"   UPDATE phase complete - {update_count} updates"))

    # ------------- phase 5: DELETE (delete-roots only) -------------
    (yield from log(f"\n[PHASE 5] START - DELETE (delete-roots only)"))
    if skip_delete:
        (yield from log("   Clone mode active: SKIPPING DELETE phase by design."))
        return (yield from log_summary_and_close(note="Clone mode: no deletes"))

    # Known Workflowy behavior: /nodes-export is limited to ~1 call/minute.
    # We almost always hit rate limits here because a bulk export was already
    # performed at the start of the WEAVE (Phase 0), and DELETE typically
    # begins <60s later. To reduce noisy 429s and make behavior predictable,
    # wait until at least ~65 seconds have elapsed since weave_start_time
    # before issuing the DELETE-phase bulk export.
    #
    # F12 / Cartographer per-file refresh is the *opposite* scenario: human is
    # at the wheel in Workflowy, disk-side edits are local and immediate, and the
    # expectation is fast feedback. When skip_delete_bulk_export_wait=True,
    # we bypass this rate-limit guard entirely and rely on the caller to use
    # cached /nodes-export snapshots appropriately.
    if not skip_delete_bulk_export_wait:
        try:
            min_elapsed = 65.0  # seconds
            now = datetime.now()
            elapsed = (now - weave_start_time).total_seconds()
            if elapsed < min_elapsed:
                wait_seconds = min_elapsed - elapsed
                (yield from log(
                    f"   DELETE phase: waiting {wait_seconds:.1f}s before bulk export "
                    f"to respect Workflowy /nodes-export rate limit (elapsed since WEAVE_START={elapsed:.1f}s)"
                ))
                (yield ReconcileRequest('sleep', (wait_seconds,), {}))
                now2 = datetime.now()
                elapsed2 = (now2 - weave_start_time).total_seconds()
                (yield from log(
                    f"   DELETE phase: wait complete; elapsed since WEAVE_START={elapsed2:.1f}s. "
                    f"Proceeding to bulk export snapshot."
                ))
        except Exception:
            # If anything goes wrong with timing/sleep, continue without blocking DELETE.
            pass

    ids_in_source = {n['id'] for n in preorder(source_nodes)}
    (yield from log(f"   Source IDs (from preorder): {ids_in_source}"))

    # Snapshot Map_T again under the parent to catch any nodes not visited in source
    (yield from log(f"   Re-snapshotting target under parent {parent_uuid}..."))
    Map_T2, Parent_T2, _ = (yield from snapshot_target(parent_uuid, use_efficient_traversal=False, export_func=export_nodes))
    ids_in_target = set(Map_T2.keys())

    # Precompute protection set for identity-seeded ambiguous nodes and their
    # ancestors (only meaningful when explicitly_preserved_ids is present).
    # Any node in this set must never be deleted, even if it is missing from
    # the source nodes list.
    protected_preserved_ids: set[str] = set()
    if explicitly_preserved_ids:
        protected_preserved_ids.update(explicitly_preserved_ids)
        for sid in explicitly_preserved_ids:
            cur = Parent_T2.get(sid)
            while cur is not None:
                if cur in protected_preserved_ids:
                    break
                protected_preserved_ids.add(cur)
                cur = Parent_T2.get(cur)
        (yield from log(
            f"   explicit preserved protection: {len(explicitly_preserved_ids)} explicitly_preserved_ids, "
            f"{len(protected_preserved_ids)} IDs including ancestors"
        ))
    else:
        (yield from log("   No explicitly_preserved_ids present; no additional ancestor protection in DELETE phase"))

    # Guardrail: never consider the reconciliation root for deletion
    if parent_uuid in ids_in_target:
        ids_in_target.discard(parent_uuid)
    (yield from log(f"   Target IDs (after reconciliation, excluding root): {len(ids_in_target)} IDs (list suppressed)"))

    # Raw delete candidates: nodes present in target snapshot but absent from
    # the source JSON. For parents whose children were not fully loaded in the
    # source (truncated_parents), we must NOT interpret missing descendants as
    # deletes – they were simply never shown.
    # @beacon[
    #   id=auto-beacon@reconcile_tree.is_hidden_descendant-ka59,
    #   role=reconcile_tree.is_hidden_descendant,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def is_hidden_descendant(node_id: str, truncated_roots: set[str], parent_map: Dict[str, Optional[str]]) -> bool:
        cur = parent_map.get(node_id)
        while cur is not None:
            if cur in truncated_roots:
                return True
            cur = parent_map.get(cur)
        return False

    # @beacon[
    #   id=auto-beacon@reconcile_tree.parent_all_children_known-3vk8,
    #   role=reconcile_tree.parent_all_children_known,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def parent_all_children_known(
        tid: str,
        parent_map: Dict[str, Optional[str]],
        source_nodes_by_id: Dict[Optional[str], Node],
    ) -> bool:
        # A target node is only eligible for automatic deletion when:
        # - It has a parent present in the source JSON, and
        # - That parent is marked with children_status == 'complete'.
        #
        # This confines DELETE to subtrees whose immediate children set we know
        # is complete (full SCRY or fully-expanded GLIMPSE parents).
        p = parent_map.get(tid)
        if p is None:
            # Never auto-delete the reconciliation root or nodes without a parent.
            return False

        parent_node = source_nodes_by_id.get(p)
        if not parent_node:
            # Parent branch was never present in the source JSON (outside GLIMPSE).
            return False

        status = parent_node.get('children_status', 'complete')
        # For full SCRY exports, children_status may be absent; treat that as complete.
        return status == 'complete'

    raw_delete_candidates = ids_in_target - ids_in_source

    # Remove identity-seeded ambiguous nodes (and their ancestors) from deletion
    # consideration entirely.
    if protected_preserved_ids:
        raw_delete_candidates -= protected_preserved_ids
        (yield from log(
            f"   After explicit-sparing protection, raw_delete_candidates={len(raw_delete_candidates)} "
            "(IDs list suppressed)"
        ))

    # @beacon[
    #   id=auto-beacon@reconcile_tree.can_delete_candidate-p99v,
    #   role=reconcile_tree.can_delete_candidate,
    #   slice_labels=ra-reconcile,
    #   kind=ast,
    # ]
    def can_delete_candidate(tid: str) -> bool:
        """Decide whether a candidate ID is eligible for automatic deletion.

        SAFETY-HARDENED RULE (Option B ALWAYS, now WITH TRUNCATION ENFORCEMENT):

        ✅ A node may be deleted ONLY if:
          1) tid ∈ original_ids_seen   (witness ledger allowlist)
          2) tid ∉ truncated_parents   (subtree is known-complete; safe subtree delete)

        Rationale:
        - `original_ids_seen` prevents deleting IDs we never explicitly saw.
        - `truncated_parents` prevents deleting a node whose subtree may contain
          unseen descendants (Workflowy subtree delete would wipe them).

        Fail-closed:
        - If `original_ids_seen` is empty ⇒ no deletes.
        - If `children_status` is missing for a node, we treat it as TRUNCATED
          (handled when building truncated_parents).
        """
        return (tid in original_ids_seen) and (tid not in truncated_parents)

    to_delete_before_protection = {tid for tid in raw_delete_candidates if can_delete_candidate(tid)}

    # Guardrail: protect all parents referenced in source (containers)
    to_delete = to_delete_before_protection - parent_refs

    # Threshold checks
    planned_delete_roots_set = compute_delete_roots(to_delete, Parent_T2)
    planned_delete_roots[:] = list(planned_delete_roots_set)
    planned_delete_count = len(planned_delete_roots)
    (yield from log(f"   Planned delete candidates (after protections): {to_delete}"))
    (yield from log(f"   Planned delete roots: {set(planned_delete_roots)} (count={planned_delete_count})"))

    # Warn if protections saved container nodes
    protected_hit = len(to_delete_before_protection & parent_refs) > 0
    if protected_hit:
        (yield from log("   NOTE: Some potential deletes were PROTECTED because they are parent refs or the reconciliation root."))

    if dry_run:
        (yield from log("   [DRY-RUN] Skipping deletions. Dry-run complete."))
        return (yield from log_summary_and_close())

    if (max_delete_threshold is not None) and (planned_delete_count > max_delete_threshold) and (not force):
        (yield from log(f"   ABORTING: planned_delete_count {planned_delete_count} exceeds max_delete_threshold {max_delete_threshold} and force=False."))
        return (yield from log_summary_and_close(note="Aborted by delete threshold"))

    for d in planned_delete_roots:
        # @beacon[
        #   id=carto-cancel@reconcile_tree.delete_loop_cancel_check,
        #   role=reconcile_tree delete loop cancellation check,
        #   slice_labels=ra-carto-jobs,ra-reconcile,
        #   kind=span,
        # ]
        if (yield from _check_cancel(where="delete")):
            return (yield from log_summary_and_close(note="Cancelled during DELETE phase"))
        # @beacon-close[
        #   id=carto-cancel@reconcile_tree.delete_loop_cancel_check,
        # ]
        try:
            node_obj = Map_T2.get(d, {}) or {}
            node_name = node_obj.get('name', 'unknown')

            (yield from log(f"   >>> DELETING root: {d} (name: {node_name})"))
            (yield from journal(
                {
                    "op": "delete",
                    "status": "started",
                    "node_id": d,
                    "resumability_safe": False,
                    "message": (
                        f"node operation DELETE started for node {d} (name: {node_name}) - "
                        "RESUMABILITY UNSAFE until delete completes"
                    ),
                }
            ))
            (yield ReconcileRequest('delete', (d,), {}))
            (yield from log(f"   >>> DELETE SUCCESS for {d}"))
            (yield from journal(
                {
                    "op": "delete",
                    "status": "completed",
                    "node_id": d,
                    "resumability_safe": True,
                    "message": (
                        f"node operation DELETE completed for node {d} (name: {node_name}) - "
                        "RESUMABILITY SAFE"
                    ),
                }
            ))
        except Exception as e:
            (yield from log(f"   >>> DELETE FAILED for {d}: {type(e).__name__}: {str(e)}"))
            raise
    (yield from log(f"   DELETE phase complete"))

    return (yield from log_summary_and_close())
