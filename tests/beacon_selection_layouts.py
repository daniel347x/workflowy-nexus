"""Area-4's 58 layouts plus four twins, two extents and two adoption cases.

Quoted physical rows transcribed from the recon inputs, not live beacons.
Each tuple is (original name, language, target specs, physical rows).
"""
RECON_LAYOUTS = [
    ('py_p00_base.py', 'python', ['f', 'g', {'remove_targets': ['f']}], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def f():',
        '    return 1', '', '', 'def g():', '    return 2',
    ]),
    ('py_p01_blank.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '', '', 'def f():',
        '    return 1',
    ]),
    ('py_p02_comment.py', 'python', ['f', {'remove_targets': ['f']}], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '# a note about f',
        'def f():', '    return 1',
    ]),
    ('py_p03_deco_below.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '# @beacon[', '#   id=b1@f,',
        '#   slice_labels=lab,', '#   kind=ast,', '# ]', '@deco', 'def f():', '    return 1',
    ]),
    ('py_p04_between_deco.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '@deco', '# @beacon[',
        '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def f():', '    return 1',
    ]),
    ('py_p05_deco_comment.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '# @beacon[', '#   id=b1@f,',
        '#   slice_labels=lab,', '#   kind=ast,', '# ]', '@deco', '# why decorated', 'def f():', '    return 1',
    ]),
    ('py_p06_multiline_deco.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '# @beacon[', '#   id=b1@f,',
        '#   slice_labels=lab,', '#   kind=ast,', '# ]', '@deco(', '    None,', '    1,', ')', 'def f():', '    return 1',
    ]),
    ('py_p07_string_between.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]',
        '"""Section: helpers."""', 'def f():', '    return 1',
    ]),
    ('py_p08a_atbeacon_call.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]',
        'print("@beacon is a marker")', 'def f():', '    return 1',
    ]),
    ('py_p08b_atbeacon_const.py', 'python', ['f', 'MARK'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'MARK = "@beacon"',
        'def f():', '    return 1',
    ]),
    ('py_p09_code_between.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'print("hello")',
        'def f():', '    return 1',
    ]),
    ('py_p10_orphan_above_other.py', 'python', ['g'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def g():',
        '    return 2',
    ]),
    ('py_p11_orphan_stacked.py', 'python', ['g'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '# @beacon[',
        '#   id=b2@g,', '#   slice_labels=glab,', '#   kind=ast,', '# ]', 'def g():', '    return 2',
    ]),
    ('py_p12_reordered.py', 'python', ['g',
        {'t': 'f', 'note': 'AST_QUALNAME: f\nBEACON (AST)\nid: b1@f\nrole: b1\nslice_labels: lab\nkind: ast\n'},
    ], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def g():',
        '    return 2', '', '', 'def f():', '    return 1',
    ]),
    ('py_p13_stacked_two_ast.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '# @beacon[',
        '#   id=b2@f,', '#   slice_labels=other,', '#   kind=ast,', '# ]', 'def f():', '    return 1',
    ]),
    ('py_p14_span_plus_ast.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=s1@0,', '#   slice_labels=region,', '#   kind=span,', '# ]', '# @beacon[',
        '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def f():', '    return 1', '# @beacon-close[',
        '#   id=s1@0,', '# ]',
    ]),
    ('py_p15_span_directly_above.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=s1@0,', '#   slice_labels=region,', '#   kind=span,', '# ]', 'def f():',
        '    return 1', '# @beacon-close[', '#   id=s1@0,', '# ]', '', '', 'def g():', '    return 2',
    ]),
    ('py_p16_implicit_kind.py', 'python', ['f', {'remove_targets': ['f']}], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '# ]', 'def f():', '    return 1',
    ]),
    ('py_p17_property.py', 'python', ['C.x', 'C.x#2'], [
        'class C:', '    @property', '    def x(self):', '        return 1', '', '    # @beacon[', '    #   id=b1@C.x,',
        '    #   slice_labels=lab,', '    #   kind=ast,', '    # ]', '    @x.setter', '    def x(self, v):', '        pass',
    ]),
    ('py_p18_overload.py', 'python', ['f', 'f#3'], [
        'from typing import overload', '', '', '@overload', 'def f(a: int) -> int: ...', '@overload',
        'def f(a: str) -> str: ...', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]',
        'def f(a):', '    return a',
    ]),
    ('py_p19_conditional.py', 'python', ['f'], [
        'import sys', '', 'if sys.version_info >= (3,):', '    # @beacon[', '    #   id=b1@f,', '    #   slice_labels=lab,',
        '    #   kind=ast,', '    # ]', '    def f():', '        return 3', 'else:', '    def f():', '        return 2',
    ]),
    ('py_p20_redefined.py', 'python', ['f', 'f#2'], [
        'def f():', '    return 1', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]',
        'def f():', '    return 2',
    ]),
    ('py_p21_close_between.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=s1@0,', '#   slice_labels=region,', '#   kind=span,', '# ]', 'X = 1',
        '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '# @beacon-close[', '#   id=s1@0,',
        '# ]', 'def f():', '    return 1',
    ]),
    ('py_p22_comment_between_decos.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '# @beacon[', '#   id=b1@f,',
        '#   slice_labels=lab,', '#   kind=ast,', '# ]', '@deco', '# second decorator below', '@deco', 'def f():',
        '    return 1',
    ]),
    ('py_p23_block_in_deco_expr.py', 'python', ['f'], [
        'def deco(fn=None, *a):', '    return fn if callable(fn) else (lambda g: g)', '', '', '@deco(', '    # @beacon[',
        '    #   id=b1@f,', '    #   slice_labels=lab,', '    #   kind=ast,', '    # ]', '    None,', ')', 'def f():',
        '    return 1',
    ]),
    ('py_p24_eof.py', 'python', ['f'], [
        'def f():', '    return 1', '', '', '# @beacon[', '#   id=b1@gone,', '#   slice_labels=lab,', '#   kind=ast,', '# ]',
    ]),
    ('py_p25_method_comment.py', 'python', ['C.m'], [
        'class C:', '    # @beacon[', '    #   id=b1@C.m,', '    #   slice_labels=lab,', '    #   kind=ast,', '    # ]',
        '    # the m method', '    def m(self):', '        return 1',
    ]),
    ('py_p26_const.py', 'python', ['LIMIT'], [
        'import os', '', '', '# @beacon[', '#   id=b1@LIMIT,', '#   slice_labels=lab,', '# ]', 'LIMIT = 10',
    ]),
    ('py_p27_cf19_stale_id_comment.py', 'python', [
        {'t': 'f', 'note': 'AST_QUALNAME: f\nBEACON (AST)\nid: b9@f\nrole: b9\nslice_labels: lab\nkind: ast\n'},
    ], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', '# a note about f',
        'def f():', '    return 1',
    ]),
    ('py_p28_cf19_no_id_code.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', "print('x')",
        'def f():', '    return 1',
    ]),
    ('py_p29_docstring_func.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def f():',
        '    """Doc."""', '    return 1',
    ]),
    ('py_p30_close_then_comment.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=s1@0,', '#   slice_labels=region,', '#   kind=span,', '# ]', 'X = 1',
        '# @beacon-close[', '#   id=s1@0,', '# ]', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,',
        '# ]', 'def f():', '    return 1',
    ]),
    ('py_p31_nested.py', 'python', ['outer.inner'], [
        'def outer():', '    # @beacon[', '    #   id=b1@outer.inner,', '    #   slice_labels=lab,', '    #   kind=ast,',
        '    # ]', '    # helper', '    def inner():', '        return 1', '    return inner',
    ]),
    ('js_j00_base.js', 'js_ts', ['f', 'g'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', 'function f() {', '  return 1;',
        '}', '', 'function g() {', '  return 2;', '}',
    ]),
    ('js_j01_jsdoc.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', '/**', ' * Does f.',
        ' * @param a the input', ' */', 'function f(a) {', '  return a;', '}',
    ]),
    ('js_j02_linecomment.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', '// a note about f',
        'function f() {', '  return 1;', '}',
    ]),
    ('js_j03_ts_decorator.ts', 'js_ts', ['Foo'], [
        '// @beacon[', '//   id=b1@Foo,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', '@Component({})',
        'export class Foo {', '  run() {', '    return 1;', '  }', '}',
    ]),
    ('js_j04_ts_between.ts', 'js_ts', ['Foo'], [
        '@Component({})', '// @beacon[', '//   id=b1@Foo,', '//   slice_labels=lab,', '//   kind=ast,', '// ]',
        'export class Foo {', '  run() {', '    return 1;', '  }', '}',
    ]),
    ('js_j05_span_above.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=s1@0,', '//   slice_labels=region,', '//   kind=span,', '// ]', 'function f() {',
        '  return 1;', '}', '// @beacon-close[', '//   id=s1@0,', '// ]',
    ]),
    ('js_j06_implicit_kind.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '// ]', 'function f() {', '  return 1;', '}',
    ]),
    ('js_j07_getset.js', 'js_ts', ['C.x', 'C.x#2'], [
        'class C {', '  get x() {', '    return 1;', '  }', '  // @beacon[', '  //   id=b1@C.x,', '  //   slice_labels=lab,',
        '  //   kind=ast,', '  // ]', '  set x(v) {', '    this._x = v;', '  }', '}',
    ]),
    ('js_j08_ts_overload.ts', 'js_ts', ['f'], [
        'function f(a: string): void;', 'function f(a: number): void;', '// @beacon[', '//   id=b1@f,',
        '//   slice_labels=lab,', '//   kind=ast,', '// ]', 'function f(a: any) {', '  return a;', '}',
    ]),
    ('js_j09_generator.js', 'js_ts', ['C.gen'], [
        'class C {', '  // @beacon[', '  //   id=b1@C.gen,', '  //   slice_labels=lab,', '  //   kind=ast,', '  // ]',
        '  *gen() {', '    yield 1;', '  }', '}',
    ]),
    ('js_j10_arrow_const.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', 'const f = () => {', '  return 1;',
        '};',
    ]),
    ('js_j11_code_between.js', 'js_ts', ['f'], [
        '// @beacon[', '//   id=b1@f,', '//   slice_labels=lab,', '//   kind=ast,', '// ]', "console.log('x');",
        'function f() {', '  return 1;', '}',
    ]),
    ('md_m00_base.md', 'markdown', ['Section A', 'Section B'], [
        '# Title', '', 'intro', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->',
        '## Section A', '', 'text a', '', '## Section B', '', 'text b',
    ]),
    ('md_m01_blank.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '', '',
        '## Section A', '', 'text a',
    ]),
    ('md_m02_prose_between.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '',
        'Some prose that belongs to Title.', '', '## Section A', '', 'text a',
    ]),
    ('md_m03a_html_comment_one_line.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->',
        '<!-- editor note -->', '## Section A', '', 'text a',
    ]),
    ('md_m03b_html_comment_multi.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '<!--',
        'editor note', '-->', '## Section A', '', 'text a',
    ]),
    ('md_m04_ast_no_heading_after.md', 'markdown', ['Section A'], [
        '# Title', '', '## Section A', '', 'text a', '', '<!--', '@beacon[', '  id=h-x,', '  slice_labels=lab,', '  kind=ast,',
        ']', '-->', '', 'closing remark',
    ]),
    ('md_m05_implicit_prose.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', ']', '-->', '', 'prose', '', '## Section A', '',
        'text a',
    ]),
    ('md_m06_heading_renamed.md', 'markdown', [
        {'t': 'Section Alpha', 'note': 'MD_PATH:\n# Title\n## Section A\n---\n\nBEACON (MD AST)\nid: h-a\nrole: Section A\nslice_labels: lab\nkind: ast\n---\n'},
    ], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '## Section Alpha',
        '', 'text a',
    ]),
    ('md_m07_heading_moved.md', 'markdown', ['Section B',
        {'t': 'Section A', 'note': 'MD_PATH:\n# Title\n## Section A\n---\n\nBEACON (MD AST)\nid: h-a\nrole: Section A\nslice_labels: lab\nkind: ast\n---\n'},
    ], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '## Section B', '',
        'text b', '', '## Section A', '', 'text a',
    ]),
    ('md_m08_dup_headings.md', 'markdown', ['Notes', 'Notes#2'], [
        '# Title', '', '## Notes', '', 'one', '', '<!--', '@beacon[', '  id=h-n2,', '  slice_labels=lab,', '  kind=ast,', ']',
        '-->', '## Notes', '', 'two',
    ]),
    ('md_m09_front_matter.md', 'markdown', ['Title'], [
        '---', 'title: x', '---', '', '<!--', '@beacon[', '  id=h-t,', '  slice_labels=lab,', '  kind=ast,', ']', '-->',
        '# Title', '', 'text',
    ]),
    ('md_m10_fence_hash.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-f,', '  slice_labels=lab,', ']', '-->', '```bash', '# install', 'npm i',
        '```', '', '## Section A', '', 'text a',
    ]),
    ('md_m11_stacked.md', 'markdown', ['Section A'], [
        '# Title', '', '<!--', '@beacon[', '  id=h-a1,', '  slice_labels=lab,', '  kind=ast,', ']', '-->', '<!--', '@beacon[',
        '  id=h-a2,', '  slice_labels=other,', '  kind=ast,', ']', '-->', '## Section A', '', 'text a',
    ]),
    ('tw_py_cond_else.py', 'python', ['f#2'], [
        'import sys', '', 'if sys.version_info >= (3,):', '    # @beacon[', '    #   id=b1@f,', '    #   slice_labels=lab,',
        '    #   kind=ast,', '    # ]', '    def f():', '        return 3', 'else:', '    def f():', '        return 2',
    ]),
    ('tw_py_getter_block_setter_tag.py', 'python', ['C.x#2'], [
        'class C:', '    # @beacon[', '    #   id=b1@C.x,', '    #   slice_labels=lab,', '    #   kind=ast,',
        '    #   comment=getter doc,', '    # ]', '    @property', '    def x(self):', '        return 1', '', '    @x.setter',
        '    def x(self, v):', '        pass',
    ]),
    ('tw_py_redef_second_tag.py', 'python', ['f#2'], [
        '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ]', 'def f():', '    return 1', '', '',
        'def f():', '    return 2',
    ]),
    ('tw_js_get_block_set_tag.js', 'js_ts', ['C.x#2'], [
        'class C {', '  // @beacon[', '  //   id=b1@C.x,', '  //   slice_labels=lab,', '  //   kind=ast,', '  // ]',
        '  get x() {', '    return 1;', '  }', '  set x(v) {', '    this._x = v;', '  }', '}',
    ]),
    ('x_no_later_bracket.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ],', 'def f():',
        '    return 1',
    ]),
    ('x_later_bracket.py', 'python', ['f'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,', '# ],', 'def f():',
        '    a = [', '        1,', '    ]', '    return a',
    ]),
    ('case2_adopt.py', 'python', ['ƒ f()'], [
        'import os', '', '', '# @beacon[', '#   id=b1@f,', '#   slice_labels=lab,', '#   kind=ast,',
        '#   comment=f computes the thing,', '# ]', 'def f():', '    return 1',
    ]),
    ('case2_adopt_span.py', 'python', ['ƒ f()'], [
        'import os', '', '', '# @beacon[', '#   id=s1@0,', '#   slice_labels=region,', '#   kind=span,', '# ]', 'def f():',
        '    return 1', '# @beacon-close[', '#   id=s1@0,', '# ]', '', '', 'def g():', '    return 2',
    ]),
]
