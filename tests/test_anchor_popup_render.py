"""Regression locks for rendering Anchor Docks from the Anchors surface.

The surface is UI chrome only: anchor discovery stays server-authoritative, and
Render must enter the existing raw-root RenderProjectionSpec/apply pipeline.
Since 'The Rail' (designer F, ui-bakeoff/fable) the chrome is the Anchors
drawer in glimpse-ui-drawers.js; the three locks that pinned the UUID Explorer
widget's own markup now pin the deck's equivalent structure.
"""

from pathlib import Path


CLIENT = (
    Path(__file__).resolve().parents[1]
    / "workflowy-glimpse-cache"
    / "glimpse-cache-client.js"
)
UI = CLIENT.parent / "glimpse-ui.js"
DRAWERS = CLIENT.parent / "glimpse-ui-drawers.js"
UI_CSS = CLIENT.parent / "glimpse-ui.css"


def _source() -> str:
    return CLIENT.read_text(encoding="utf-8")


def _function_block(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def _drawer_block(source: str, drawer_id: str, next_id: str) -> str:
    start = source.index(f"UI.registerDrawer('{drawer_id}', {{")
    end = source.index(f"UI.registerDrawer('{next_id}', {{", start)
    return source[start:end]


def _anchors_region(drawers: str) -> str:
    """The Anchors drawer: its state, painter, open action and registration
    (everything between the ANCHORS and SLICES section banners)."""
    start = drawers.index("// ═════════════════════════ ANCHORS")
    end = drawers.index("// ═════════════════════════ SLICES", start)
    return drawers[start:end]


def test_anchor_render_delegates_to_shared_raw_root_apply_pipeline() -> None:
    source = _source()
    # Leg 3 ('The Rail'): the dead popup builders that used to follow this
    # function are deleted; the next top-level function is the export builder.
    block = _function_block(
        source, "_renderAnchorFromPopup", "buildSliceGroupsExportJson"
    )

    assert "closeAnchorListPopup();" in block
    assert "_minimizeUuidNavigatorWidget();" in block
    assert block.index("closeAnchorListPopup();") < block.index(
        "_minimizeUuidNavigatorWidget();"
    ) < block.index("applySliceGroupIntoDock(null, {")
    assert "applySliceGroupIntoDock(null, {" in block
    assert "rootUuid: uid" in block
    assert "sortBy: 'none'" in block
    assert "renderTimeoutMs: CACHE_RENDER_LONG_TIMEOUT_MS" in block
    assert "const generation = ++dockedRenderGeneration;" in block
    assert "renderGeneration: generation" in block
    assert "requestSliceModalRender" not in block


def test_anchor_and_slice_descend_preferences_are_strictly_decoupled() -> None:
    """Leg 3 ('The Rail'): the Anchors popup is deleted; its Descend toggle is
    the Anchors drawer's state button, which reads and writes ONLY the anchor
    preference through the api (readAnchorOpts / saveAnchorOpts), never the
    slice Apply preference. Same lock, new surface."""
    source = _source()
    drawers = DRAWERS.read_text(encoding="utf-8")
    render_block = _function_block(
        source, "_renderAnchorFromPopup", "buildSliceGroupsExportJson"
    )
    anchors = _anchors_region(drawers)

    assert "const ANCHOR_POPUP_OPTS_KEY = 'glimpse_anchor_popup_options';" in source
    assert "const SLICE_APPLY_OPTS_KEY = 'glimpse_slice_apply_options';" in source
    assert "const applyOpts = _readAnchorPopupOptions();" in render_block
    assert (
        "applyOpts.descendThroughDocks ? 'all' : 'stop_at_child_anchors'"
        in render_block
    )
    assert "_readSliceApplyOptions" not in render_block
    # The api binds the drawer's two accessors to the anchor-only store.
    assert "readAnchorOpts: _readAnchorPopupOptions, saveAnchorOpts: _saveAnchorPopupOptions," in source
    # The drawer's Descend button toggles and re-reads the anchor preference ...
    assert "api().saveAnchorOpts({ descendThroughDocks: !o.descendThroughDocks });" in anchors
    assert "k === 'descend' ? !!api().readAnchorOpts().descendThroughDocks" in anchors
    # ... and never touches the slice Apply preference (state shown as is-on, not an ON/OFF label).
    assert "saveApplyOpts" not in anchors
    assert "readApplyOpts" not in anchors
    assert "Descend child Docks: ON" not in anchors


def test_one_time_migration_repairs_the_contaminated_slice_preference() -> None:
    source = _source()
    start = source.index("// ONE-TIME repair for the just-shipped coupling bug")
    end = source.index("// Published handle consumed", start)
    migration = source[start:end]

    assert "localStorage.getItem(ANCHOR_POPUP_OPTS_KEY) == null" in migration
    assert "const contaminatedSliceOpts = _readSliceApplyOptions();" in migration
    assert "descendThroughDocks: !!contaminatedSliceOpts.descendThroughDocks" in migration
    assert "_saveSliceApplyOptions({ descendThroughDocks: true });" in migration


def test_primary_sources_share_one_rail_in_a_fixed_order() -> None:
    """The widget's seven-button header row became the rail: every source of
    material registers as a drawer with an explicit order, the rail sorts by
    it, keeps the Reader button first, and parks Settings and Help at the
    bottom. (Replaces the widget-header order lock; same intent: one row of
    primary controls in a stable order.)"""
    drawers = DRAWERS.read_text(encoding="utf-8")
    ui = UI.read_text(encoding="utf-8")

    expected = [
        ("anchors", "order: 10"),
        ("slices", "order: 20"),
        ("rod", "order: 30"),
        ("tabs", "order: 40"),
        ("jobs", "order: 50"),
        ("settings", "order: 90, bottom: true"),
        ("help", "order: 95, bottom: true"),
    ]
    last = -1
    for drawer_id, order_literal in expected:
        head_start = drawers.index(f"UI.registerDrawer('{drawer_id}', {{")
        head = drawers[head_start : drawers.index("build(container)", head_start)]
        assert order_literal in head, (drawer_id, head)
        assert head_start > last
        last = head_start
    assert "sort((a, b) => (a.def.order || 0) - (b.def.order || 0))" in ui
    assert "ordered.filter((d) => !d.def.bottom).forEach((d) => mid.appendChild(railButtonFor(d)));" in ui
    assert "ordered.filter((d) => d.def.bottom).forEach((d) => bottom.appendChild(railButtonFor(d)));" in ui
    assert "S.els.railReader = readerBtn;" in ui
    assert ui.index("S.els.railReader = readerBtn;") < ui.index("const mid = el('div', 'nx-rail-mid');")


def test_global_tags_render_as_read_only_hash_chips_in_settings() -> None:
    """The widget's comma-delimited Global Tags textarea became the Settings
    drawer's read-only chip list: every stored tag shows with a leading '#',
    there is no editable control, and the empty state points at Options."""
    drawers = DRAWERS.read_text(encoding="utf-8")
    block = _drawer_block(drawers, "settings", "help")

    assert "const tags = Array.isArray(s.global_tags) ? s.global_tags : [];" in block
    assert "tags.forEach((t) => { const c = el('span', 'nx-tag', '#' + t);" in block
    assert "'None configured (Options page → Note sessions → Global tags).'" in block
    assert "el('textarea'" not in block
    assert "el('input'" not in block


def test_closed_deck_has_a_structural_status_sink_guard() -> None:
    """The minimized pill's CSS guard (no async status could pop the widget
    open) became the deck's status sink: legacy status writers land in a
    hidden element that is never displayed, an observer turns each write into
    an Activity entry, and the deck body opens only through its own show path
    (style.display, mirrored onto data-open)."""
    client = _source()
    ui = UI.read_text(encoding="utf-8")
    css = UI_CSS.read_text(encoding="utf-8")

    # !important: legacy writers still set style.display = 'block' on the sink.
    assert ".nx-sink { display: none !important; }" in css
    assert "const sink = el('div', 'nx-sink');" in ui
    assert (
        "new MutationObserver(() => logActivity(sinkText()))"
        ".observe(sink, { childList: true, characterData: true, subtree: true })"
    ) in ui
    assert "uuidNavigatorOutputEl = r.sink;" in client
    assert "body.style.display = 'none';" in ui
    assert "if (S.els.deck) S.els.deck.setAttribute('data-open', open ? '1' : '0');" in ui


def test_every_anchor_row_opens_through_the_shared_render_action() -> None:
    """Leg 3 ('The Rail'): the popup's tree and flat painters (each with its own
    Render button) are deleted. The Anchors drawer walks the server's tree OR
    the flat sessions fallback through ONE row painter whose primary click is
    anchorsOpen, and anchorsOpen enters the client through api.renderAnchor =
    _renderAnchorFromPopup. Same lock: every row, both shapes, one Render path."""
    source = _source()
    drawers = DRAWERS.read_text(encoding="utf-8")
    anchors = _anchors_region(drawers)

    assert (
        "(res.anchor_tree && Array.isArray(res.anchor_tree.children)) ? res.anchor_tree.children"
        " : (Array.isArray(res.sessions) ? res.sessions : [])"
    ) in anchors
    assert "onPrimary: () => anchorsOpen(id, name)," in anchors
    assert "try { api().renderAnchor(uuid); }" in anchors
    assert "renderAnchor: (uuid) => _renderAnchorFromPopup(uuid)," in source


def test_raw_root_success_toast_is_not_labeled_as_slice() -> None:
    source = _source()
    block = _function_block(source, "displaySliceInDock", "requestCompileSliceSearch")

    assert "_rs && _rs.kind === 'raw_root'" in block
    assert "? 'Rendered subtree'" in block
    assert "statusLead" in block
