/**
 * Workflowy GLIMPSE Cache - Options page logic
 *
 * Pairs with options.html. Renders a table of customizable actions, lets the
 * user rebind each one by clicking "Rebind" and pressing a key combination,
 * and persists changes to chrome.storage.sync.
 *
 * Storage key: 'hotkey_config' (single object: { action_name: binding, ... }).
 *
 * Capture flow (Option Y per the design discussion):
 *   - User clicks "Rebind" on a row.
 *   - The binding cell flashes amber and the button text changes to "Cancel".
 *   - The next non-modifier KeyboardEvent is captured by a one-shot keydown
 *     listener (capture phase, preventDefault, stopPropagation).
 *   - If the captured combo is reserved by Chrome (e.g., bare F12), we show
 *     an inline error and refuse — the user picks again.
 *   - Otherwise, the new binding is stored immediately and synced.
 *   - Escape during capture cancels without changing.
 */

'use strict';

const STORAGE_KEY = 'hotkey_config';
const SETTINGS_STORAGE_KEY = 'extension_settings';
// The broker shared-secret token is a PER-LOCAL-MACHINE pairing secret, so it
// lives in chrome.storage.LOCAL (top-level key), NOT in chrome.storage.sync and
// NOT nested inside the 'extension_settings' object. background.js's
// getBrokerToken() reads local first; keep these in agreement.
const BROKER_TOKEN_STORAGE_KEY = 'broker_token';
const SAVED_TOAST_DURATION_MS = 1800;

let currentConfig = {}; // merged defaults + user overrides
let currentSettings = {}; // merged extension settings + user overrides
let captureTeardown = null; // function to call to exit capture mode

const tbody = document.getElementById('hotkeys-tbody');
const resetBtn = document.getElementById('reset-btn');
const savedToast = document.getElementById('saved-toast');
const payloadLimitInput = document.getElementById('payload-limit-kb');
const mdPreviewWidthInput = document.getElementById('md-preview-width-px');
const mdPreviewMaxCharsInput = document.getElementById('md-preview-max-chars');
const mdModalPrerenderMaxCharsInput = document.getElementById('md-modal-prerender-max-chars');
const mdPreviewDockTopOffsetInput = document.getElementById('md-preview-dock-top-offset-px');
const savedTabsDriveDelayInput = document.getElementById('saved-tabs-drive-delay-seconds');
// NOTE (Dan, July 2026): the old 'Expand-tag prefixes (F8)' options field was
// removed. F8 now acts on the ACTIVE expand GROUP's prefixes, managed in the
// UUID Explorer widget's 'Groups' section, not here. The flat
// expand_tag_prefixes key is retained in storage for parse-compat only and is
// no longer surfaced on this page.
const markerTagsInput = document.getElementById('marker-tags');
// F8 slice postfixes + global never-expand tags (Dan, 2026). The postfix
// WORDS are server-consumed (it joins them as P + '--' + word); the
// never-expand list is a blanket suppressor. All live in extension_settings.
const f8PostfixAutoExpandInput = document.getElementById('f8-postfix-auto-expand');
const f8PostfixSuppressInput = document.getElementById('f8-postfix-suppress');
const f8PostfixDeprecatedInput = document.getElementById('f8-postfix-deprecated');
const f8GlobalNeverExpandTagsInput = document.getElementById('f8-global-never-expand-tags');
const renameTabWithAliasInput = document.getElementById('rename-tab-with-alias');
const tabTitleAffixInput = document.getElementById('tab-title-affix');
const tabTitleAffixPositionInput = document.getElementById('tab-title-affix-position');
const tabTitleExampleEl = document.getElementById('tab-title-example');
const defaultMarkdownFolderInput = document.getElementById('default-markdown-folder');
const archiveFolderInput = document.getElementById('archive-folder');
// ── Note sessions ──────────────────────────────────────────────────────────
const noteSessionTagsInput = document.getElementById('note-session-tags');
// LOCAL-ANCHOR postfix words (sits directly beneath the anchor-tag field in
// the UI). Comma-separated list; parsed/formatted with the SAME shared
// helpers as the anchor tags; falls back to ['local'] when cleared/invalid.
const localAnchorPostfixesInput = document.getElementById('note-session-local-postfixes');
// FAVORITE-ANCHOR postfix words (sits directly beneath the local-anchor
// field in the UI). Comma-separated list; parsed/formatted with the SAME
// shared helpers as the local-anchor words; falls back to ['fav'] when
// cleared/invalid.
const favoriteAnchorPostfixesInput = document.getElementById('note-session-favorite-postfixes');
const globalTagsInput = document.getElementById('global-tags');
const defaultNoteSessionsParentInput = document.getElementById('default-note-sessions-parent');
const defaultNoteSessionsParentActivateBtn = document.getElementById('default-note-sessions-parent-activate');
const defaultNoteSessionsParentPathEl = document.getElementById('default-note-sessions-parent-path');
// Project Maps location: a Workflowy node pointer (UUID, 8-hex or [ap:…]) —
// stored verbatim and read back through system.config.get. No Activate and no
// validation: NEXUS never interprets what is inside the node.
const projectMapsLocationInput = document.getElementById('project-maps-location');
// Placeholder alias used purely to illustrate the composed title in the
// options-page live example (the real alias is per-tab and lives in the
// Workflowy tab's sessionStorage, which the options page cannot see).
const TAB_TITLE_EXAMPLE_ALIAS = 'spine';
const brokerTokenInput = document.getElementById('broker-token');
const brokerTokenToggle = document.getElementById('broker-token-toggle');
// "The Rail" (ui-bakeoff/fable): the deck's two appearance keys, also set from
// the deck's Settings drawer and the Reader's View popover; same storage.
const glimpseUiThemeInput = document.getElementById('glimpse-ui-theme');
const glimpseUiOutlineInput = document.getElementById('glimpse-ui-outline');
// "The Rail, side by side" (ui-next): push Workflowy aside, panel side, and the
// first-run tips reset (clears glimpse_help_dismissed so the three cards return).
const glimpseUiPushInput = document.getElementById('glimpse-ui-push');
const glimpseUiFlipInput = document.getElementById('glimpse-ui-flip');
const glimpseHelpTipsResetBtn = document.getElementById('glimpse-help-tips-reset');

