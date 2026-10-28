'use strict';

// U5 (spec [ap:HFYAG2], tether [ap:2DXBJA]) — F12+1 fail-closed override row.
//
// Behavioural harness for the two client functions the override channel
// touches, extracted from the real client source and evaluated with stubs
// (the established tests/js pattern — zero dependencies):
//
//   dispatchF12RefreshAction(state, cartoAsync, force)
//     — payload.force=true ONLY on an explicit force; key absent otherwise.
//   executeF12PopupAction(selectionKey)
//     — the confirmFirst row needs a SECOND deliberate selection before it
//       dispatches; the first click only arms it in-popup; ordinary rows are
//       single-click as before.
//
// Plus source-shape locks for the popup wiring the spec pins (§6): the
// override row exists with key '0', alwaysLast, confirmFirst, is appended
// AFTER the static options, the sorter keeps it last, both key gates accept
// '0', and force=true is dispatched from exactly one place.
//
// And three inherited event-order bugs (fixed on ui-next/base, ported from
// ui-bakeoff/astra), each driven through the real extracted functions:
//
//   D. initializeUuidHoverHelper's keydown/keyup listeners — one physical
//      key is ONE press: the keyup of the arming 0 never confirms it, a held
//      key's repeats never act, and a sub-menu opened on keydown never takes
//      that key's release; keyup alone still works as the fallback.
//   E. showF12ActionPopup's late bulk verdicts — they extend only their own
//      still-open main menu, never a sub-menu or a newer menu on the same node.
//   F. the F9 duplicate-heading picker — digit and click both dispatch the
//      chosen file/line before the picker state is cleared.
//
// Run: node tests/js/test_f12_force_override_popup.js   (exit 0 = pass)

const fs = require('fs');
const path = require('path');

const CLIENT_FILE = path.join(__dirname, '..', '..',
  'workflowy-glimpse-cache', 'glimpse-cache-client.js');
const src = fs.readFileSync(CLIENT_FILE, 'utf8');

function extractFunction(name) {
  // Line-start anchored so beacon `role=function name(...)` comment lines are
  // never mistaken for the definition; keeps any `async` keyword.
  const re = new RegExp(`^[ \\t]*(?:async[ \\t]+)?function[ \\t]+${name}[ \\t]*\\(`, 'm');
  const m = re.exec(src);
  if (!m) throw new Error(`function not found in client file: ${name}`);
  const idx = m.index;
  const start = src.indexOf('{', idx);
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(idx, i + 1);
    }
  }
  throw new Error(`unbalanced braces extracting: ${name}`);
}

let failures = 0;
function check(label, condition) {
  if (condition) console.log('ok   ' + label);
  else { failures++; console.log('FAIL ' + label); }
}

function countOccurrences(haystack, needle) {
  let count = 0;
  let at = haystack.indexOf(needle);
  while (at >= 0) { count++; at = haystack.indexOf(needle, at + needle.length); }
  return count;
}

// ---------------------------------------------------------------------------
// A. dispatchF12RefreshAction — the WS payload carries force ONLY when asked
// ---------------------------------------------------------------------------

const sentFrames = [];
const dispatchRefresh = new Function(
  'ws', 'WebSocket', 'showTransientHoverText',
  `${extractFunction('dispatchF12RefreshAction')}
   return dispatchF12RefreshAction;`
)(
  { readyState: 1, send: (raw) => sentFrames.push(JSON.parse(raw)) },
  { OPEN: 1 },
  () => {},
);

const baseState = { uuid: 'U-1', name: 'n', note: 'x', projectEl: {} };

dispatchRefresh({ ...baseState, targetType: 'folder' }, true, true);
check('folder + force → refresh_folder_node payload carries force:true',
  sentFrames.length === 1
  && sentFrames[0].action === 'refresh_folder_node'
  && sentFrames[0].force === true
  && sentFrames[0].carto_async === true);

