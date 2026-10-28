"""Regression locks for the slice-group Apply keyboard wiring.

Final keyboard design (exhaustive live debugging): bare digits typed against a
NON-EDITABLE focus are consumed by page-command layers (e.g. Vimium; B opened
its bookmark Vomnibar) before any page listener sees them, while digits type
normally into real editable controls (why F12+1/2 works from Workflowy's
editor). The Apply surface therefore focuses a genuine 'Shortcut' text input on
open and accepts ONLY 1/2/Escape (keydown primary, 'input' event as the
text-entry fallback). The earlier MAIN/document_start ingress was removed as
unnecessary.

Since 'The Rail' (designer F, ui-bakeoff/fable) the surface is the Slices
drawer's Apply popover (slicesApplyPop in glimpse-ui-drawers.js), which
publishes its key handler to the client through api.setSliceApplyDialogState;
the UUID Explorer's openSliceApplyDialog is deleted (leg 3).

RETIRED (leg 3): test_slice_apply_has_no_native_title_tooltips. The Rail's
discovery model gives every control a tooltip naming its hotkey (design note
[ap:VX7YBY], Interaction model), so a no-tooltips lock contradicts the design.
"""

import json
from pathlib import Path


EXTENSION = Path(__file__).resolve().parents[1] / "workflowy-glimpse-cache"
CLIENT = EXTENSION / "glimpse-cache-client.js"
DRAWERS = EXTENSION / "glimpse-ui-drawers.js"
UI = EXTENSION / "glimpse-ui.js"
MANIFEST = EXTENSION / "manifest.json"


def _source() -> str:
    return CLIENT.read_text(encoding="utf-8")


def _apply_pop() -> str:
    drawers = DRAWERS.read_text(encoding="utf-8")
    start = drawers.index("function slicesApplyPop(")
    end = drawers.index("function slicesMenu(", start)
    return drawers[start:end]


def test_extension_versions_make_reload_provable() -> None:
    source = _source()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    # Both move together on every UI change so a reload is provable in the
    # console AND on chrome://extensions ('The Rail', ui-bakeoff/fable).
    # Re-pinned for the Rail's page-scroll fix (fix/rail-page-scroll): the push no longer
    # transforms <body> or hides <html>'s overflow.
    # Re-pinned for the Project Maps location option (3.54.0).
    assert "const GLIMPSE_VERSION = '3.54.0';" in source
    assert manifest["version"] == "3.54.0"


def test_no_early_keyboard_ingress_machinery_remains() -> None:
    """The diagnostic MAIN/document_start ingress + custom-event sink were
    diagnostic scaffolding; the focused-editable-input fix replaces them."""
    source = _source()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    all_js = [
        name
        for entry in manifest["content_scripts"]
        for name in entry.get("js", [])
    ]

    assert "early-keyboard-main.js" not in all_js
    assert not (EXTENSION / "early-keyboard-main.js").exists()
    assert "__glimpse_slice_apply_key__" not in source
    assert "handleEarlyMainSliceApplyKey" not in source


def test_dialog_focuses_a_real_shortcut_input_accepting_only_1_2_escape() -> None:
    """Leg 3: the Apply popover keeps the Vimium-safe design: a real text input
    (#glimpse-sliceapply-shortcut) is focused by the popover's focus option on
    open, keydown is primary, 'input' is the text-entry fallback, and only
    1/2/Escape act."""
    pop = _apply_pop()
    ui = UI.read_text(encoding="utf-8")

    assert "shortcut = el('input', 'nx-input nx-apply-shortcut'); shortcut.type = 'text'; shortcut.id = 'glimpse-sliceapply-shortcut';" in pop
    assert "focus: '#glimpse-sliceapply-shortcut'" in pop
    assert "if (o.focus) { try { const f = pop.querySelector(o.focus); if (f) f.focus(); }" in ui
    assert "shortcut.addEventListener('keydown'" in pop
    assert "shortcut.addEventListener('input'" in pop
    # Only 1/2/Escape act; no hidden A/B aliases.
    assert "ev.key === 'Escape' || ev.key === '1' || ev.key === '2'" in pop
    assert "['1', 'Apply', () => doApply(false)]" in pop
    assert "['2', 'Apply + make this tab default', () => doApply(true)]" in pop
    assert "['A'" not in pop
    assert "['B'" not in pop


def test_central_keydown_keyup_fallbacks_remain() -> None:
    """Leg 3: the central dispatcher's slice-Apply branch still runs ahead of
    F9/F10/F12 and reads the state the popover publishes through the api."""
    source = _source()
    dispatcher = source.index("id=f12-popup@keydown-dispatch")
    branch = source.index("if (sliceApplyDialogState)", dispatcher)
    keyup_start = source.index("document.addEventListener('keyup', (event) => {")

    assert dispatcher < branch < keyup_start
    assert "sliceApplyDialogState.executeKey(event.key, 'keydown')" in source[branch:keyup_start]
    keyup_slice_branch = source.index(
        "sliceApplyDialogState.executeKey(event.key, 'keyup')", keyup_start
    )
    assert keyup_start < keyup_slice_branch
    assert "setSliceApplyDialogState: (st) => { sliceApplyDialogState = st || null; }," in source
    assert "_sliceApplyEscHandler" not in source


def test_slice_apply_dialog_state_closes_state() -> None:
    """Leg 3: the popover publishes {executeKey} on open and clears it on close
    (its onClose), Escape closes, and the client holds the state in the one
    hoisted var the dispatcher reads."""
    source = _source()
    pop = _apply_pop()

    assert "api().setSliceApplyDialogState({ executeKey: (key) => executeKey(key) });" in pop
    assert "if (key === 'Escape') { close(); return true; }" in pop
    assert "onClose: () => { try { api().setSliceApplyDialogState(null); }" in pop
    assert "var sliceApplyDialogState = null;" in source


def test_copy_closes_and_minimizes_at_intent_time() -> None:
    """Leg 3: Copy search terms closes the popover AND the drawer before the
    copy round-trip (the old closeSliceApplyDialog + _minimizeUuidNavigatorWidget
    pair), so the Reader is unobstructed when the toast lands.

    "The Rail, side by side" (ui-next/rail-side-by-side): the drawer close became
    UI.afterPick(): a transient picker sheet closes, a pinned one stays open (Dan's
    call: pickers close after a pick, with a pin to keep them open). Same intent,
    same place in the handler: before the copy round-trip."""
    source = _source()
    pop = _apply_pop()
    start = pop.index("const doCopy = () => {")
    end = pop.index("const executeKey = (key) => {", start)
    handler = pop[start:end]

    assert handler.index("close(); UI.afterPick();") < handler.index("api().copySearch(gid")
    assert "copySearch: (gid, flash) => _copySliceGroupSearchString(gid, flash)," in source


def test_slice_rows_declare_label_tags_and_actions() -> None:
    """'The Rail' (designer F): the slice-group table's three real cells
    (name | APPLY | copy) became one tree row per slice in the Slices drawer:
    the label, the tag chips, and the Apply / Edit actions, keyed by
    data-group-id so the Apply glow and the fixture find the row. The old
    prefix / root cells stay gone."""
    source = _source()
    drawers = DRAWERS.read_text(encoding="utf-8")
    block = drawers[drawers.index("function slicesRender()") : drawers.index("function slicesEdit(")]

    assert "const tags = el('span', 'nx-tree-tags');" in block
    assert "extra: tags, actions: [play, edit]," in block
    assert "row.setAttribute('data-group-id', g.id);" in block
    assert "const tdPrefixes = document.createElement('td')" not in source
    assert "const tdRoot = document.createElement('td')" not in source