/**
 * Load the current config (defaults merged with storage overrides) and
 * render the table.
 */
function loadAndRender() {
  chrome.storage.sync.get([STORAGE_KEY, SETTINGS_STORAGE_KEY], (raw) => {
    const userConfig = (raw && raw[STORAGE_KEY]) || {};
    const userSettings = (raw && raw[SETTINGS_STORAGE_KEY]) || {};
    currentConfig = GlimpseHotkeys.mergeHotkeyConfig(userConfig);
    currentSettings = GlimpseHotkeys.mergeExtensionSettings(userSettings);
    renderTable();
    renderSettings();
  });
}

// Load the broker token from chrome.storage.local (its canonical store) and
// show it in the input. Falls back to chrome.storage.sync only so a token
// written by an older sync-primary build still appears here.
function loadBrokerToken() {
  if (!brokerTokenInput) return;
  chrome.storage.local.get([BROKER_TOKEN_STORAGE_KEY], (raw) => {
    let tok = raw && raw[BROKER_TOKEN_STORAGE_KEY];
    if (tok && String(tok).trim()) {
      brokerTokenInput.value = String(tok).trim();
      return;
    }
    chrome.storage.sync.get([BROKER_TOKEN_STORAGE_KEY], (raw2) => {
      const tok2 = raw2 && raw2[BROKER_TOKEN_STORAGE_KEY];
      brokerTokenInput.value = tok2 && String(tok2).trim() ? String(tok2).trim() : '';
    });
  });
}

// Persist the broker token to chrome.storage.local (top-level key). An empty
// value REMOVES the key so the broker handshake cleanly reports "no token set"
// rather than sending an empty string.
function persistBrokerToken(rawValue) {
  const trimmed = String(rawValue || '').trim();
  const done = () => {
    if (chrome.runtime.lastError) {
      console.error(
        '[GLIMPSE Options] storage.local broker_token write failed:',
        chrome.runtime.lastError
      );
      return;
    }
    flashSavedToast();
  };
  if (trimmed) {
    chrome.storage.local.set({ [BROKER_TOKEN_STORAGE_KEY]: trimmed }, done);
  } else {
    chrome.storage.local.remove([BROKER_TOKEN_STORAGE_KEY], done);
  }
}

