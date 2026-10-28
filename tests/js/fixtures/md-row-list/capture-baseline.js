'use strict';
// Extraction/shim only: never a test implementation of the painter.
process.env.TZ = 'America/New_York';
process.env.LANG = 'en_US.UTF-8';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');
const root = path.resolve(__dirname, '../../../..');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const sourceHash = '0c691876d9d5974f29328dec411a55339a61e8ee5fe6ea2320256c79695aa72f';
function extract(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw Error('Missing function ' + name);
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
  }
  throw Error('Unbalanced ' + name);
}
function hostCss(src) {
  const start = src.indexOf("style.id = 'glimpse-md-dock-style'");
  const a = src.indexOf('style.textContent = [', start) + 'style.textContent = '.length;
  const b = src.indexOf("].join('\\n')", a) + "].join('\\n')".length;
  return vm.runInNewContext(src.slice(a, b));
}
function cssRules(css) {
  return (css.replace(/\/\*[\s\S]*?\*\//g, '').match(/[^{}]+\{[^{}]*\}/g) || []).map(s => s.trim());
}
function shim(events) {
  class Element {
    constructor(tag) {
      this.tag = tag; this.children = []; this.attributes = []; this.listeners = [];
      this._text = ''; this._html = ''; this.className = '';
      const styles = new Map();
      this.styleEntries = styles;
      this.style = new Proxy({setProperty:(key, value)=>styles.set(key, String(value))}, {
        set(target, key, value) { styles.set(key, String(value)); return true; },
      });
      this.classList = {add:(...names)=>{
        this.className = [...new Set((this.className.split(/\s+/).filter(Boolean)).concat(names))].join(' ');
      }};
    }
    set innerHTML(value) { this._html = String(value); this._text = ''; this.children = []; }
    get innerHTML() { return this._html; }
    set textContent(value) { this._text = String(value); this._html = ''; this.children = []; }
    get textContent() { return this._text; }
    setAttribute(key, value) { this.attributes.push([key, String(value)]); }
    appendChild(child) { this.children.push(child); return child; }
    addEventListener(type, listener) { this.listeners.push({type, listener}); }
    focus() { events.push(['focus']); }
    record() {
      return {tag:this.tag, className:this.className, attributes:this.attributes,
        text:this._text, innerHTML:this._html, style:[...this.styleEntries], title:this.title || '',
        listeners:this.listeners.map(l=>l.type), children:this.children.map(c=>c.record())};
    }
  }
  return {createElement:tag=>new Element(tag)};
}
function run(src, c, shared, canonical = false, emoji) {
  const events = [];
  const document = shim(events);
  const container = document.createElement('div');
  container.innerHTML = 'stale content';
  container.appendChild(document.createElement('stale'));
  const rows = JSON.parse(JSON.stringify(c.rows));
  if (canonical) for (const row of rows) { row.name = row.text; delete row.text; }
  const context = vm.createContext({document, console});
  const options = {sortBy:c.sortBy, sortDir:c.sortDir, selected:new Set(c.selected), primary:c.primary,
    onClick:(index,event)=>events.push(['callback',index,event === context.clickEvent]),
    markdownEmoji:emoji === undefined ? '📘' : emoji};
  Object.assign(context, {container, rows, options});
  if (shared) vm.runInContext(shared, context);
  if (shared === null) {
    vm.runInContext(['formatDockTimestamp','localDayOrdinal','localDayLabel'].map(n=>extract(src,n)).join('\n'), context);
  }
  if (shared === undefined) throw Error('Choose legacy (null), shared API or delegate explicitly');
  const delegate = c.delegate || shared === null;
  if (delegate) {
    vm.runInContext(`let dockedListEl=container, dockedListRows=rows,
      dockedSelectedIndices=options.selected, dockedSelectedIndex=options.primary;
      const MARKDOWN_EMOJI=options.markdownEmoji;
      const getMdPreviewSortBy=()=>options.sortBy, getMdPreviewSortDir=()=>options.sortDir;
      const handleDockRowClick=options.onClick;\n${extract(src, 'renderDockedList')}\nrenderDockedList();`, context);
  } else {
    if (emoji === undefined) delete options.markdownEmoji; // exercise default
    vm.runInContext('GlimpseMd.renderRowList(container, rows, options)', context);
  }
  const tree = container.record();
  const rowElements = container.children.filter(el=>el.className.split(' ').includes('glimpse-md-dock-row'));
  const links = rows.map((r,i)=>r._rowEl === rowElements[i]);
  for (let i = 0; i < rowElements.length; i++) {
    for (const gesture of [{}, {ctrlKey:true}, {metaKey:true}, {shiftKey:true}]) {
      context.clickEvent = {...gesture, stopPropagation:()=>events.push(['stopPropagation'])};
      rowElements[i].listeners.find(l=>l.type === 'click').listener(context.clickEvent);
    }
  }
  return {tree, links, events};
}
function corpus() {
  const t = date => Date.parse(date) / 1000;
  const rows = [
    {depth:0,text:'Root <tag> & "raw"',kind:'mdfile',collapsed:true,created:t('2020-01-01T05:00:00Z'),modified:t('2020-02-01T05:00:00Z')},
    {depth:1,text:'Child',kind:'mdnode',collapsed:false,created:t('2026-03-07T17:00:01Z'),modified:t('2026-03-07T17:00:02Z')},
    {depth:4,text:'漢字 café é 🪟',kind:'mdnode',collapsed:true,created:t('2026-03-08T07:00:00Z'),modified:t('2026-03-09T04:00:00Z')},
    {depth:7,text:'Long '.repeat(90),kind:'none',collapsed:true,created:t('2026-03-08T16:01:02Z'),modified:t('2026-03-09T12:00:00Z')},
    {depth:1,text:'',kind:'none',collapsed:false,created:t('2026-03-08T18:00:00Z'),modified:t('2026-03-10T12:00:00Z')},
    {depth:4,text:'Missing times',kind:'mdfile',collapsed:false},
    {depth:0,text:'Outdent',kind:'none',collapsed:false,created:t('2026-03-07T20:00:00Z'),modified:t('2026-03-08T15:00:00Z')},
    {depth:1,text:null,kind:'none',collapsed:false,created:0,modified:-1},
    {depth:0,text:'Final leaf',kind:'none',collapsed:false,created:t('2026-03-11T04:01:00Z'),modified:t('2026-03-12T04:01:00Z')},
  ];
  const cases = [];
  for (const sortBy of ['none','created','modified']) for (const sortDir of ['asc','desc']) {
    cases.push({name:`${sortBy}-${sortDir}-multi`, rows:sortDir === 'asc' ? rows : [...rows].reverse(), sortBy,sortDir,selected:[0,2,5],primary:2});
  }
  cases.push({name:'no-selection',rows,sortBy:'none',sortDir:'asc',selected:[],primary:-1});
  cases.push({name:'primary-outside-set',rows,sortBy:'created',sortDir:'asc',selected:[1],primary:4});
  // Same rows as daemon: no DOM kind/collapsed/element; alias tested after move.
  cases.push({name:'canonical-no-icons',rows:rows.map(({kind,collapsed,...row})=>row),sortBy:'modified',sortDir:'asc',selected:[4],primary:4});
  cases.push({name:'empty',rows:[],sortBy:'modified',sortDir:'desc',selected:[],primary:-1});
  return cases;
}
if (require.main === module) {
  const src = fs.readFileSync(path.join(root,'workflowy-glimpse-cache/glimpse-cache-client.js'),'utf8');
  if (sha(src) !== sourceHash) throw Error('Capture requires untouched 0c56c96 client; never regenerate from moved bytes');
  const cases = corpus().map(c=>({...c,expected:run(src,c,null)}));
  const output = {provenance:{commit:'0c56c969dc9e58f6695f6d132780e6bc9fdee007',clientSha256:sha(src),
    command:'node tests/js/fixtures/md-row-list/capture-baseline.js',timezone:process.env.TZ,locale:'en-US',
    resolvedLocale:new Intl.DateTimeFormat().resolvedOptions().locale},
    rowCss:cssRules(hostCss(src)).filter(r=>r.includes('.glimpse-md-dock-')).join('\n'),cases};
  if (output.provenance.resolvedLocale !== 'en-US') throw Error('Capture requires en-US locale');
  const file = path.join(__dirname,'baseline.json');
  if (fs.existsSync(file)) throw Error('Immutable oracle already exists');
  fs.writeFileSync(file, JSON.stringify(output,null,2)+'\n');
  console.log(`${cases.length} untouched-client cases; baseline sha256 ${sha(fs.readFileSync(file))}`);
}
module.exports = {extract,hostCss,cssRules,run,sha,sourceHash};
