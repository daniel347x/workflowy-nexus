'use strict';
// glimpse-lightbox.js's pure parts, offline: local-path detection and mapping input, live folder/glob lines, file
// names and captions, group labels, heading groups, the gallery test (estates too) and the key bindings; the live-line
// expansion and the grouping on a few fake elements; side by side's picks, stepping, nudges, keys and geometry. The DOM parts (mapImages, sheets, collect, wire, open) run in the
// pane's Chrome fixture (opencode-companion transcription-pane/tests/glimpse-lightbox.html)
// on the actual View page with the actual shared renderer and the vendored PhotoSwipe.
// Run: node tests/js/test_glimpse_lightbox.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const dir = path.join(__dirname, '../../workflowy-glimpse-cache');
const context = vm.createContext({console});
vm.runInContext(fs.readFileSync(path.join(dir, 'glimpse-lightbox.js'), 'utf8'), context, {filename: 'glimpse-lightbox.js'});
const LB = context.GlimpseLightbox;
let checks = 0;
function check(label, fn) {
  fn();
  checks++;
  console.log('ok   ' + label);
}
const plain = value => JSON.parse(JSON.stringify(value));

check('frozen API', () => {
  assert(Object.isFrozen(LB));
  assert.deepStrictEqual(Object.keys(LB).sort(), ['applySizes', 'blocks', 'collect', 'compare', 'compareGeometry', 'compareKey', 'comparePick', 'comparePicks',
    'compareRange', 'compareShown', 'compareStep', 'expandLive', 'fileCaption', 'fileName', 'groupImages', 'groupLabel',
    'imageOnly', 'isImageOnly', 'keyAction', 'liveKind', 'livePaths', 'localPath', 'mapImages', 'markMissing', 'open', 'sheets', 'wire']);
});

// A few elements, enough for the live-line DOM helpers: attributes, a document-order node list, replaceWith.
function fakeDoc(nodes) {
  const make = (tagName, attrs = {}, text = '') => ({tagName, textContent: text, attrs: {...attrs},
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }, hasAttribute(k) { return k in this.attrs; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    replaceWith(...made) { list.splice(list.indexOf(this), 1, ...made); }});
  const list = nodes.map(([tag, attrs, text]) => make(tag, attrs, text));
  const root = {ownerDocument: {createElement: tag => make(tag.toUpperCase())}, nodes: list,
    querySelectorAll(sel) {
      if (sel === 'img[data-glimpse-live]') return list.filter(n => n.tagName === 'IMG' && n.hasAttribute('data-glimpse-live'));
      if (sel === 'img') return list.filter(n => n.tagName === 'IMG');
      return list.filter(n => /^H[1-6]$/.test(n.tagName) || n.tagName === 'IMG');
    }};
  return root;
}

check('fileCaption: a listed file\'s caption is its name without the extension, dashes and underscores as spaces', () => {
  assert.strictEqual(LB.fileCaption('/mnt/e/x/card-dark.png'), 'card dark');
  assert.strictEqual(LB.fileCaption('/mnt/e/x/01__home_screen.v2.PNG'), '01 home screen.v2');
  assert.strictEqual(LB.fileCaption('/mnt/e/My Shots/a b.jpg'), 'a b');
  assert.strictEqual(LB.fileCaption('/mnt/e/x/.png'), '.png');
});