function renderSettings() {
  if (payloadLimitInput) {
    payloadLimitInput.value = String(currentSettings.max_glimpse_payload_kb || 500);
  }
  if (mdPreviewWidthInput) {
    mdPreviewWidthInput.value = String(currentSettings.md_preview_width_px || 720);
  }
  if (mdPreviewMaxCharsInput) {
    mdPreviewMaxCharsInput.value = String(currentSettings.md_preview_max_chars || 2000000);
  }
  if (mdModalPrerenderMaxCharsInput) {
    const v = Number.isFinite(Number(currentSettings.md_modal_prerender_max_chars))
      ? Number(currentSettings.md_modal_prerender_max_chars)
      : 100000;
    mdModalPrerenderMaxCharsInput.value = String(v);
  }
  if (mdPreviewDockTopOffsetInput) {
    const off = Number.isFinite(Number(currentSettings.md_preview_dock_top_offset_px))
      ? Number(currentSettings.md_preview_dock_top_offset_px)
      : 100;
    mdPreviewDockTopOffsetInput.value = String(off);
  }
  if (savedTabsDriveDelayInput) {
    const d = Number(currentSettings.saved_tabs_drive_delay_seconds);
    savedTabsDriveDelayInput.value = Number.isFinite(d) ? String(d) : '';
  }
  if (markerTagsInput) {
    const markers = Array.isArray(currentSettings.marker_tags)
      ? currentSettings.marker_tags
      : ['wfy-\uD83C\uDFF7\uFE0F'];
    markerTagsInput.value = GlimpseHotkeys.formatMarkerTags(markers);
  }
  if (f8PostfixAutoExpandInput) {
    f8PostfixAutoExpandInput.value = currentSettings.f8_postfix_auto_expand || 'expand';
  }
  if (f8PostfixSuppressInput) {
    f8PostfixSuppressInput.value = currentSettings.f8_postfix_suppress || 'suppress';
  }
  if (f8PostfixDeprecatedInput) {
    f8PostfixDeprecatedInput.value = currentSettings.f8_postfix_deprecated || 'deprecated';
  }
  if (f8GlobalNeverExpandTagsInput) {
    const tags = Array.isArray(currentSettings.f8_global_never_expand_tags)
      ? currentSettings.f8_global_never_expand_tags
      : [];
    f8GlobalNeverExpandTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(tags);
  }
  if (renameTabWithAliasInput) {
    // Default true: only show unchecked when the user explicitly disabled it.
    renameTabWithAliasInput.checked = currentSettings.rename_tab_with_alias !== false;
  }
  if (tabTitleAffixInput) {
    tabTitleAffixInput.value = currentSettings.tab_title_affix || 'Workflowy';
  }
  if (tabTitleAffixPositionInput) {
    tabTitleAffixPositionInput.value =
      currentSettings.tab_title_affix_position === 'suffix' ? 'suffix' : 'prefix';
  }
  if (defaultMarkdownFolderInput) {
    defaultMarkdownFolderInput.value = currentSettings.default_markdown_folder || '';
  }
  if (archiveFolderInput) {
    archiveFolderInput.value = currentSettings.archive_folder || '';
  }
  if (noteSessionTagsInput) {
    const tags = Array.isArray(currentSettings.note_session_tags)
      ? currentSettings.note_session_tags
      : ['wfy-ns'];
    noteSessionTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(tags);
  }
  if (localAnchorPostfixesInput) {
    const words = Array.isArray(currentSettings.note_session_local_postfixes)
      && currentSettings.note_session_local_postfixes.length
        ? currentSettings.note_session_local_postfixes
        : ['local'];
    localAnchorPostfixesInput.value = GlimpseHotkeys.formatExpandTagPrefixes(words);
  }
  if (favoriteAnchorPostfixesInput) {
    const words = Array.isArray(currentSettings.note_session_favorite_postfixes)
      && currentSettings.note_session_favorite_postfixes.length
        ? currentSettings.note_session_favorite_postfixes
        : ['fav'];
    favoriteAnchorPostfixesInput.value = GlimpseHotkeys.formatExpandTagPrefixes(words);
  }
  if (globalTagsInput) {
    const tags = Array.isArray(currentSettings.global_tags)
      ? currentSettings.global_tags
      : [];
    globalTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(tags);
  }
  if (defaultNoteSessionsParentInput) {
    defaultNoteSessionsParentInput.value = currentSettings.default_note_sessions_parent || '';
  }
  if (projectMapsLocationInput) {
    projectMapsLocationInput.value = currentSettings.project_maps_location || '';
  }
  if (glimpseUiThemeInput) {
    const t = String(currentSettings.glimpse_ui_theme || 'auto');
    glimpseUiThemeInput.value = (t === 'light' || t === 'dark') ? t : 'auto';
  }
  if (glimpseUiOutlineInput) {
    const o = String(currentSettings.glimpse_ui_outline || 'auto');
    glimpseUiOutlineInput.value = (o === 'side' || o === 'top') ? o : 'auto';
  }
  if (glimpseUiPushInput) {
    // Default true: only show unchecked when the user explicitly disabled it.
    glimpseUiPushInput.checked = currentSettings.glimpse_ui_push !== false;
  }
  if (glimpseUiFlipInput) {
    glimpseUiFlipInput.checked = !!currentSettings.glimpse_ui_flip;
  }
  updateTabTitleExample();
}