dispatchRefresh({ ...baseState, targetType: 'file' }, true);
check('file, no force argument → payload has NO force key (normal guarded run)',
  sentFrames.length === 2
  && sentFrames[1].action === 'refresh_file_node'
  && !('force' in sentFrames[1]));

dispatchRefresh({ ...baseState, targetType: 'file' }, true, false);
check('explicit force=false → still no force key (never defaulted true)',
  sentFrames.length === 3 && !('force' in sentFrames[2]));

dispatchRefresh({ ...baseState, targetType: 'node' }, true, true);
check('node target + force → containing-file refresh carries force:true',
  sentFrames.length === 4
  && sentFrames[3].action === 'refresh_file_node'
  && sentFrames[3].force === true);

dispatchRefresh({ ...baseState, targetType: 'folder' }, false);
check('socket-open guard intact: legacy two-argument call still works',
  sentFrames.length === 5 && !('force' in sentFrames[4]));

// ---------------------------------------------------------------------------
// B. executeF12PopupAction — the two-step confirm inside the popup
// ---------------------------------------------------------------------------

function makeExecutor(state) {
  const calls = { dispatched: [], hidden: 0, rerenders: 0 };
  const exec = new Function(
    'f12PopupState', 'hideF12ActionPopup', 'ensureSocketReady', 'showTransientHoverText', 'consoleObj',
    `${extractFunction('executeF12PopupAction').replace(/\bconsole\./g, 'consoleObj.')}
     return executeF12PopupAction;`
  )(
    state,
    () => { calls.hidden++; },
    async () => true,
    () => {},
    console,
  );
  return { exec, calls };
}

function popupState() {
  const dispatched = [];
  const actions = [
    {
      key: '1',
      label: 'Async folder refresh',
      detail: 'Detached CARTO_REFRESH over this subtree',
      disabled: false,
      onSelect: () => dispatched.push('normal'),
    },
    {
      key: '0',
      label: '⚠ Refresh allowing mass deletion (override fail-closed guards)',
      detail: 'Two-step confirm · only for a deliberately moved/emptied root',
      disabled: false,
      alwaysLast: true,
      confirmFirst: true,
      confirmLabel: '⚠ Press 0 AGAIN to confirm the forced refresh',
      confirmDetail: 'Deletions beyond the configured thresholds will be allowed for THIS run only',
      onSelect: () => dispatched.push('force'),
    },
  ];
  let rerenders = 0;
  const state = { uuid: 'U-1', actions, dispatched, get rerenders() { return rerenders; } };
  state.rerender = () => { rerenders++; };
  return state;
}

