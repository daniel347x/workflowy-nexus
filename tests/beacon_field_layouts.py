"""Quoted Area-3 p1/p2/p3/p5 and Area-4 field witnesses; no external inputs.

Repeated p3 texts share one quoted layout. Legacy acceptance is exercised as
the adapters migrate; the field planner tests already consume their intents.
"""

COMMENTS = [
    'plain text', 'a, b, c', 'x=y and k = v', 'returns list[int]',
    'ends with a bracket ]', 'ends with a comma,', 'ends ,] run',
    '] leading bracket', 'kind=ast inline', 'id=hijack attempt',
    '"quoted whole"', "'single quoted'", '"a" or "b"', "it's 'x'",
    'uses -- double dash', 'uses --!> bang close', 'mentions @beacon-close[ marker',
    'mentions @beacon marker', '# hash and // slashes', 'trailing backslash \\',
    'unicode é 漢字 🚀 ✓', 'nbsp\u00a0inside', 'zwj\u200dinside', 'bom\ufeffinside',
    '🔱 leading trident', 'x' * 5000, 'a > b && c < d', 'html <b>bold</b>',
    '-->', '*/ closes', '/* opens', '&amp; entity', 'tab\tinside',
    'line\u2028sep', 'nel\x85char', '漢' * 5000,
]
STYLES = [
    ('python', None), ('js_ts', None), ('vue', 'script'), ('vue', 'template'),
    ('vue', 'style'), ('markdown', None), ('sql', None), ('shell', None), ('yaml', None),
]

RICH = {
    'pres.py': ('python', 'box@open', [
        'import os', '', '', 'class Box:', '    """A box."""', '',
        '    # @beacon[', '    #   comment=keeps the lid shut,',
        '    #   id=box@open,', '    #   owner=dan,', '    #   slice_labels=zeta,alpha,',
        '    #', '    #   role=custom-role,', '    #   kind=ast,',
        '    #   show_span=false,', '    # ]', '    def open(self):', '        return 1']),
    'pres.js': ('js_ts', 'box@open', [
        'class Box {', '  // @beacon[', '  //   comment=keeps the lid shut,',
        '  //   id=box@open,', '  //   owner=dan,', '  //   slice_labels=zeta,alpha,',
        '  //', '  //   role=custom-role,', '  //   kind=ast,', '  // ]',
        '  open() {', '    return 1;', '  }', '}']),
    'pres.md': ('markdown', 'md@sec', [
        '# Doc', '', '<!--', '@beacon[', '  comment=keeps the lid shut,',
        '  id=md@sec,', '  owner=dan,', '  slice_labels=zeta,alpha,',
        '  role=custom-role,', '  kind=ast,', ']', '-->', '## Section', '', 'Body.']),
    'pres.sql': ('sql', 'q@1', [
        '-- @beacon[', '--   comment=keeps the lid shut,', '--   id=q@1,',
        '--   owner=dan,', '--   slice_labels=zeta,alpha,', '--   role=custom-role,',
        '--   kind=span,', '--   show_span=false,', '-- ]', 'SELECT 1;',
        '-- @beacon-close[', '--   id=q@1,', '-- ]']),
    'pres.yaml': ('yaml', 'y@1', [
        'root:', '  # @beacon[', '  #   comment=keeps the lid shut,',
        '  #   id=y@1,', '  #   slice_labels=zeta,alpha,', '  #   kind=span,',
        '  # ]', '  key: value', '  # @beacon-close[', '  #   id=y@1,', '  # ]']),
}

SPAN = [
    'from dataclasses import dataclass', 'from typing import Optional', '', '',
    '# @beacon[', '#   id=rec@fields,', '#   slice_labels=keep,', '#   kind=span,',
    '# ]', '@dataclass', 'class Rec:', '    name: str',
    '    comment: Optional[str] = None', '# @beacon-close[', '#   id=rec@fields,', '# ]',
]
DOCSTRING = ['def f():', '    """Explain the note format.', '',
             '    comment: this docstring line looks like a note field', '    """', '    return 1']
ADOPT_PY = ['# @beacon[', '#   id=keep@f,', '#   slice_labels=old1,old2,',
            '#   kind=ast,', '#   comment=an existing careful comment,', '# ]',
            'def f():', '    return 1']
ADOPT_MD = ['# Doc', '', '<!--', '@beacon[', '  id=md@sec,', '  slice_labels=old1,',
             '  kind=ast,', '  comment=an existing careful comment,', ']', '-->',
             '## Section', '', 'Body.']
AUTHOR_MD = ['# Doc', '', '<!-- an unrelated author note -->', '', '<!-- @beacon[',
             '  id=one@1,', '  slice_labels=keep,', '  kind=ast,', '] -->',
             '## Section', '', 'Body.']
SNIPPETS = ['x = 0', '# @beacon[', '#   id=s@snip,', '#   slice_labels=keep,',
            '#   kind=span,', '#   start_snippet="def g(",', '#   end_snippet="return 2",',
            '# ]', 'def g():', '    return 2', '# @beacon-close[', '#   id=s@snip,', '# ]']
VUE = [
    '<template>', '  <div>', '    <!--', '    @beacon[', '      id=tpl@1,',
    '      slice_labels=t1,', '      kind=span,', '    ]', '    -->', '    <p>hello</p>',
    '    <!--', '    @beacon-close[', '      id=tpl@1,', '    ]', '    -->', '    <pre>',
    '// @beacon[', '//   id=fake@pre,', '//   slice_labels=fake,', '//   kind=ast,',
    '// ]', '    </pre>', '  </div>', '</template>', '', '<script>', '// @beacon[',
    '//   id=s0@a,', '//   slice_labels=a0,', '//   kind=ast,',
    '//   comment=returns list[int] of rows,', '// ]', 'export function alpha() {',
    '  return [1];', '}', '</script>', '', '<script setup lang="ts">', '// @beacon[',
    '//   id=s1@b,', '//   slice_labels=b1,', '//   kind=ast,', '// ]',
    'function beta(): number {', '  return 2;', '}', '</script>', '', '<style>',
    '/* @beacon[', '   id=sty@1,', '   slice_labels=st,', '   kind=span,', '] */',
    '.a { color: red; }', '/* @beacon-close[', '   id=sty@1,', '] */', '</style>',
]

# Every p3 probe maps to an exact quoted source above, plus these operations.
P3_CASES = {
    'a': ['def f():', '    return 1'], 'a2': ['def f():', '    return 1'],
    'b': SPAN, 'c': SPAN, 'd': SPAN, 'e': DOCSTRING, 'f': ADOPT_PY,
    'g': ADOPT_MD, 'h': ADOPT_MD, 'i': SNIPPETS, 'j': ADOPT_MD,
}
NOTES = {
    'ast': ['AST_QUALNAME: f'],
    'md_adopt': ['MD_PATH:', '# Doc', '## Section', '---'],
    'md_stale': ['MD_PATH:', '# Doc', '## Section', '---', 'BEACON (MD AST)',
                 'id: md@other', 'kind: ast'],
}