check('livePaths and expandLive: each listed folder/glob line becomes one image per file; empty, failed and unlisted lines stay tiles', () => {
  const live = (path, alt, extra = {}) => ['IMG', {'data-glimpse-live': path.endsWith('/') ? 'folder' : 'glob', 'data-glimpse-path': path, 'data-glimpse-src': path, alt, ...extra}];
  const root = fakeDoc([['H2', {}, 'Opus'], live('/s/opus/', 'Opus shots', {title: 'fresh'}), ['IMG', {src: '/files/s/x.png', 'data-glimpse-path': '/s/x.png', alt: 'fixed'}],
    live('/s/fable/*.png', ''), live('/s/empty/', 'nothing'), live('/s/gone/', 'gone'), live('/s/later/', 'later'), live('/s/opus/', 'again')]);
  assert.deepStrictEqual(plain(LB.livePaths(root)), ['/s/opus/', '/s/fable/*.png', '/s/empty/', '/s/gone/', '/s/later/'], 'each once, in document order');
  const listings = {'/s/opus/': {files: [{path: '/s/opus/card-dark.png', width: 1500, height: 950}, {path: '/s/opus/card-light.png'}]},
    '/s/fable/*.png': {files: [{path: '/s/fable/f 1.png'}]}, '/s/empty/': {files: []}, '/s/gone/': {error: 'no such folder: /s/gone/'}};
  const made = LB.expandLive(root, path => listings[path] || null, path => '/files' + path.split('/').map(encodeURIComponent).join('/'));
  assert.strictEqual(made, 5, 'two, one, and two again for the repeated line');
  const shown = root.nodes.filter(n => n.tagName === 'IMG');
  assert.deepStrictEqual(plain(shown.slice(0, 4).map(n => [n.attrs.src, n.attrs.alt, n.attrs['data-glimpse-set'] || null, n.attrs['data-glimpse-from'] || null])), [
    ['/files/s/opus/card-dark.png', 'card dark', 'Opus shots', '/s/opus/'], ['/files/s/opus/card-light.png', 'card light', 'Opus shots', '/s/opus/'],
    ['/files/s/x.png', 'fixed', null, null], ['/files/s/fable/f%201.png', 'f 1', null, '/s/fable/*.png']]);
  assert.deepStrictEqual(plain([shown[0].attrs.width, shown[0].attrs.height, shown[0].attrs.title, shown[1].attrs.width]), ['1500', '950', 'fresh', null]);
  const states = shown.filter(n => n.hasAttribute('data-glimpse-live')).map(n => [n.attrs['data-glimpse-path'], n.attrs['data-glimpse-live-state'] || null, n.attrs['data-glimpse-live-error'] || null]);
  assert.deepStrictEqual(plain(states), [['/s/empty/', 'empty', null], ['/s/gone/', 'error', 'no such folder: /s/gone/'], ['/s/later/', null, null]]);
  assert.strictEqual(LB.expandLive(root, path => listings[path] || null, null), 0, 'a second pass changes nothing');
});

check('collect: an expanded line with alt text is a group of its own (until the next heading); without alt it joins the heading\'s', () => {
  const img = (src, set, extra = {}) => ['IMG', {src, 'data-glimpse-path': src, ...(set ? {'data-glimpse-set': set} : {}), ...extra}];
  const root = fakeDoc([['H1', {}, '[ap:FIHIHX] ⚗️ probe · live gallery'], img('/a/intro.png'), ['H2', {}, 'Opus · candidate O'], img('/o/1.png', 'Opus'), img('/o/2.png', 'Opus'),
    ['H2', {}, 'Astra'], img('/a/d1.png', 'Astra dark'), img('/a/l1.png', 'Astra light'), img('/a/fixed.png'), ['H2', {}, 'Fable'], img('/f/1.png'),
    ['IMG', {'data-glimpse-live': 'folder', 'data-glimpse-path': '/later/'}]]);
  const groups = LB.collect(root);
  assert.deepStrictEqual(plain(groups.map(g => [g.label, g.heading, g.images.map(i => i.path)])), [
    ['⚗️ probe', '[ap:FIHIHX] ⚗️ probe · live gallery', ['/a/intro.png']],
    ['Opus', 'Opus', ['/o/1.png', '/o/2.png']],
    ['Astra dark', 'Astra › Astra dark', ['/a/d1.png']],
    ['Astra light', 'Astra › Astra light', ['/a/l1.png', '/a/fixed.png']],
    ['Fable', 'Fable', ['/f/1.png']]]);
  assert.deepStrictEqual(plain(root.nodes.filter(n => n.attrs['data-glimpse-lb']).map(n => n.attrs['data-glimpse-lb'])), ['0', '1', '2', '3', '4', '5', '6']);
});