// Render the live example tab title (e.g. 'WFY - spine' / 'spine - WFY')
// from the CURRENT field values, using the same shared composer the client
// uses so the example matches the real result exactly.
function updateTabTitleExample() {
  if (!tabTitleExampleEl) return;
  const affix = tabTitleAffixInput ? tabTitleAffixInput.value : 'Workflowy';
  const position = (tabTitleAffixPositionInput && tabTitleAffixPositionInput.value === 'suffix')
    ? 'suffix' : 'prefix';
  const composer = (GlimpseHotkeys && GlimpseHotkeys.composeTabTitle)
    ? GlimpseHotkeys.composeTabTitle
    : (a, p, al) => (p === 'suffix' ? `${al} - ${(a || 'Workflowy')}` : `${(a || 'Workflowy')} - ${al}`);
  tabTitleExampleEl.textContent = composer(affix, position, TAB_TITLE_EXAMPLE_ALIAS);
}

function renderTable() {
  tbody.innerHTML = '';
  for (const [actionName, meta] of Object.entries(GlimpseHotkeys.HOTKEY_ACTIONS)) {
    const row = document.createElement('tr');
    row.dataset.action = actionName;

    const labelCell = document.createElement('td');
    labelCell.className = 'action-label';
    labelCell.textContent = meta.label;
    row.appendChild(labelCell);

    const bindingCell = document.createElement('td');
    bindingCell.className = 'binding-cell';
    const bindingDisplay = document.createElement('span');
    bindingDisplay.className = 'binding-display';
    bindingDisplay.textContent = currentConfig[actionName] || meta.default;
    bindingCell.appendChild(bindingDisplay);

    const messageEl = document.createElement('div');
    messageEl.className = 'inline-message';
    messageEl.style.display = 'none';
    bindingCell.appendChild(messageEl);

    row.appendChild(bindingCell);

    const buttonCell = document.createElement('td');
    buttonCell.className = 'button-cell';
    const rebindBtn = document.createElement('button');
    rebindBtn.className = 'rebind';
    rebindBtn.textContent = 'Rebind';
    rebindBtn.addEventListener('click', () =>
      enterCaptureMode(actionName, row, bindingDisplay, messageEl, rebindBtn)
    );
    buttonCell.appendChild(rebindBtn);
    row.appendChild(buttonCell);

    tbody.appendChild(row);
  }
}

/**
 * Put a single row into capture mode. Returns a teardown function via the
 * module-level captureTeardown variable so a second click elsewhere can
 * cancel the in-progress capture cleanly.
 */
function enterCaptureMode(actionName, row, bindingDisplay, messageEl, rebindBtn) {
  // Cancel any previous in-flight capture before starting a new one.
  if (captureTeardown) {
    captureTeardown('superseded');
  }

  const originalText = bindingDisplay.textContent;
  bindingDisplay.classList.add('capturing');
  bindingDisplay.classList.remove('reserved');
  bindingDisplay.textContent = 'Press any key…';
  rebindBtn.textContent = 'Cancel';
  rebindBtn.classList.add('cancel');
  messageEl.style.display = 'none';
  messageEl.classList.remove('error');

  function teardown(reason) {
    document.removeEventListener('keydown', onKeydown, true);
    rebindBtn.removeEventListener('click', onCancelClick);
    bindingDisplay.classList.remove('capturing');
    rebindBtn.textContent = 'Rebind';
    rebindBtn.classList.remove('cancel');
    if (reason === 'cancelled' || reason === 'superseded') {
      bindingDisplay.textContent = originalText;
      messageEl.style.display = 'none';
    }
    if (captureTeardown === teardown) {
      captureTeardown = null;
    }
  }
  captureTeardown = teardown;

  function onKeydown(event) {
    // Bare modifier presses don't count as a capture — let the user keep
    // holding modifiers while they reach for the final key.
    const k = event.key;
    if (k === 'Control' || k === 'Alt' || k === 'Shift' || k === 'Meta') {
      return;
    }

    // Always preventDefault during capture so the captured key doesn't
    // accidentally trigger other browser shortcuts.
    event.preventDefault();
    event.stopPropagation();

    if (k === 'Escape') {
      teardown('cancelled');
      return;
    }

    const binding = GlimpseHotkeys.formatHotkey(event);
    if (!binding) {
      messageEl.textContent = 'Could not capture that combination — try again.';
      messageEl.classList.add('error');
      messageEl.style.display = 'block';
      return;
    }

    if (GlimpseHotkeys.isHotkeyReserved(binding)) {
      bindingDisplay.classList.remove('capturing');
      bindingDisplay.classList.add('reserved');
      bindingDisplay.textContent = binding;
      messageEl.textContent =
        binding +
        ' is reserved by Chrome and will not reach the extension. Add a modifier (e.g., Ctrl+' +
        binding +
        ') or pick a different key.';
      messageEl.classList.add('error');
      messageEl.style.display = 'block';
      // Stay in capture mode so the user can try again immediately.
      bindingDisplay.classList.add('capturing');
      return;
    }

    // Commit.
    bindingDisplay.classList.remove('capturing', 'reserved');
    bindingDisplay.textContent = binding;
    messageEl.style.display = 'none';
    currentConfig[actionName] = binding;
    persistConfig({ [actionName]: binding });
    teardown('committed');
  }

  function onCancelClick(ev) {
    ev.preventDefault();
    teardown('cancelled');
  }

  document.addEventListener('keydown', onKeydown, true);
  rebindBtn.addEventListener('click', onCancelClick);
}

