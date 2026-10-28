"""Quoted Area 5 P4 inputs; no final test reads the original probe or scratch."""

P4_PY = [
    'import os', '', '', '# @beacon[', '#   id=p@alpha,',
    '#   slice_labels=lab-a,', '#   kind=ast,', '#   comment=Alpha.,', '# ]',
    'def alpha():', '    return 1', '', '', '# @beacon[', '#   id=p@alpha,',
    '#   slice_labels=lab-b,', '#   kind=ast,', '#   comment=Beta copy-paste.,', '# ]',
    'def beta():', '    return 2', '', '', 'def gamma():', '    return 3',
]
P4_JS = [
    'const x = 1;', '', '// @beacon[', '//   id=j@alpha,',
    '//   slice_labels=lab-a,', '//   kind=ast,', '//   comment=Alpha.,', '// ]',
    'function alpha() {', '  return 1;', '}', '', '// @beacon[', '//   id=j@alpha,',
    '//   slice_labels=lab-b,', '//   kind=ast,', '//   comment=Beta copy-paste.,', '// ]',
    'function beta() {', '  return 2;', '}',
]
P4_SQL = [
    'SELECT 0;', '-- @beacon[', '--   id=q@s,', '--   slice_labels=lab-a,',
    '--   kind=span,', '-- ]', 'SELECT 1;', '-- @beacon-close[', '--   id=q@s,', '-- ]',
    '-- @beacon[', '--   id=q@s,', '--   slice_labels=lab-b,', '--   kind=span,', '-- ]',
    'SELECT 2;', '-- @beacon-close[', '--   id=q@s,', '-- ]',
]
P4_SPANS = [
    'import os', '', '# @beacon[', '#   id=p@s1,', '#   slice_labels=lab-s,',
    '#   kind=span,', '#   comment=Span one.,', '# ]', 'VALUE = 1',
    '# @beacon-close[', '#   id=p@s1,', '# ]', '', '# @beacon[', '#   id=p@s2,',
    '#   slice_labels=lab-t,', '#   kind=span,', '#   comment=Span two.,', '# ]',
    'OTHER = 2', '# @beacon-close[', '#   id=p@s2,', '# ]',
]
P4_MD = [
    '# Title', '', '<!--', '@beacon[', '  id=m@sec,', '  slice_labels=lab-a,',
    '  kind=ast,', '  comment=First.,', ']', '-->', '## Section A', '', 'Text A.', '',
    '<!--', '@beacon[', '  id=m@sec,', '  slice_labels=lab-b,', '  kind=ast,',
    '  comment=Second.,', ']', '-->', '## Section B', '', 'Text B.',
]
