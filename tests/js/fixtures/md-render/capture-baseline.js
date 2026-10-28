'use strict';
// One-shot pre-extraction capture. No production body is duplicated here.
const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const cp = require('child_process');
const root = process.cwd();
const dir = root + '/workflowy-glimpse-cache/';
const src = fs.readFileSync(dir + 'glimpse-cache-client.js', 'utf8');
if (crypto.createHash('sha256').update(src).digest('hex') !== 'fc48feecce30316119763904eb5cdd886de971d8fe72f291c6bd284aaee8cb8a') {
  throw Error('Capture requires the untouched 8dd6e0f client; never regenerate the oracle from the extracted renderer.');
}
function extract(name) {
  const a = src.indexOf('function ' + name + '(');
  if (a < 0) throw Error(name);
  let depth = 0;
  for (let i = src.indexOf('{', a); i < src.length; i++) {
    if (src[i] === '{') depth++;
    if (src[i] === '}' && --depth === 0) return src.slice(a, i + 1);
  }
  throw Error('unbalanced ' + name);
}
const context = vm.createContext({ console });
context.window = context;
const assets = ['marked.min.js', 'katex/katex.min.js', 'marked-katex-init.js'];
for (const f of assets) vm.runInContext(fs.readFileSync(dir + f, 'utf8'), context, {filename:f});
vm.runInContext(`let dockedSourceViewMode='rendered', dockedSourceSearchRegex=null,
  dockedSourceSearchCount=17, dockedSourceSearchCurrent=9, dockedRenderContentFormat='markdown';\n`
  + ['escapeHtmlForPreview','composedMarkdownToHtml','composedContentToHtml','compileDockSourceSearch','dockedSourceHtml'].map(extract).join('\n'), context);
const realMarked = context.marked;
const cases = [
  {name:'headings-lists',text:'# One\n## Two\n### Three\n#### Four\n##### Five\n###### Six\n\n- parent\n  - child\n    - grandchild\n\n1. first\n2. second\n\n> quoted\n\n[link](https://example.com) ![image](x.png)\n\n---'},
  {name:'table',text:'| A | B |\n|---|---|\n| one | two |'},
  {name:'single-newlines',text:'one\ntwo\nthree'},
  {name:'fenced-code',text:'```js\nconst x = "<>&";\n```\n\n`inline`'},
  {name:'raw-html',text:'<section data-x="raw"><b>raw</b><a href="javascript:example()">link</a></section>\n\n<script>window.rawNote = true;</script>'},
  {name:'entities-unicode',text:'&amp; &lt; &copy; < > café 漢字 🪟 é'},
  {name:'empty',text:''},
  {name:'inline-math',text:'The value $x^2 + \\alpha$ is math.'},
  {name:'display-math',text:'$$\nx^2 + y^2 = z^2\n$$\n\nAnd $$x+1$$ inline display.'},
  {name:'currency-parentheses-bad-tex',text:'Costs $5 and $10. Significant ($p < 0.05$). Bad $\\notARealCommand{x}$.'},
  {name:'absent-fallback',text:'<x>& plain\nline',marked:'absent'},
  {name:'throwing-fallback',text:'<x>& plain\nline',marked:'throw'},
  {name:'source-escape',text:'<b a="x">&\n漢字</b>',view:'source'},
  {name:'source-multiple-current',text:'invoice and Invoice <b>invoice</b>',view:'source',query:'invoice'},
  {name:'source-no-match',text:'nothing here',view:'source',query:'invoice'},
  {name:'source-empty',text:'',view:'source',query:'invoice'},
  {name:'source-blank',text:'invoice <x>',view:'source',query:'  '},
  {name:'source-negated',text:'invoice draft',view:'source',query:'-invoice !draft'},
  {name:'source-smart',text:'invoice BIG receipt monitor draft',view:'source',query:'invoice OR "big receipt" AND mon* -draft'},
  {name:'raw-text-rendered',text:'# raw <tag> & invoice INVOICE',contentFormat:'raw_text',query:'invoice'},
  {name:'raw-text-source',text:'# raw <tag> & invoice',contentFormat:'raw_text',view:'source',query:'invoice'},
];
for (const c of cases) {
  context.marked = c.marked === 'absent' ? undefined : c.marked === 'throw' ? {parse(){throw Error('fixture');}} : realMarked;
  context.input = c;
  c.expected = vm.runInContext(`dockedSourceViewMode=input.view || 'rendered';
    dockedRenderContentFormat=input.contentFormat || 'markdown';
    dockedSourceSearchRegex=compileDockSourceSearch(input.query || '').regex;
    dockedSourceSearchCount=17; dockedSourceSearchCurrent=9;
    (() => {const html=composedContentToHtml(input.text); return {html,count:dockedSourceSearchCount,current:dockedSourceSearchCurrent};})()`, context);
}
const queries = ['', '   ', '-draft !invoice', 'OR AND', '*', '"unbalanced', 'invoice OR "big receipt" mon* -draft', 'a.c', 'monit?r'];
const search = queries.map(query => {
  const r = context.compileDockSourceSearch(query);
  return {query,expected:r.ok ? {ok:true,source:r.regex ? r.regex.source : null,flags:r.regex ? r.regex.flags : null} : {ok:false,error:r.error}};
});
const cssArray = src.match(/style\.textContent = \[([\s\S]*?)\]\.join\('\\n'\);/g).find(s => s.includes('#glimpse-md-dock-panel'));
const css = vm.runInNewContext(cssArray.replace('style.textContent = ', ''));
const rules = css.match(/[^{}]+\{[^{}]*\}/g).map(s => s.trim());
const hash = s => crypto.createHash('sha256').update(s).digest('hex');
const output = {
  provenance:{commit:cp.execSync('git rev-parse HEAD').toString().trim(),clientSha256:hash(src),captureCommand:'node tmp/capture-md-oracle.js',assets:Object.fromEntries(assets.map(f=>[f,hash(fs.readFileSync(dir+f))]))},
  cases,search,
  escape:[null,'','<>&"\'漢字',0].map(text=>({text,html:context.escapeHtmlForPreview(text)})),
  renderCss:rules.filter(s=>s.includes('#glimpse-md-dock-render')).join('\n'),
  hostCss:rules.filter(s=>!s.includes('#glimpse-md-dock-render')).join('\n'),
};
fs.mkdirSync(root+'/tests/js/fixtures/md-render',{recursive:true});
fs.writeFileSync(root+'/tests/js/fixtures/md-render/baseline.json', JSON.stringify(output,null,2)+'\n');
console.log(cases.length+' render cases, '+search.length+' queries; baseline sha256 '+hash(fs.readFileSync(root+'/tests/js/fixtures/md-render/baseline.json')));