/**
 * Persist a partial config patch into chrome.storage.sync. We merge the
 * patch onto whatever is already there so other actions are not clobbered.
 */
function persistConfig(patch) {
  chrome.storage.sync.get([STORAGE_KEY], (raw) => {
    const existing = (raw && raw[STORAGE_KEY]) || {};
    const updated = Object.assign({}, existing, patch);
    chrome.storage.sync.set({ [STORAGE_KEY]: updated }, () => {
      if (chrome.runtime.lastError) {
        console.error(
          '[GLIMPSE Options] storage.sync.set failed:',
          chrome.runtime.lastError
        );
        return;
      }
      flashSavedToast();
    });
  });
}

function persistSettings(patch) {
  chrome.storage.sync.get([SETTINGS_STORAGE_KEY], (raw) => {
    const existing = (raw && raw[SETTINGS_STORAGE_KEY]) || {};
    const updated = GlimpseHotkeys.mergeExtensionSettings(Object.assign({}, existing, patch));
    chrome.storage.sync.set({ [SETTINGS_STORAGE_KEY]: updated }, () => {
      if (chrome.runtime.lastError) {
        console.error(
          '[GLIMPSE Options] storage.sync.set settings failed:',
          chrome.runtime.lastError
        );
        return;
      }
      currentSettings = updated;
      renderSettings();
      flashSavedToast();
    });
  });
}

if (payloadLimitInput) {
  payloadLimitInput.addEventListener('change', () => {
    const n = Number(payloadLimitInput.value);
    if (!Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ max_glimpse_payload_kb: n });
  });
  payloadLimitInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      payloadLimitInput.blur();
    }
  });
}

if (mdPreviewWidthInput) {
  mdPreviewWidthInput.addEventListener('change', () => {
    const n = Number(mdPreviewWidthInput.value);
    if (!Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ md_preview_width_px: n });
  });
  mdPreviewWidthInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      mdPreviewWidthInput.blur();
    }
  });
}

if (mdPreviewMaxCharsInput) {
  mdPreviewMaxCharsInput.addEventListener('change', () => {
    const n = Number(mdPreviewMaxCharsInput.value);
    if (!Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ md_preview_max_chars: n });
  });
  mdPreviewMaxCharsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      mdPreviewMaxCharsInput.blur();
    }
  });
}

if (mdModalPrerenderMaxCharsInput) {
  mdModalPrerenderMaxCharsInput.addEventListener('change', () => {
    const n = Number(mdModalPrerenderMaxCharsInput.value);
    if (!Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ md_modal_prerender_max_chars: n });
  });
  mdModalPrerenderMaxCharsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      mdModalPrerenderMaxCharsInput.blur();
    }
  });
}

if (mdPreviewDockTopOffsetInput) {
  mdPreviewDockTopOffsetInput.addEventListener('change', () => {
    const n = Number(mdPreviewDockTopOffsetInput.value);
    if (!Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ md_preview_dock_top_offset_px: n });
  });
  mdPreviewDockTopOffsetInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      mdPreviewDockTopOffsetInput.blur();
    }
  });
}

if (savedTabsDriveDelayInput) {
  savedTabsDriveDelayInput.addEventListener('change', () => {
    const raw = String(savedTabsDriveDelayInput.value || '').trim();
    const n = Number(raw);
    if (!raw || !Number.isFinite(n)) {
      renderSettings();
      return;
    }
    persistSettings({ saved_tabs_drive_delay_seconds: n });
  });
  savedTabsDriveDelayInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      savedTabsDriveDelayInput.blur();
    }
  });
}