check('localPath: WSL, drive-letter and file:// paths become WSL paths; remote, data and blob sources are not local', () => {
  const cases = [
    ['/mnt/e/__daniel347x/__Dan_Root/__repos/x/docs/ui-bakeoff/card-dark.png', '/mnt/e/__daniel347x/__Dan_Root/__repos/x/docs/ui-bakeoff/card-dark.png'],
    ['/mnt/e/My%20Shots/a%20b.png', '/mnt/e/My Shots/a b.png'],
    ['E:/__daniel347x/shot.png', '/mnt/e/__daniel347x/shot.png'],
    ['c:/Users/x/y.jpg', '/mnt/c/Users/x/y.jpg'],
    ['/E:/x/y.png', '/mnt/e/x/y.png'],
    ['file:///mnt/e/x/y.png', '/mnt/e/x/y.png'],
    ['file:///E:/x/y.png', '/mnt/e/x/y.png'],
    ['file://localhost/home/dan/z.webp', '/home/dan/z.webp'],
    ['E:\\x\\y.png', '/mnt/e/x/y.png'],
    ['  /home/dan/a.gif  ', '/home/dan/a.gif'],
    ['/mnt/e/100%done.png', '/mnt/e/100%done.png'],
    ['https://example.com/a.png', null], ['http://127.0.0.1:9911/files/mnt/e/a.png', null], ['data:image/png;base64,AAAA', null],
    ['blob:https://x/1', null], ['//cdn.example.com/a.png', null], ['shots/a.png', null], ['mailto:x@y', null], ['', null], [null, null]
  ];
  for (const [src, expected] of cases) assert.strictEqual(LB.localPath(src), expected, String(src));
});

check('liveKind: a folder (trailing /) or a glob (* or ? in the last segment); a single file is not live', () => {
  assert.strictEqual(LB.liveKind('/mnt/e/x/shots/'), 'folder');
  assert.strictEqual(LB.liveKind('/mnt/e/x/shots/*.png'), 'glob');
  assert.strictEqual(LB.liveKind('/mnt/e/x/shot-?.png'), 'glob');
  assert.strictEqual(LB.liveKind('/mnt/e/x*y/shot.png'), null);
  assert.strictEqual(LB.liveKind('/mnt/e/x/shot.png'), null);
});

check('fileName: the last segment, decoded, without query or fragment', () => {
  assert.strictEqual(LB.fileName('/mnt/e/x/card-dark.png'), 'card-dark.png');
  assert.strictEqual(LB.fileName('/files/mnt/e/My%20Shots/a%20b.png'), 'a b.png');
  assert.strictEqual(LB.fileName('https://e.com/p/q.jpg?w=200#x'), 'q.jpg');
  assert.strictEqual(LB.fileName('/mnt/e/x/shots/'), 'shots');
});

check('groupLabel: the heading up to its first " · ", without a leading [ap:…] or trailing #tags', () => {
  assert.strictEqual(LB.groupLabel('Opus · candidate O (Opus 5.5 max) · worktree `x`'), 'Opus');
  assert.strictEqual(LB.groupLabel('[ap:PZNQ9J] 🖼️ GALLERY · UI bake-off screenshots'), '🖼️ GALLERY');
  assert.strictEqual(LB.groupLabel('Before and after #prog-human #ui'), 'Before and after');
  assert.strictEqual(LB.groupLabel('  Fable\n · candidate F '), 'Fable');
  assert.strictEqual(LB.groupLabel(''), '');
});