// D rig: the REAL keydown/keyup listeners (initializeUuidHoverHelper) over the
// REAL executeF12PopupAction + hideF12ActionPopup, sharing one f12PopupState.
function makeKeyRig() {
  const listeners = {};
  const fakeDocument = {
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const rig = new Function(
    'document', 'ensureSocketReady', 'showTransientHoverText', 'consoleObj',
    'matchesHotkey', 'hotkeyConfig', 'hideUuidTooltip',
    `let f12PopupState = null;
     let f12PopupEl = null;
     let sliceApplyDialogState = null;
     let ambiguousMdPickerState = null;
     let markerPickerState = null;
     let lastMousePos = null;
     const GLIMPSE_VERSION = 'test';
     ${extractFunction('hideF12ActionPopup')}
     ${extractFunction('executeF12PopupAction').replace(/\bconsole\./g, 'consoleObj.')}
     ${extractFunction('initializeUuidHoverHelper')}
     initializeUuidHoverHelper();
     return { setState: (s) => { f12PopupState = s; }, getState: () => f12PopupState };`
  )(fakeDocument, async () => true, () => {}, console, () => false, {}, () => {});
  rig.press = (type, key, repeat) => {
    const ev = {
      key, repeat: !!repeat, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false,
      stopped: false,
      preventDefault() {}, stopPropagation() { this.stopped = true; }, stopImmediatePropagation() {},
    };
    (listeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  };
  return rig;
}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

// E rig: the REAL showF12ActionPopup with its two bulk verdicts held open, so
// a test decides when each lands. renderMenu records each repaint's row keys.
function makeVerdictRig() {
  const menus = [];
  const verdicts = { f12p4: [], f12p3: [] };
  const popupEl = { style: {}, innerHTML: '' };
  const rig = new Function(
    'extractNodeName', 'extractNodeNote', 'classifyF12Target', 'nodeChildVisibility',
    'requestBulkMaterializeVerdict', 'requestBulkF12p3Verdict', 'nodeHasAnyChildren',
    'hideUuidTooltip', 'ensureF12ActionPopup', 'getF12ActionOptions', 'GlimpseUI',
    'dispatchF12RefreshAction', 'showF12BulkMaterializePopup', 'showF12BulkGeneratePopup',
    'showTransientHoverText', 'window',
    `let f12PopupState = null;
     let f12PopupEl = null;
     let lastMousePos = null;
     ${extractFunction('hideF12ActionPopup')}
     ${extractFunction('showF12ActionPopup')}
     return { show: showF12ActionPopup, getState: () => f12PopupState };`
  )(
    () => 'Folder', () => '', () => 'folder', () => ({ hasVisible: true, hasHidden: false }),
    () => new Promise((resolve) => verdicts.f12p4.push(resolve)),
    () => new Promise((resolve) => verdicts.f12p3.push(resolve)),
    () => true, () => {}, () => popupEl,
    () => [
      { key: '1', label: 'Refresh', onSelect: () => {} },
      { key: '2', label: 'Refresh visible', onSelect: () => {} },
    ],
    { renderMenu: (el, spec) => menus.push(spec.rows.map((r) => r.key).join(',')) },
    () => {}, () => true,
    // Mirrors the real sub-menus: the SAME state object, its actions replaced.
    (state) => {
      state.actions = [{ key: '1', onSelect: () => {} }, { key: '2', onSelect: () => {} }];
      rig.subMenuOpened = state;
      return true;
    },
    () => {}, { scrollX: 0, scrollY: 0 },
  );
  rig.menus = menus;
  rig.verdicts = verdicts;
  rig.projectEl = {
    getAttribute: () => 'U-1',
    getBoundingClientRect: () => ({ bottom: 0, left: 0 }),
  };
  return rig;
}
const bulkYes = { success: true, show: true, mode: 'bulk' };

// F rig: the REAL F9 duplicate-heading picker functions over one state.
function makePickerRig() {
  const frames = [];
  let lastMenu = null;
  const rig = new Function(
    'ws', 'WebSocket', 'hideUuidTooltip', 'ensureAmbiguousMdPickerPopup', 'GlimpseUI', 'window',
    `let ambiguousMdPickerEl = null;
     let ambiguousMdPickerState = null;
     let lastMousePos = null;
     ${extractFunction('hideAmbiguousMdPickerPopup')}
     ${extractFunction('dispatchAmbiguousMdSelection')}
     ${extractFunction('executeAmbiguousMdPickerSelection')}
     ${extractFunction('showAmbiguousMdPickerPopup')}
     return {
       show: showAmbiguousMdPickerPopup,
       pick: executeAmbiguousMdPickerSelection,
       isOpen: () => ambiguousMdPickerState !== null,
     };`
  )(
    { readyState: 1, send: (raw) => frames.push(JSON.parse(raw)) }, { OPEN: 1 }, () => {},
    () => ({ style: {} }),
    { renderMenu: (el, spec) => { lastMenu = spec; }, hotkeyLabel: () => 'F9' },
    { scrollX: 0, scrollY: 0 },
  );
  rig.frames = frames;
  rig.menu = () => lastMenu;
  return rig;
}
const candidates = [
  { occurrence: 1, line: 12, hint: 'first ## Dump Bay' },
  { occurrence: 2, line: 48, hint: 'second ## Dump Bay' },
];

(async () => {
  {
    const state = popupState();
    const { exec, calls } = makeExecutor(state);
    const first = await exec('0');
    check('first click on the override row arms it: no dispatch, popup stays open',
      first === true && state.dispatched.length === 0 && calls.hidden === 0);
    check('armed row relabels to the confirm text and re-renders in place',
      state.actions[1].label === state.actions[1].confirmLabel
      && state.actions[1].detail === state.actions[1].confirmDetail
      && state.rerenders === 1);
    const second = await exec('0');
    check('second click on the SAME row dispatches the forced refresh and closes',
      second === true && state.dispatched.join() === 'force' && calls.hidden === 1);
  }

  {
    const state = popupState();
    const { exec, calls } = makeExecutor(state);
    await exec('1');
    check('ordinary row stays single-click (no confirm step, dispatched at once)',
      state.dispatched.join() === 'normal' && calls.hidden === 1);
  }

  {
    const state = popupState();
    const { exec, calls } = makeExecutor(state);
    await exec('0');            // arm the override…
    await exec('1');            // …but choose the ordinary row instead
    check('arming the override then choosing another row never dispatches force',
      state.dispatched.join() === 'normal' && calls.hidden === 1);
  }

  {
    const state = popupState();
    const { exec } = makeExecutor(state);
    const missing = await exec('7');
    check('unknown key is a harmless no-op', missing === false && state.dispatched.length === 0);
  }

  // -------------------------------------------------------------------------
  // C. Source-shape locks for the popup wiring (spec §6 / §11.5)
  // -------------------------------------------------------------------------

  check('the override row exists, keyed 0, alwaysLast, confirmFirst',
    src.includes("key: '0'")
    && src.includes('alwaysLast: true')
    && src.includes('confirmFirst: true')
    && src.includes('Refresh allowing mass deletion (override fail-closed guards)'));

  check('the row is appended AFTER the static options (placed last)',
    src.indexOf('state.actions = getF12ActionOptions(state);')
      < src.indexOf('Refresh allowing mass deletion'));

  check('the confirmed row dispatches F12+1 with force from exactly one place',
    countOccurrences(src, 'dispatchF12RefreshAction(state, true, true)') === 1
    && countOccurrences(src, 'payload.force = true') === 1);

  check('the sorter pins the alwaysLast row to the end',
    src.includes('if (a.alwaysLast && !b.alwaysLast) return 1;')
    && src.includes('if (!a.alwaysLast && b.alwaysLast) return -1;'));

  check('both popup key gates accept 0-9 and no 1-9 gate remains',
    countOccurrences(src, '/^[0-9]$/.test(event.key)') === 4
    && !src.includes('[1-9]'));

  check('the confirm step runs before the popup hides (in-popup, not a new modal)',
    src.indexOf('if (action.confirmFirst && !action._confirmArmed)')
      < src.indexOf('hideF12ActionPopup();', src.indexOf('async function executeF12PopupAction(')));

  check('the in-popup re-render is exposed for the armed relabel',
    src.includes('state.rerender = renderPopup;'));

  // -------------------------------------------------------------------------
  // D. One physical key is one press (keydown + its keyup)
  // -------------------------------------------------------------------------

  {
    const rig = makeKeyRig();
    const state = popupState();
    rig.setState(state);
    const down = rig.press('keydown', '0');
    await tick();
    rig.press('keyup', '0');
    await tick();
    check('one physical press of 0 only arms the force row (its keyup never confirms it)',
      state.dispatched.length === 0 && rig.getState() === state && state.actions[1]._confirmArmed === true);
    check('the digit is still swallowed on keydown so Workflowy never types it', down.stopped === true);
    rig.press('keydown', '0');
    await tick();
    rig.press('keyup', '0');
    await tick();
    check('a second physical press of 0 dispatches the forced refresh exactly once',
      state.dispatched.join() === 'force' && rig.getState() === null);
  }

  {
    const rig = makeKeyRig();
    const state = popupState();
    rig.setState(state);
    rig.press('keydown', '0');
    for (let i = 0; i < 3; i++) rig.press('keydown', '0', true);
    await tick();
    rig.press('keyup', '0');
    await tick();
    check('holding 0 (auto-repeat) arms only; repeats never confirm',
      state.dispatched.length === 0 && state.actions[1]._confirmArmed === true);
  }

  {
    const rig = makeKeyRig();
    const state = popupState();
    state.actions[0].onSelect = () => {
      state.dispatched.push('open-sub');
      state.actions = [{ key: '1', label: 'sub', onSelect: () => state.dispatched.push('sub-1') }];
      rig.setState(state);
    };
    rig.setState(state);
    rig.press('keydown', '1');
    await tick();
    rig.press('keyup', '1');
    await tick();
    check('a sub-menu opened on keydown never takes that same key\'s keyup',
      state.dispatched.join() === 'open-sub' && rig.getState() === state);
  }

  {
    const rig = makeKeyRig();
    const state = popupState();
    rig.setState(state);
    rig.press('keyup', '1');
    await tick();
    check('keyup alone (no keydown delivered) still selects: the fallback is intact',
      state.dispatched.join() === 'normal');
  }

  // -------------------------------------------------------------------------
  // E. Late bulk verdicts extend only their own still-open main menu
  // -------------------------------------------------------------------------

  {
    const rig = makeVerdictRig();
    rig.show(rig.projectEl);
    const state = rig.getState();
    rig.verdicts.f12p3[0](bulkYes);
    await tick();
    check('a verdict landing on its open main menu still appends its row (3) and repaints',
      rig.menus[rig.menus.length - 1] === '1,2,3,0');
    state.actions.find((a) => a.key === '3').onSelect();
    const paints = rig.menus.length;
    rig.verdicts.f12p4[0](bulkYes);
    await tick();
    check('a late verdict never writes into or repaints over an open F12 sub-menu',
      rig.subMenuOpened === state && state.actions.map((a) => a.key).join() === '1,2'
      && rig.menus.length === paints && rig.getState() === state);
  }

  {
    const rig = makeVerdictRig();
    rig.show(rig.projectEl);
    const first = rig.getState();
    rig.show(rig.projectEl);
    const second = rig.getState();
    const paints = rig.menus.length;
    rig.verdicts.f12p3[0](bulkYes);
    await tick();
    check('a stale verdict from an earlier menu on the SAME node never repaints the newer one',
      second !== first && rig.getState() === second && rig.menus.length === paints
      && second.actions.map((a) => a.key).join() === '1,2,0');
  }

  // -------------------------------------------------------------------------
  // F. F9 duplicate-heading picker dispatches the choice before clearing it
  // -------------------------------------------------------------------------

  {
    const rig = makePickerRig();
    rig.show(candidates, 'E:/notes/capsule.md', 'N-1');
    rig.pick('2');
    const f = rig.frames[0] || {};
    check('F9 picker digit opens the chosen heading (explicit file + line) and closes',
      rig.frames.length === 1 && f.action === 'open_node_in_windsurf'
      && f.explicit_file_path === 'E:/notes/capsule.md' && f.explicit_line === 48
      && f.node_id === 'N-1' && !rig.isOpen());
  }

  {
    const rig = makePickerRig();
    rig.show(candidates, 'E:/notes/capsule.md', 'N-1');
    rig.menu().rows[0].onClick();
    const f = rig.frames[0] || {};
    check('F9 picker click opens the clicked heading (explicit file + line) and closes',
      rig.frames.length === 1 && f.explicit_file_path === 'E:/notes/capsule.md'
      && f.explicit_line === 12 && !rig.isOpen());
  }

  if (failures) {
    console.error(`\n${failures} check(s) FAILED`);
    process.exit(1);
  }
  console.log('\nall F12 force-override popup checks passed');
  process.exit(0);
})().catch((err) => {
  console.error('harness error:', err);
  process.exit(1);
});