if (markerTagsInput) {
  markerTagsInput.addEventListener('change', () => {
    // Sanitize the comma-separated input into the canonical marker-tag array.
    // If the user clears the field or types only invalid tokens, fall back to
    // the shipped default so F10 always has at least one marker tag to toggle.
    const markers = GlimpseHotkeys.parseMarkerTags(markerTagsInput.value);
    const finalMarkers = markers.length ? markers : ['wfy-\uD83C\uDFF7\uFE0F'];
    persistSettings({ marker_tags: finalMarkers });
    // Re-render so the field shows the cleaned, canonical form.
    markerTagsInput.value = GlimpseHotkeys.formatMarkerTags(finalMarkers);
  });
  markerTagsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      markerTagsInput.blur();
    }
  });
}

// F8 slice postfix WORDS. Persist verbatim on commit; mergeExtensionSettings
// sanitizes (single word, '#'-stripped) and falls back to the shipped default
// on empty/invalid, then renderSettings re-syncs the field to the stored value.
function _wireF8PostfixInput(inputEl, key) {
  if (!inputEl) return;
  inputEl.addEventListener('change', () => {
    persistSettings({ [key]: inputEl.value });
  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { inputEl.blur(); }
  });
}
_wireF8PostfixInput(f8PostfixAutoExpandInput, 'f8_postfix_auto_expand');
_wireF8PostfixInput(f8PostfixSuppressInput, 'f8_postfix_suppress');
_wireF8PostfixInput(f8PostfixDeprecatedInput, 'f8_postfix_deprecated');

if (f8GlobalNeverExpandTagsInput) {
  f8GlobalNeverExpandTagsInput.addEventListener('change', () => {
    // Parse + sanitize like prefixes. An EMPTY result is legitimate (no global
    // suppressors), so we persist whatever parses — no fallback default.
    const tags = GlimpseHotkeys.parseExpandTagPrefixes(f8GlobalNeverExpandTagsInput.value);
    persistSettings({ f8_global_never_expand_tags: tags });
    // Reflect the cleaned, canonical form back into the field.
    f8GlobalNeverExpandTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(tags);
  });
  f8GlobalNeverExpandTagsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { f8GlobalNeverExpandTagsInput.blur(); }
  });
}

if (renameTabWithAliasInput) {
  renameTabWithAliasInput.addEventListener('change', () => {
    persistSettings({ rename_tab_with_alias: !!renameTabWithAliasInput.checked });
  });
}

if (tabTitleAffixInput) {
  // Live-update the example as the user types; persist (trimmed/defaulted) on
  // commit. persistSettings -> mergeExtensionSettings trims and falls back to
  // 'Workflowy' on empty, and renderSettings re-syncs the field afterward.
  tabTitleAffixInput.addEventListener('input', updateTabTitleExample);
  tabTitleAffixInput.addEventListener('change', () => {
    persistSettings({ tab_title_affix: tabTitleAffixInput.value });
  });
  tabTitleAffixInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      tabTitleAffixInput.blur();
    }
  });
}

if (tabTitleAffixPositionInput) {
  tabTitleAffixPositionInput.addEventListener('change', () => {
    const pos = tabTitleAffixPositionInput.value === 'suffix' ? 'suffix' : 'prefix';
    persistSettings({ tab_title_affix_position: pos });
    updateTabTitleExample();
  });
}

if (defaultMarkdownFolderInput) {
  // Persist (trimmed) on commit. persistSettings -> mergeExtensionSettings
  // trims and stores verbatim (empty allowed = unset); renderSettings then
  // re-syncs the field to the stored value.
  defaultMarkdownFolderInput.addEventListener('change', () => {
    persistSettings({ default_markdown_folder: defaultMarkdownFolderInput.value });
  });
  defaultMarkdownFolderInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      defaultMarkdownFolderInput.blur();
    }
  });
}

if (archiveFolderInput) {
  // Archive folder (Archive Reads): same commit-trim-store shape as the
  // default Markdown folder. Empty = archive reads off.
  archiveFolderInput.addEventListener('change', () => {
    persistSettings({ archive_folder: archiveFolderInput.value });
  });
  archiveFolderInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      archiveFolderInput.blur();
    }
  });
}

if (noteSessionTagsInput) {
  noteSessionTagsInput.addEventListener('change', () => {
    // Sanitize the comma-separated input into the canonical tag array. If the
    // user clears it or types only invalid tokens, fall back to the seeded
    // ['wfy-ns'] default so the system can always identify note sessions.
    const tags = GlimpseHotkeys.parseExpandTagPrefixes(noteSessionTagsInput.value);
    const finalTags = tags.length ? tags : ['wfy-ns'];
    persistSettings({ note_session_tags: finalTags });
    noteSessionTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(finalTags);
  });
  noteSessionTagsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      noteSessionTagsInput.blur();
    }
  });
}