check('groupImages: every heading starts a group; images before any heading are the title\'s; empty groups dropped; one flat index', () => {
  const img = n => ({image: {src: n}});
  const groups = LB.groupImages([img('t1'), {heading: 'Opus · O'}, {heading: 'Fable · F'}, img('f1'), img('f2'), {heading: 'Astra · A'}, img('a1')],
    '[ap:PZNQ9J] 🖼️ GALLERY · bake-off');
  assert.deepStrictEqual(plain(groups.map(g => [g.label, g.heading, g.images.map(i => [i.src, i.index, i.group])])), [
    ['🖼️ GALLERY', '[ap:PZNQ9J] 🖼️ GALLERY · bake-off', [['t1', 0, 0]]],
    ['Fable', 'Fable · F', [['f1', 1, 1], ['f2', 2, 1]]],
    ['Astra', 'Astra · A', [['a1', 3, 2]]]
  ]);
  assert.deepStrictEqual(plain(LB.groupImages([img('x')]).map(g => g.label)), ['Images']);
  assert.deepStrictEqual(plain(LB.groupImages([{heading: 'Only words'}])), []);
  // a live line's set: its own label, taken whole (never cut at " · ")
  assert.deepStrictEqual(plain(LB.groupImages([{heading: 'Astra', label: ' Astra · dark '}, img('d')]).map(g => [g.label, g.heading])), [['Astra · dark', 'Astra']]);
});

check('isImageOnly: headings and images with an intro line or two open straight away; real prose does not', () => {
  const H = {kind: 'heading'}, I = n => ({kind: 'images', count: n}), P = chars => ({kind: 'prose', chars}), M = {kind: 'meta'};
  // the bake-off gallery as View renders the node: title, intro, three groups of images
  assert.strictEqual(LB.isImageOnly([H, P(281), H, I(42), H, I(33), H, I(70)]), true);
  assert.strictEqual(LB.isImageOnly([I(3)]), true);
  assert.strictEqual(LB.isImageOnly([H, M, P(120), I(2), M]), true);
  assert.strictEqual(LB.isImageOnly([H, P(401), I(4)]), false, 'a long paragraph is a document with images, not a gallery');
  assert.strictEqual(LB.isImageOnly([H, P(80), P(80), P(80), I(4)]), false, 'more paragraphs than sections');
  assert.strictEqual(LB.isImageOnly([H, P(80)]), false, 'no images');
  assert.strictEqual(LB.isImageOnly([]), false);
  // an estate's document: a gallery only when every entry has images and none failed
  const E = {kind: 'entry'}, F = {kind: 'entry', failed: true};
  assert.strictEqual(LB.isImageOnly([H, E, M, H, P(90), I(2), M, E, M, H, P(281), H, I(42), H, I(33), H, I(70)]), true, 'two gallery nodes (recipe B)');
  assert.strictEqual(LB.isImageOnly([H, E, M, H, I(2), M, E, M, H, H, H, H]), false, 'a gallery beside a headings-only Dock');
  assert.strictEqual(LB.isImageOnly([H, E, M, H, I(2), M, F, P(60)]), false, 'a failed entry');
});

check('keyAction: groups by Page/arrow keys (arrows pan instead while zoomed), Home/End in the group, zoom, chrome, fullscreen', () => {
  const keys = ['PageDown', 'PageUp', 'ArrowDown', 'ArrowUp', 'Home', 'End', '+', '=', '-', '0', '1', ' ', 'f', 'F', 'ArrowLeft', 'Escape', 'z', 'x'];
  assert.deepStrictEqual(keys.map(k => LB.keyAction(k, false)), ['next-group', 'prev-group', 'next-group', 'prev-group', 'group-first', 'group-last',
    'zoom-in', 'zoom-in', 'zoom-out', 'zoom-fit', 'zoom-actual', 'chrome', 'fullscreen', 'fullscreen', null, null, null, null]);
  assert.strictEqual(LB.keyAction('ArrowDown', true), null);
  assert.strictEqual(LB.keyAction('PageDown', true), 'next-group');
});