if (localAnchorPostfixesInput) {
  localAnchorPostfixesInput.addEventListener('change', () => {
    // Sanitize the comma-separated input into a canonical word list (reusing
    // the same tag-token parser -- postfix words obey the same charset rules:
    // no '#', no whitespace). If the user clears it or types only invalid
    // tokens, fall back to the seeded ['local'] so LOCAL anchors always have a
    // usable postfix (mirrors the anchor-tag field's fallback discipline, NOT
    // the honor-empty discipline of global tags -- an empty local-postfix set
    // would silently disable the local-anchor feature).
    const words = GlimpseHotkeys.parseExpandTagPrefixes(localAnchorPostfixesInput.value);
    const finalWords = words.length ? words : ['local'];
    persistSettings({ note_session_local_postfixes: finalWords });
    localAnchorPostfixesInput.value = GlimpseHotkeys.formatExpandTagPrefixes(finalWords);
  });
  localAnchorPostfixesInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      localAnchorPostfixesInput.blur();
    }
  });
}

if (favoriteAnchorPostfixesInput) {
  favoriteAnchorPostfixesInput.addEventListener('change', () => {
    // Sanitize the comma-separated input into a canonical word list (reusing
    // the same tag-token parser -- postfix words obey the same charset rules:
    // no '#', no whitespace). If the user clears it or types only invalid
    // tokens, fall back to the seeded ['fav'] so favorite anchors always have
    // a usable postfix (mirrors the local-anchor field's fallback discipline,
    // NOT the honor-empty discipline of global tags -- an empty
    // favorite-postfix set would silently disable the favorites shortlist).
    const words = GlimpseHotkeys.parseExpandTagPrefixes(favoriteAnchorPostfixesInput.value);
    const finalWords = words.length ? words : ['fav'];
    persistSettings({ note_session_favorite_postfixes: finalWords });
    favoriteAnchorPostfixesInput.value = GlimpseHotkeys.formatExpandTagPrefixes(finalWords);
  });
  favoriteAnchorPostfixesInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      favoriteAnchorPostfixesInput.blur();
    }
  });
}

if (globalTagsInput) {
  globalTagsInput.addEventListener('change', () => {
    // Global tags may legitimately be empty (nothing known yet), so unlike the
    // note-session tags we honor an explicitly-cleared list rather than
    // restoring a default.
    const tags = GlimpseHotkeys.parseExpandTagPrefixes(globalTagsInput.value);
    persistSettings({ global_tags: tags });
    globalTagsInput.value = GlimpseHotkeys.formatExpandTagPrefixes(tags);
  });
  globalTagsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      globalTagsInput.blur();
    }
  });
}

if (defaultNoteSessionsParentInput) {
  // Persist (trimmed) on commit. Empty = unset (the create_note_session MCP
  // tool then fails loudly rather than rooting). No UUID-shape validation on
  // persist; Activate provides the human-facing confirmation render.
  defaultNoteSessionsParentInput.addEventListener('change', () => {
    persistSettings({
      default_note_sessions_parent: defaultNoteSessionsParentInput.value,
    });
    // Persisting a new UUID invalidates any previously-rendered path.
    if (defaultNoteSessionsParentPathEl) {
      defaultNoteSessionsParentPathEl.textContent = '';
    }
  });
  defaultNoteSessionsParentInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      defaultNoteSessionsParentInput.blur();
      // Enter doubles as Activate for convenience.
      activateDefaultNoteSessionsParentPath();
    }
  });
}

if (projectMapsLocationInput) {
  // Project Maps location. Same commit-trim-store shape as the archive folder:
  // persist the raw field value on change (mergeExtensionSettings trims it),
  // empty allowed = no Project Maps node configured. No Activate, no
  // resolution and no validation — the value is a pointer, nothing more.
  projectMapsLocationInput.addEventListener('change', () => {
    persistSettings({ project_maps_location: projectMapsLocationInput.value });
  });
  projectMapsLocationInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      projectMapsLocationInput.blur();
    }
  });
}

if (defaultNoteSessionsParentActivateBtn) {
  defaultNoteSessionsParentActivateBtn.addEventListener('click', () => {
    activateDefaultNoteSessionsParentPath();
  });
}