// ---- side by side (compare): the arithmetic; the viewer itself runs in the pane's Chrome fixture (glimpse-lightbox-compare.html)
check('comparePicks and comparePick: the first two or three groups; a chip swaps one in or out, three at most, one always stays', () => {
  assert.deepStrictEqual(plain(LB.comparePicks(2)), [0, 1]);
  assert.deepStrictEqual(plain(LB.comparePicks(3)), [0, 1, 2]);
  assert.deepStrictEqual(plain(LB.comparePicks(6)), [0, 1, 2], 'an estate of two galleries: the first three');
  assert.deepStrictEqual(plain(LB.comparePicks(1)), [0]);
  assert.deepStrictEqual(plain(LB.comparePicks(0)), []);
  assert.deepStrictEqual(plain(LB.comparePick([0, 1, 2], 2)), [0, 1], 'a lit chip: its panel goes');
  assert.deepStrictEqual(plain(LB.comparePick([0, 1], 4)), [0, 1, 4], 'another comes in beside them');
  assert.deepStrictEqual(plain(LB.comparePick([0, 2], 1)), [0, 1, 2], 'panels keep document order');
  assert.deepStrictEqual(plain(LB.comparePick([0, 1, 2], 3)), [0, 1, 3], 'with three shown, it takes the right-most panel\'s place');
  assert.deepStrictEqual(plain(LB.comparePick([1, 2, 3], 0)), [0, 1, 2]);
  assert.deepStrictEqual(plain(LB.comparePick([3], 3)), [3], 'one panel always stays');
});

check('compareStep: the panels step together by position, stop where the last one ends, and a nudge moves one panel only', () => {
  const step = (m, ...actions) => actions.reduce((x, a) => (Array.isArray(a) ? LB.compareStep(x, a[0], a[1]) : LB.compareStep(x, a)), m);
  const start = {lengths: [42, 33, 70], offsets: [0, 0, 0], position: 0};
  assert.deepStrictEqual(plain(LB.compareShown(start)), [0, 0, 0]);
  const seventh = step(start, 'next', 'next', 'next', 'next', 'next', 'next');
  assert.deepStrictEqual(plain(LB.compareShown(seventh)), [6, 6, 6], 'image 7 of each group');
  assert.deepStrictEqual(plain(LB.compareRange(start)), {lo: 0, hi: 69});
  const end = step(start, 'last');
  assert.deepStrictEqual(plain([end.position, LB.compareShown(end)]), [69, [-1, -1, 69]], 'past a shorter group\'s last image its panel shows none');
  assert.strictEqual(step(end, 'next').position, 69, 'no step past the longest group');
  assert.strictEqual(step(start, 'prev').position, 0);
  // a nudge: one panel on (or back) one image; the others stay
  const nudged = step(seventh, ['nudge-next', 1]);
  assert.deepStrictEqual(plain([nudged.offsets, LB.compareShown(nudged)]), [[0, 1, 0], [6, 7, 6]]);
  assert.deepStrictEqual(plain(LB.compareShown(step(nudged, 'next'))), [7, 8, 7], 'then they step together again, the nudge kept');
  assert.deepStrictEqual(plain(LB.compareShown(step(nudged, ['nudge-prev', 1], ['nudge-prev', 1]))), [6, 5, 6]);
  // the range follows the nudges: Fable ahead by one ends a step earlier, and starts a step earlier
  const ahead = step(start, ['nudge-next', 1]);
  assert.deepStrictEqual(plain(LB.compareRange(ahead)), {lo: -1, hi: 69});
  assert.deepStrictEqual(plain(LB.compareShown(step(ahead, 'first'))), [-1, 0, -1], 'before the others\' first image');
  // a nudge never leaves its own group; from none past its end, a nudge back shows its last
  assert.deepStrictEqual(plain(LB.compareShown(step(start, ['nudge-prev', 0])).slice(0, 1)), [0]);
  const late = step(start, 'last', ['nudge-prev', 0]);
  assert.deepStrictEqual(plain([LB.compareShown(late)[0], late.offsets[0]]), [41, -28]);
  assert.deepStrictEqual(plain(LB.compareShown(step({lengths: [3, 3], offsets: [0, 0], position: 2}, ['nudge-next', 0]))), [2, 2], 'already at its last');
  // Line up: the nudges go, the shared position stays
  const lined = step(nudged, 'align');
  assert.deepStrictEqual(plain([lined.offsets, lined.position, LB.compareShown(lined)]), [[0, 0, 0], 6, [6, 6, 6]]);
  assert.deepStrictEqual(plain(step(start, 'mystery')), plain(start), 'an unknown action changes nothing');
  assert.deepStrictEqual(plain(start), {lengths: [42, 33, 70], offsets: [0, 0, 0], position: 0}, 'the model given is never changed');
});