// Persist (and confirm) the configured default note-sessions parent UUID.
//
// SIMPLIFIED (Dan, 2026): this used to ALSO dial the broker over a throwaway
// WebSocket and ask the server (resolve_uuid_path) to render the full
// root->node path for the UUID, purely as a human-facing confirmation. That
// server round-trip routinely surfaced a misleading 'Timed out' / 'WebSocket
// died' error in this dialog even though the UUID itself persisted fine (the
// options page is not a Workflowy tab and holds no persistent socket, so the
// transient broker dial is flaky). The path render was never load-bearing —
// the create_note_session tool resolves the parent from the stored setting at
// use time, not from anything this function does. So we drop the entire
// network attempt and just persist + confirm locally. No socket, no timeout,
// no false error. (If a real root->path confirmation is ever wanted here, do
// it as a deliberate, separate feature — not as a side effect of saving.)
function activateDefaultNoteSessionsParentPath() {
  if (!defaultNoteSessionsParentPathEl) return;
  const uuid = String(
    (defaultNoteSessionsParentInput && defaultNoteSessionsParentInput.value) || ''
  ).trim();
  if (!uuid) {
    defaultNoteSessionsParentPathEl.textContent =
      '(no UUID set — paste a Workflowy node UUID above)';
    return;
  }
  // Persist the (trimmed) UUID. This is the only thing that matters — the MCP
  // create_note_session tool reads this stored value at use time.
  persistSettings({ default_note_sessions_parent: uuid });
  defaultNoteSessionsParentPathEl.textContent = 'Default note-sessions parent set ✓';
}

if (brokerTokenInput) {
  brokerTokenInput.addEventListener('change', () => {
    persistBrokerToken(brokerTokenInput.value);
    // Normalize the field to the trimmed value actually stored.
    brokerTokenInput.value = String(brokerTokenInput.value || '').trim();
  });
  brokerTokenInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      brokerTokenInput.blur();
    }
  });
}

if (glimpseUiThemeInput) {
  glimpseUiThemeInput.addEventListener('change', () => {
    persistSettings({ glimpse_ui_theme: glimpseUiThemeInput.value });
  });
}

if (glimpseUiOutlineInput) {
  glimpseUiOutlineInput.addEventListener('change', () => {
    persistSettings({ glimpse_ui_outline: glimpseUiOutlineInput.value });
  });
}

if (glimpseUiPushInput) {
  glimpseUiPushInput.addEventListener('change', () => {
    persistSettings({ glimpse_ui_push: glimpseUiPushInput.checked });
  });
}

if (glimpseUiFlipInput) {
  glimpseUiFlipInput.addEventListener('change', () => {
    persistSettings({ glimpse_ui_flip: glimpseUiFlipInput.checked });
  });
}

if (glimpseHelpTipsResetBtn) {
  glimpseHelpTipsResetBtn.addEventListener('click', () => {
    persistSettings({ glimpse_help_dismissed: [] });
  });
}

if (brokerTokenToggle && brokerTokenInput) {
  brokerTokenToggle.addEventListener('click', () => {
    const masked = brokerTokenInput.type === 'password';
    brokerTokenInput.type = masked ? 'text' : 'password';
    brokerTokenToggle.textContent = masked ? 'Hide' : 'Show';
  });
}

function flashSavedToast() {
  savedToast.classList.add('visible');
  clearTimeout(flashSavedToast._t);
  flashSavedToast._t = setTimeout(() => {
    savedToast.classList.remove('visible');
  }, SAVED_TOAST_DURATION_MS);
}

resetBtn.addEventListener('click', () => {
  if (captureTeardown) captureTeardown('cancelled');
  chrome.storage.sync.remove([STORAGE_KEY, SETTINGS_STORAGE_KEY], () => {
    if (chrome.runtime.lastError) {
      console.error(
        '[GLIMPSE Options] storage.sync.remove failed:',
        chrome.runtime.lastError
      );
      return;
    }
    currentConfig = GlimpseHotkeys.defaultHotkeyConfig();
    currentSettings = GlimpseHotkeys.mergeExtensionSettings({});
    renderTable();
    renderSettings();
    flashSavedToast();
  });
});

// React to changes made elsewhere (e.g., on a different device's sync push,
// or the user editing storage via DevTools).
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'sync') {
    if (changes[STORAGE_KEY] || changes[SETTINGS_STORAGE_KEY]) {
      loadAndRender();
    }
    // A token migrated/edited in sync should still refresh the field.
    if (changes[BROKER_TOKEN_STORAGE_KEY]) {
      loadBrokerToken();
    }
    return;
  }
  if (areaName === 'local') {
    // The broker token's canonical store is local; reflect external edits.
    if (changes[BROKER_TOKEN_STORAGE_KEY]) {
      loadBrokerToken();
    }
    return;
  }
});

loadAndRender();
loadBrokerToken();