check('compareKey: the keys every panel shares (zoom keys as in the lightbox)', () => {
  const keys = ['ArrowRight', 'ArrowLeft', 'Home', 'End', '+', '=', '-', '_', '0', '1', 'a', 'A', ' ', 'f', 'F', 'Escape', 'ArrowUp', 'PageDown', 'x'];
  assert.deepStrictEqual(keys.map(LB.compareKey), ['next', 'prev', 'first', 'last', 'zoom-in', 'zoom-in', 'zoom-out', 'zoom-out', 'zoom-fit', 'zoom-actual',
    'align', 'align', 'chrome', 'fullscreen', 'fullscreen', 'close', null, null, null]);
});

check('compareGeometry: fitted and centred; at a shared zoom the same point of each image sits mid-panel, never leaving a gap', () => {
  const view = {width: 460, height: 600}, shot = {width: 1500, height: 950};
  const fit = LB.compareGeometry(view, shot, null);
  assert.strictEqual(fit.scale, 460 / 1500);
  assert.deepStrictEqual(plain([fit.width, fit.height, fit.left, fit.top].map(Math.round)), [460, 291, 0, 154]);
  const actual = LB.compareGeometry(view, shot, 1);
  assert.deepStrictEqual(plain([actual.width, actual.height, actual.left, actual.top]), [1500, 950, 230 - 750, 300 - 475], '1:1, the middle of the image mid-panel');
  const corner = LB.compareGeometry(view, shot, 1, 0, 0);
  assert.deepStrictEqual(plain([corner.left, corner.top]), [0, 0], 'panned to the top-left corner: no gap past the edge');
  const far = LB.compareGeometry(view, shot, 1, 1, 1);
  assert.deepStrictEqual(plain([far.left, far.top]), [460 - 1500, 600 - 950]);
  // two panels of the same screenshot at the same zoom and centre draw it identically (aligned)
  assert.deepStrictEqual(plain(LB.compareGeometry(view, shot, 2, 0.3, 0.7)), plain(LB.compareGeometry(view, shot, 2, 0.3, 0.7)));
  const small = LB.compareGeometry(view, {width: 100, height: 50}, 1);
  assert.deepStrictEqual(plain([small.left, small.top]), [180, 275], 'smaller than its panel: centred');
  assert.strictEqual(LB.compareGeometry({width: 0, height: 600}, shot, null), null);
  assert.strictEqual(LB.compareGeometry(view, null, null), null, 'no size yet: nothing drawn');
});

check('without PhotoSwipe or images, open returns null and draws nothing', () => {
  assert.strictEqual(LB.open([{label: 'A', heading: 'A', images: [{src: '/x.png', mapped: '/files/x.png'}]}]), null);
  assert.strictEqual(LB.open([]), null);
  assert.strictEqual(LB.compare([{label: 'A', heading: 'A', images: [{src: '/x.png', mapped: '/files/x.png'}]}]), null, 'no document: no side by side');
});

check('the vendored PhotoSwipe is the 5.4.4 UMD core, with its MIT licence', () => {
  const umd = fs.readFileSync(path.join(dir, 'photoswipe/photoswipe.umd.min.js'), 'utf8');
  assert(umd.startsWith('/*!\n  * PhotoSwipe 5.4.4 - https://photoswipe.com'), 'version header');
  assert(/\.PhotoSwipe=/.test(umd.slice(0, 600)), 'defines the PhotoSwipe global');
  assert(fs.readFileSync(path.join(dir, 'photoswipe/LICENSE'), 'utf8').startsWith('The MIT License (MIT)'));
  assert(fs.readFileSync(path.join(dir, 'photoswipe/photoswipe.css'), 'utf8').includes('PhotoSwipe main CSS'));
});

console.log(`${checks} checks passed`);
