"""U03 §6.4–6.6: scoped semantic fields, owned value splices and exact intents.

Old failures distinguished: p1 punctuation/quote loss and false conflicts;
p2 opaque-key glue/rebuilding; p3 phantom/erased/adopted comments; p5 duplicates;
A4 implicit kind materialization. All mutation checks compare original bytes.
"""
import pytest

import beacon_mutation_contract as bmc
import nexus_map_codebase as nmc
from beacon_source import source_snapshot_from_bytes
from beacon_edits import commit_beacon_edit_plan, verify_beacon_edit_plan
from beacon_field_layouts import COMMENTS, STYLES, RICH, SPAN, DOCSTRING, ADOPT_PY, ADOPT_MD, SNIPPETS, VUE


def raw(rows):
    return ('\n'.join(rows) + '\n').encode()


def mutation(path, language='python', bid='b@f', kind='delta_ensure', **intent):
    snapshot = nmc.read_source_snapshot(path)
    target = {'language': language, 'note_beacon_id': bid, 'operation': kind}
    selected = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, language), target)
    return nmc.plan_beacon_node_mutation(snapshot, selected, {'kind': kind, 'tags': ['new'], **intent})


def publish(result):
    assert result['success'], result
    assert result['edit_plan'] is not None, result
    written = commit_beacon_edit_plan(result['edit_plan'], scanner=nmc._beacon_edit_records)
    assert written['success'], written
    return written


@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('comment', COMMENTS, ids=lambda c: c[:45])
def test_comment_matrix_roundtrip_and_semantic_idempotence(style, comment):
    language, section = style
    try:
        accepted = bmc.validate_beacon_comment_text(comment)
    except ValueError:
        assert any(t in comment for t in ['-->', '*/', '/*', '&amp;', '\t', '\u2028', '\x85'])
        return
    rows = nmc._build_beacon_block_for_language(language, bid='t@1', role=None, slice_labels='a,b',
        kind='span', show_span=None, comment_text=accepted, section=section)
    # A section's wrapper grammar is scanned directly; whole SFC routing is
    # separately covered by the plan matrix below.
    grammar = {'template': 'markdown', 'style': 'js_ts', 'script': 'js_ts'}.get(section, language)
    snapshot = source_snapshot_from_bytes(raw(rows))
    parsed = nmc._beacon_edit_records(snapshot, grammar)[0]['fields']['comment']
    assert parsed == accepted
    for intent in [bmc.BeaconCommentIntent.fill_if_empty(accepted), bmc.BeaconCommentIntent.replace(accepted)]:
        resolved = bmc.resolve_beacon_comment(intent=intent, note_comment='stale',
            disk_comment=parsed, beacon_exists_on_disk=True)
        assert resolved.outcome == 'idempotent' and resolved.resolved == accepted
        assert resolved.warning is None


@pytest.mark.parametrize('filename', RICH)
def test_rich_fields_are_raw_identical_except_requested_label_value(tmp_path, filename):
    language, bid, rows = RICH[filename]
    path = tmp_path / filename
    before = raw(rows)
    path.write_bytes(before)
    result = mutation(path, language, bid)
    assert result['planned_operation'] == 'update'
    assert len(result['edit_plan'].edits) == 1
    publish(result)
    assert path.read_bytes() == before.replace(b'slice_labels=zeta,alpha,', b'slice_labels=zeta,alpha,new,')
    assert mutation(path, language, bid)['planned_operation'] == 'noop'


def test_scoped_note_records_never_start_after_ordinary_note_text():
    note = '\n'.join(['id: phantom', 'comment: phantom', 'BEACON (AST)', 'id: b@f',
        'kind: ast', 'role: mine', 'show_span: false', 'comment: x &gt; y,',
        'slice_labels: one,two', '', 'SPAN TEXT', 'id: evil', 'kind: span',
        'show_span: true', 'comment: Optional[str] = None', '---',
        'BEACON (SPAN)', 'id: second', 'comments: &amp;lt; remains encoded', '---',
        'CONTEXT COMMENTS', 'comment: not metadata'])
    records = bmc.parse_beacon_note_metadata(note)
    assert records == []
    assert nmc._extract_beacon_id_from_note(note) is None
    from workflowy_mcp.client.api_client_nexus import _extract_note_beacon_comment
    assert _extract_note_beacon_comment('\n'.join(['BEACON (SPAN)', 'id: b@f', '',
        'SPAN TEXT', 'comment: Optional[str] = None'])) is None


@pytest.mark.parametrize('mode', ['preserve', 'fill_if_empty'])
def test_existing_api_never_resurrects_stale_note_text(tmp_path, mode):
    path = tmp_path / 'span.py'
    path.write_bytes(raw(SPAN))
    before = path.read_bytes()
    intent = bmc.BeaconCommentIntent.preserve() if mode == 'preserve' else bmc.BeaconCommentIntent.fill_if_empty('the real comment')
    result = mutation(path, bid='rec@fields', comment_intent=intent,
        note='BEACON (SPAN)\nid: rec@fields\ncomment: stale\n\nSPAN TEXT\ncomment: Optional[str] = None')
    publish(result)
    assert b'stale' not in path.read_bytes()
    if mode == 'preserve':
        assert path.read_bytes() == before.replace(b'slice_labels=keep,', b'slice_labels=keep,new,')
    else:
        assert result['comment_outcome'] == 'fill'
        assert b'comment=the real comment,' in path.read_bytes()
        assert mutation(path, bid='rec@fields', comment_intent=intent)['planned_operation'] == 'noop'


@pytest.mark.parametrize('note_comment', [None, 'new,', '', 'arrows --&gt; are fun'])
def test_absolute_comment_authority_absence_conflicts_and_field_errors(tmp_path, note_comment):
    path = tmp_path / 'absolute.py'
    before = raw(ADOPT_PY)
    path.write_bytes(before)
    note = 'BEACON (AST)\nid: keep@f\nkind: ast\n'
    if note_comment is not None:
        note += 'comment: ' + note_comment + '\n'
    result = mutation(path, bid='keep@f', kind='absolute_labels', tags=['new'], note=note)
    assert result['removed_labels'] == ['old1', 'old2']
    publish(result)
    parsed = nmc._beacon_edit_records(nmc.read_source_snapshot(path), 'python')[0]['fields']
    assert parsed['slice_labels'] == 'new'
    if note_comment is None or '&gt;' in note_comment:
        assert parsed['comment'] == 'an existing careful comment'
        assert result.get('comment_conflict') is None
    else:
        assert parsed['comment'] == note_comment
        assert result['comment_conflict'] == {'field': 'comment', 'old': 'an existing careful comment', 'new': note_comment or None}
    if note_comment and '&gt;' in note_comment:
        assert result['field_errors'][0]['error_kind'] == 'invalid_comment'
        assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=new,')


@pytest.mark.parametrize('kind,note,expected,removed', [
    ('delta_ensure', '', 'old1,old2,new', []),
    ('delta_remove', '', 'old2', []),
    ('absolute_labels', 'BEACON (AST)\nid: keep@f\n', 'new', ['old1', 'old2']),
    ('absolute_labels', 'AST_QUALNAME: f\n', 'old1,old2,new', []),
])
def test_delta_absolute_and_headerless_adoption_are_distinct(tmp_path, kind, note, expected, removed):
    path = tmp_path / 'labels.py'
    path.write_bytes(raw(ADOPT_PY))
    tags = ['old1'] if kind == 'delta_remove' else ['new', 'new']
    result = mutation(path, bid='keep@f', kind=kind, tags=tags, note=note)
    assert result['removed_labels'] == removed
    publish(result)
    assert path.read_bytes() == raw(ADOPT_PY).replace(b'slice_labels=old1,old2,', ('slice_labels=' + expected + ',').encode())


def test_canonical_dedupe_and_delta_tokens_never_cascade(tmp_path):
    assert nmc._canonicalize_slice_labels('t1,more,more,a.b,a-b,漢,漢', None) == 't1,more,a-b,漢'
    path = tmp_path / 'tokens.py'
    rows = [r.replace('old1,old2', 'mci,mci-lease,mci-lease') for r in ADOPT_PY]
    path.write_bytes(raw(rows))
    result = mutation(path, bid='keep@f', kind='delta_remove', tags=['mci'])
    publish(result)
    assert path.read_bytes() == raw(rows).replace(b'slice_labels=mci,mci-lease,mci-lease,', b'slice_labels=mci-lease,')


@pytest.mark.parametrize('ambiguous_field', ['comment', 'slice_labels'])
def test_duplicate_field_is_local_and_independent_field_commits(tmp_path, ambiguous_field):
    path = tmp_path / 'duplicates.py'
    rows = ADOPT_PY.copy()
    rows.insert(2, '#   ' + ambiguous_field + '=another,')
    before = raw(rows)
    path.write_bytes(before)
    result = mutation(path, bid='keep@f', comment_intent=bmc.BeaconCommentIntent.replace('requested'))
    assert result['field_errors'][0]['field'] == ambiguous_field
    publish(result)
    if ambiguous_field == 'comment':
        assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=old1,old2,new,')
    else:
        assert path.read_bytes() == before.replace(b'comment=an existing careful comment,', b'comment=requested,')


@pytest.mark.parametrize('prefix,opener,closer,language,tail', [
    ('# ', '# @beacon[', '# ]', 'python', 'def f(): pass'),
    (' * ', '/** @beacon[', ' */', 'js_ts', 'function f() {}'),
    (' ', '/* @beacon[', ' */', 'js_ts', 'function f() {}'),
    ('', '<!-- @beacon[', '-->', 'markdown', '# Heading'),
])
def test_multiline_values_wrapper_prefixes_and_missing_fields(tmp_path, prefix, opener, closer, language, tail):
    rows = [opener, prefix + 'id=b@f,', prefix + 'slice_labels=old,', prefix + 'more,',
            prefix.rstrip(), prefix + 'kind=ast,']
    if closer.strip() in {'*/', '-->'}:
        rows += [prefix + ']', closer]
    else:
        rows += [closer]
    rows += [tail]
    path = tmp_path / ('rich.md' if language == 'markdown' else 'rich.js' if language == 'js_ts' else 'rich.py')
    before = raw(rows)
    path.write_bytes(before)
    result = mutation(path, language, comment_intent=bmc.BeaconCommentIntent.replace('tail ],'))
    publish(result)
    expected = before.replace((prefix + 'slice_labels=old,').encode(), (prefix + 'slice_labels=old,more,new,').encode())
    expected = expected.replace(('\n' + prefix + 'more,\n').encode(), ('\n' + prefix + '\n').encode())
    bracket_line = (prefix + ']' if closer.strip() in {'*/', '-->'} else closer).encode()
    expected = expected.replace(bracket_line, (prefix + 'comment="tail ],",\n').encode() + bracket_line)
    assert path.read_bytes() == expected


def test_implicit_kind_and_unrequested_snippets_stay_exact(tmp_path):
    for language, rows, bid in [('python', [r for r in ADOPT_PY if 'kind=' not in r], 'keep@f'),
                               ('python', SNIPPETS, 's@snip'),
                               ('markdown', [r for r in ADOPT_MD if 'kind=' not in r], 'md@sec')]:
        path = tmp_path / ('fields.md' if language == 'markdown' else 'fields.py')
        before = raw(rows)
        path.write_bytes(before)
        publish(mutation(path, language, bid))
        old = b'slice_labels=old1,old2,' if bid == 'keep@f' else b'slice_labels=keep,' if bid == 's@snip' else b'slice_labels=old1,'
        assert path.read_bytes() == before.replace(old, old[:-1] + b',new,')


def test_create_scoped_fallback_validates_and_mints_no_role(tmp_path):
    path = tmp_path / 'create.py'
    path.write_bytes(raw(DOCSTRING))
    snapshot = nmc.read_source_snapshot(path)
    selected = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, 'python'),
        {'language': 'python', 'ast_qualname': 'f', 'operation': 'delta_ensure'})
    result = nmc.plan_beacon_node_mutation(snapshot, selected, {'kind': 'delta_ensure', 'tags': ['new'],
        'create_id': 'new@f', 'note': 'BEACON (AST)\nid: gone\ncomment: note fallback,\n\ncomment: phantom'})
    publish(result)
    assert b'role=' not in path.read_bytes()
    assert b'comment="note fallback,",' in path.read_bytes()
    assert b'comment=phantom' not in path.read_bytes()


def test_vue_script_fields_preserve_other_sections_and_comment(tmp_path):
    path = tmp_path / 'fields.vue'
    before = raw(VUE)
    path.write_bytes(before)
    publish(mutation(path, 'vue', 's0@a'))
    assert path.read_bytes() == before.replace(b'slice_labels=a0,', b'slice_labels=a0,new,')
    refused = mutation(path, 'vue', 'tpl@1')
    assert not refused['success'] and refused['error_kind'] == 'operation_not_supported_for_target'
    # Capability refusal leaves a writable script neighbour in this same file.
    publish(mutation(path, 'vue', 's1@b'))


def test_unquoted_historical_loss_is_not_guessed():
    assert bmc.parse_beacon_field_lines(['comment=old ],'])['comment'] == 'old'
    assert bmc.resolve_beacon_comment(intent=bmc.BeaconCommentIntent.preserve(),
        disk_comment='old', note_comment='old ]', beacon_exists_on_disk=True).resolved == 'old'


@pytest.mark.parametrize('case', ['rich', 'phantom', 'deleted_preserve', 'deleted_fill', 'implicit', 'punctuation'])
def test_existing_plan_and_executor_interfaces_field_witnesses(tmp_path, case):
    """These pre-existing interfaces fail behaviorally at the unit base, not
    merely because a new selector/parser/planner surface is absent there."""
    from workflowy_mcp.client import api_client_nexus as acn
    from workflowy_mcp.lightning_rod_mutation_spec import BeaconMutationOperation
    rows = ADOPT_PY.copy()
    note_extra = ''
    intent = bmc.BeaconCommentIntent.preserve()
    expected_comment = 'an existing careful comment'
    if case == 'rich':
        rows.insert(2, '#   owner=dan,')
    elif case in {'phantom', 'deleted_preserve', 'deleted_fill'}:
        rows = [r for r in rows if 'comment=' not in r]
        note_extra = ('\nSPAN TEXT\ncomment: Optional[str] = None' if case == 'phantom'
                      else 'comment: stale human-deleted comment\n')
        expected_comment = None
        if case == 'deleted_fill':
            intent = bmc.BeaconCommentIntent.fill_if_empty('the real comment')
            expected_comment = 'the real comment'
    elif case == 'implicit':
        rows = [r for r in rows if 'kind=' not in r]
    else:
        intent = bmc.BeaconCommentIntent.replace('returns list[int]')
        expected_comment = 'returns list[int]'
    path = tmp_path / 'public.py'
    before = raw(rows)
    path.write_bytes(before)
    note = 'AST_QUALNAME: f\n---\nBEACON (AST)\nid: keep@f\nkind: ast\n' + note_extra
    cache = {'file': {'id': 'file', 'name': path.name, 'note': 'Path: ' + str(path), 'parent_id': None},
             'node': {'id': 'node', 'name': 'f #old1 #old2', 'note': note, 'parent_id': 'file'}}
    operation = BeaconMutationOperation(input_index=0, handle='node', tags=('new',), comment=intent)
    plan = acn._compute_beacon_write_plan(cache, 'node', operation, 'ensure_beacons', include_fingerprint=True)
    assert plan.success and plan.planned_operation == 'update', plan
    assert plan.resolved_comment == expected_comment
    committed = nmc._execute_beacon_write_plan(language='python', file_path=str(path), name=plan.intended_name,
        note=note, write_intent=plan.write_intent, write_plan=plan)
    assert committed.get('success'), committed
    if case in {'rich', 'phantom', 'deleted_preserve', 'implicit'}:
        assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=old1,old2,new,')
    else:
        parsed = nmc._beacon_edit_records(nmc.read_source_snapshot(path), 'python')[0]['fields']
        assert parsed['comment'] == expected_comment
    # A rerun is a real noop at the public plan seam, not a needless rewrite.
    again = acn._compute_beacon_write_plan(cache, 'node', operation, 'ensure_beacons', include_fingerprint=True)
    assert again.success and again.planned_operation == 'noop', again


def test_tagless_absolute_deletes_only_selected_block(tmp_path):
    rows = ADOPT_PY + ['', *[r.replace('keep@f', 'safe@g').replace('def f', 'def g') for r in ADOPT_PY]]
    path = tmp_path / 'delete.py'
    before = raw(rows)
    path.write_bytes(before)
    result = mutation(path, bid='keep@f', kind='absolute_labels', tags=[], note='BEACON (AST)\nid: keep@f')
    assert result['planned_operation'] == 'delete'
    assert result['removed_labels'] == ['old1', 'old2']
    publish(result)
    assert path.read_bytes() == raw(['def f():', '    return 1', '', *rows[len(ADOPT_PY) + 1:]])
    publish(mutation(path, bid='safe@g'))


def test_invalid_create_note_comment_does_not_block_labels(tmp_path):
    path = tmp_path / 'new.py'
    path.write_bytes(raw(['def f():', '    pass']))
    snapshot = nmc.read_source_snapshot(path)
    selection = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, 'python'),
        {'language': 'python', 'ast_qualname': 'f', 'operation': 'delta_ensure'})
    result = nmc.plan_beacon_node_mutation(snapshot, selection, {'kind': 'delta_ensure', 'tags': ['new'],
        'create_id': 'b@f', 'note': 'BEACON (AST)\nid: old\ncomment: --&gt;\n'})
    assert result['field_errors'][0]['error_kind'] == 'invalid_comment'
    publish(result)
    assert b'slice_labels=new,' in path.read_bytes() and b'comment=' not in path.read_bytes()


def test_comment_alias_splice_keeps_its_key_and_other_bytes(tmp_path):
    path = tmp_path / 'alias.py'
    before = raw([r.replace('comment=', 'comments=') for r in ADOPT_PY])
    path.write_bytes(before)
    publish(mutation(path, bid='keep@f', comment_intent=bmc.BeaconCommentIntent.replace('new ],')))
    assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=old1,old2,new,').replace(
        b'comments=an existing careful comment,', b'comments="new ],",')


def test_inline_comment_ambiguity_does_not_block_owned_label_value(tmp_path):
    path = tmp_path / 'inline.py'
    before = raw(['# @beacon[ id=b@f, slice_labels=old, kind=ast, comment=describes kind=span, id=other, ]',
                  'def f():', '    pass'])
    path.write_bytes(before)
    result = mutation(path)
    assert not result['field_errors'], result
    publish(result)
    assert path.read_bytes() == before.replace(b'slice_labels=old,', b'slice_labels=old,new,')


@pytest.mark.parametrize('language,rows', [
    ('python', ['# @beacon[ id=b@f, kind=ast, ]', 'def f(): pass']),
    ('js_ts', ['/** @beacon[ id=b@f, kind=ast, ] */', 'function f() {}']),
    ('markdown', ['<!-- @beacon[ id=b@f, kind=ast, ] -->', '# Heading']),
])
def test_missing_fields_in_inline_blocks_use_prefixed_metadata_lines(tmp_path, language, rows):
    path = tmp_path / 'inline.txt'
    before = raw(rows)
    path.write_bytes(before)
    planned = mutation(path, language, comment_intent=bmc.BeaconCommentIntent.replace('new ],'))
    publish(planned)
    after = nmc.read_source_snapshot(path)
    fields = nmc._beacon_edit_records(after, language)[0]['fields']
    assert fields == {'id': 'b@f', 'kind': 'ast', 'slice_labels': 'new', 'comment': 'new ],'}
    assert path.read_bytes().endswith(raw(rows[1:]))
    assert mutation(path, language, comment_intent=bmc.BeaconCommentIntent.replace('new ],'))['planned_operation'] == 'noop'


def test_missing_comment_extent_is_field_local_to_known_label_splice(tmp_path):
    path = tmp_path / 'incomplete.py'
    before = raw(['# @beacon[', '# id=b@f,', '# slice_labels=old,', '# kind=ast,', 'def f(): pass'])
    path.write_bytes(before)
    planned = mutation(path, comment_intent=bmc.BeaconCommentIntent.replace('new comment'))
    assert planned['field_errors'][0]['field'] == 'comment'
    assert planned['field_errors'][0]['error_kind'] == 'beacon_extent_unreadable'
    publish(planned)
    assert path.read_bytes() == before.replace(b'slice_labels=old,', b'slice_labels=old,new,')


def test_preserve_does_not_strip_quoted_disk_interior_whitespace(tmp_path):
    path = tmp_path / 'space.py'
    before = raw([r.replace('comment=an existing careful comment,', 'comment="  disk text  ",') for r in ADOPT_PY])
    path.write_bytes(before)
    resolved = bmc.resolve_beacon_comment(intent=bmc.BeaconCommentIntent.preserve(),
        disk_comment='  disk text  ', note_comment='stale', beacon_exists_on_disk=True)
    assert resolved.resolved == '  disk text  '
    publish(mutation(path, bid='keep@f', resolved_comment=resolved.resolved))
    assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=old1,old2,new,')


LEGACY_FIELDS = [('python', '#', '.py', ['def f():', '    pass']),
                 ('js_ts', '//', '.ts', ['function f() {}']),
                 ('sql', '--', '.sql', ['SELECT 1;']),
                 ('shell', '#', '.sh', ['echo safe']),
                 ('yaml', '#', '.yaml', ['safe: true'])]


@pytest.mark.parametrize('filename', list(RICH))
def test_legacy_rich_fields_are_raw_identical_and_removals_are_reported(tmp_path, filename):
    language, bid, rows = RICH[filename]
    path = tmp_path / filename
    before = raw(rows)
    path.write_bytes(before)
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    note = 'BEACON (SPAN)\nid: ' + bid + '\nshow_span: false\nrole: stale\n'
    result = writer(str(path), 'Renamed #alpha #new #new', note)
    assert result['operation'] == 'updated_beacon', result
    assert result['removed_labels'] == ['zeta'] and result['slice_labels'] == 'alpha,new'
    assert path.read_bytes() == before.replace(b'slice_labels=zeta,alpha,', b'slice_labels=alpha,new,')
    unchanged, mtime = path.read_bytes(), path.stat().st_mtime_ns
    again = writer(str(path), 'Renamed #alpha #new #new', note)
    assert again['operation'] == 'noop' and again['noop_reason'] == 'already_satisfied', again
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (unchanged, mtime)


@pytest.mark.parametrize('language,prefix,suffix,code', LEGACY_FIELDS)
@pytest.mark.parametrize('comment', [None, 'new,', 'returns list[int]', '"whole quotes"', 'x=y', '漢🙂' * 2500, 'arrows --&gt; are fun'])
def test_legacy_scoped_comment_authority_conflicts_and_invalid_field_locality(tmp_path, language, prefix, suffix, code, comment):
    rows = [prefix + ' @beacon[', prefix + ' id=b@f,', prefix + ' slice_labels=old,',
            prefix + ' role=custom,', prefix + ' kind=span,', prefix + ' show_span=true,',
            prefix + ' start_snippet=begin,', prefix + ' end_snippet=end,',
            prefix + ' comment=disk text,', prefix + ' owner=dan,', prefix + ' ]', *code]
    path = tmp_path / ('fields' + suffix)
    before = ('\r\n'.join(rows) + '\r\n').encode()
    path.write_bytes(before)
    note = 'BEACON (SPAN)\nid: b@f\nshow_span: false\n'
    if comment is not None:
        note += 'comment: ' + comment + '\n'
    note += '\nSPAN TEXT\ncomment: phantom\nshow_span: false\nid: evil\n'
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'A rename #new #new', note)
    assert result['operation'] == 'updated_beacon', result
    assert result['role'] == 'custom' and result['removed_labels'] == ['old']
    expected = before.replace(b'slice_labels=old,', b'slice_labels=new,')
    if comment is None or '&gt;' in comment:
        assert result['comment'] == 'disk text' and result.get('comment_conflict') is None
        if comment:
            assert result['field_errors'][0]['error_kind'] == 'invalid_comment'
    else:
        assert result['comment'] == comment
        assert result['comment_conflict'] == {'field': 'comment', 'old': 'disk text', 'new': comment}
        protected = '"' + comment + '"' if comment[-1] in ',]' or comment[0] == comment[-1] == '"' else comment
        expected = expected.replace(b'comment=disk text,', ('comment=' + protected + ',').encode())
    assert path.read_bytes() == expected
    mtime = path.stat().st_mtime_ns
    again = writer(str(path), 'A rename #new #new', note)
    assert again['operation'] == 'noop', again
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (expected, mtime)


@pytest.mark.parametrize('language,prefix,suffix,code', LEGACY_FIELDS)
def test_legacy_duplicate_comment_does_not_block_labels_or_neighbour(tmp_path, language, prefix, suffix, code):
    rows = [prefix + ' @beacon[', prefix + ' id=b@f,', prefix + ' slice_labels=old,',
            prefix + ' kind=span,', prefix + ' comment=first,', prefix + ' comment=second,', prefix + ' ]', *code]
    safe = [r.replace('b@f', 'safe').replace('comment=second,', 'owner=dan,') for r in rows]
    path = tmp_path / ('duplicate' + suffix)
    before = raw(rows + safe)
    path.write_bytes(before)
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'target #new', 'BEACON (SPAN)\nid: b@f\ncomment: requested\n')
    assert result['operation'] == 'updated_beacon', result
    assert result['field_errors'][0]['field'] == 'comment'
    assert result['field_errors'][0]['error_kind'] == 'ambiguous_beacon_field'
    assert path.read_bytes() == before.replace(b'slice_labels=old,', b'slice_labels=new,', 1)
    expected = path.read_bytes()
    good = writer(str(path), 'safe #new', 'BEACON (SPAN)\nid: safe\n')
    assert good['operation'] == 'updated_beacon', good
    assert path.read_bytes() == expected.replace(b'slice_labels=old,', b'slice_labels=new,')


@pytest.mark.parametrize('language,prefix,suffix,code', LEGACY_FIELDS[:2])
def test_legacy_headerless_adoption_keeps_comment_labels_and_implicit_kind(tmp_path, language, prefix, suffix, code):
    rows = [prefix + ' @beacon[', prefix + ' id=b@f,', prefix + ' slice_labels=old,',
            prefix + ' role=custom,', prefix + ' comment=disk text,', prefix + ' ]', *code]
    path = tmp_path / ('adopt' + suffix)
    before = raw(rows)
    path.write_bytes(before)
    result = getattr(nmc, 'update_beacon_from_node_' + language)(str(path), 'f #new #new', 'AST_QUALNAME: f\n\ncomment: phantom\n')
    assert result['operation'] == 'updated_beacon' and result['adoption_additive'], result
    assert result['removed_labels'] == [] and result['beacon_id'] == 'b@f'
    assert path.read_bytes() == before.replace(b'slice_labels=old,', b'slice_labels=old,new,')


@pytest.mark.parametrize('case,rows,note', [
    ('stale-absent-comment', ADOPT_PY, 'AST_QUALNAME: f\nBEACON (AST)\nid: keep@f\n'),
    ('span-phantom', SPAN, 'BEACON (SPAN)\nid: rec@fields\n\nSPAN TEXT\ncomment: Optional[str] = None\n'),
    ('docstring-phantom', DOCSTRING, 'AST_QUALNAME: f\nBEACON (AST)\nid: absent\n\nDocstring\ncomment: phantom\n'),
])
def test_original_p3_legacy_absent_and_phantom_comment_witnesses(tmp_path, case, rows, note):
    path = tmp_path / 'p3.py'
    before = raw(rows)
    path.write_bytes(before)
    result = nmc.update_beacon_from_node_python(str(path), 'f #new', note)
    assert result['operation'] == ('created_beacon' if case == 'docstring-phantom' else 'updated_beacon'), result
    assert b'comment=phantom' not in path.read_bytes() and b'comment=Optional' not in path.read_bytes()
    if case == 'stale-absent-comment':
        assert path.read_bytes() == before.replace(b'slice_labels=old1,old2,', b'slice_labels=new,')
    elif case == 'span-phantom':
        assert path.read_bytes() == before.replace(b'slice_labels=keep,', b'slice_labels=new,')
    else:
        assert path.read_bytes().endswith(before) and result['comment'] is None


@pytest.mark.parametrize('style', ['vue-script', 'vue-template', 'vue-style', 'markdown'])
@pytest.mark.parametrize('comment', [None, 'new,', 'returns list[int]', '"whole quotes"', 'x=y', '漢🙂' * 2500, 'arrows --&gt; are fun'])
def test_vue_markdown_legacy_scoped_fields_and_comment_fidelity(tmp_path, style, comment):
    """004d1ba rebuilds wrappers/metadata; MD decodes an unsafe --> into source.
    Safe labels proceed on a rejected comment with every other byte unchanged.
    """
    html = style in {'markdown', 'vue-template'}
    prefix = '  ' if html else '   ' if style == 'vue-style' else '// '
    rows = (['<!--', '@beacon['] if html else ['/* @beacon['] if style == 'vue-style' else ['// @beacon[']) + [
        prefix + 'id=b@f,', prefix + 'slice_labels=old,', prefix + 'role=custom,',
        prefix + ('kind=ast,' if style in {'markdown', 'vue-script'} else 'kind=span,'),
        prefix + 'show_span=true,', prefix + 'start_snippet=begin,', prefix + 'end_snippet=end,',
        prefix + 'comment=disk text,', prefix + 'owner=dan,'] + (
        [']', '-->'] if html else ['] */'] if style == 'vue-style' else ['// ]'])
    language = 'markdown' if style == 'markdown' else 'vue'
    if style == 'markdown':
        rows += ['# Heading']
    else:
        section = style.removeprefix('vue-')
        rows = ['<' + section + '>'] + rows + [
            '<p>body</p>' if section == 'template' else '.body { color: red; }' if section == 'style' else 'function f() {}',
            '</' + section + '>']
    path = tmp_path / ('fields.md' if language == 'markdown' else 'fields.vue')
    before = ('\r\n'.join(rows) + '\r\n').encode()
    path.write_bytes(before)
    note = 'BEACON (AST)\nid: b@f\nkind: span\nshow_span: false\nrole: stale\n'
    if comment is not None:
        note += 'comment: ' + comment + '\n'
    note += '\nSPAN TEXT\nid: phantom\ncomment: phantom\n'
    writer = getattr(nmc, 'update_beacon_from_node_' + language)
    result = writer(str(path), 'Renamed #new #new', note)
    assert result['operation'] == 'updated_beacon' and result['removed_labels'] == ['old'], result
    assert result['role'] == 'custom'
    expected = before.replace(b'slice_labels=old,', b'slice_labels=new,')
    if comment is None or '&gt;' in comment:
        assert result['comment'] == 'disk text'
        if comment:
            assert result['field_errors'][0]['error_kind'] == 'invalid_comment'
    else:
        assert result['comment'] == comment
        protected = '"' + comment + '"' if comment.endswith((',', ']')) or comment[0] == comment[-1] == '"' else comment
        expected = expected.replace(b'comment=disk text,', ('comment=' + protected + ',').encode())
    assert path.read_bytes() == expected
    stamp = path.stat().st_mtime_ns
    assert writer(str(path), 'Renamed #new #new', note)['operation'] == 'noop'
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (expected, stamp)


@pytest.mark.parametrize('case', ['headerless', 'stale-id', 'unsafe-comment'])
def test_original_markdown_p3_adoption_resurrection_comment_and_owned_wrapper(tmp_path, case):
    from beacon_field_layouts import NOTES
    rows = ADOPT_MD.copy()
    before = raw(rows)
    path = tmp_path / 'p3.md'
    path.write_bytes(before)
    note = '\n'.join(NOTES['md_stale'] if case == 'stale-id' else NOTES['md_adopt']) + '\n'
    if case not in {'headerless', 'stale-id'}:
        note += 'BEACON (MD AST)\nid: md@sec\n'
    if case == 'unsafe-comment':
        note += 'comment: --&gt;\n'
    result = nmc.update_beacon_from_node_markdown(str(path), 'Section #new', note)
    assert result['operation'] == 'updated_beacon' and result['beacon_id'] == 'md@sec', result
    labels = b'old1,new' if case == 'headerless' else b'new'
    assert path.read_bytes() == before.replace(b'slice_labels=old1,', b'slice_labels=' + labels + b',')
    assert path.read_bytes().count(b'@beacon[') == 1
    if case == 'unsafe-comment':
        assert result['field_errors'][0]['error_kind'] == 'invalid_comment'
    if case == 'stale-id':
        assert any(d['type'] == 'beacon_id_adopted' and d['note_beacon_id'] == 'md@other' and
                   d['disk_beacon_id'] == 'md@sec' for d in result['diagnostics'])


@pytest.mark.parametrize('front', ['legacy', 'plan'])
def test_original_p7_author_note_above_inline_markdown_opener_is_not_owned(tmp_path, front):
    from beacon_field_layouts import AUTHOR_MD
    path = tmp_path / 'author.md'
    before = raw(AUTHOR_MD)
    path.write_bytes(before)
    note = 'MD_PATH:\n# Doc\n## Section\n---\nBEACON (MD AST)\nid: one@1\n'
    if front == 'legacy':
        result = nmc.update_beacon_from_node_markdown(str(path), 'Section #keep #new', note)
    else:
        from test_beacon_target_selection import delta_plan, commit_plan
        cache = {'file': {'id': 'file', 'note': 'Path: ' + str(path)},
                 'target': {'id': 'target', 'parent_id': 'file', 'name': 'Section #keep', 'note': note}}
        plan = delta_plan(cache, 'target')
        assert plan.success, plan
        result = commit_plan(path, plan, note)
    assert result['operation'] == 'updated_beacon', result
    assert path.read_bytes() == before.replace(b'slice_labels=keep,', b'slice_labels=keep,new,')
    assert path.read_bytes().startswith(b'# Doc\n\n<!-- an unrelated author note -->\n\n<!-- @beacon[')


def test_rehydrate_is_note_authoritative_at_heading_not_duplicate_id_elsewhere(tmp_path):
    """004d1ba edits the first duplicate; the opt-in did not exist.
    Rehydrate may deliberately restore a supplied id over the attached id,
    while absolute-label synchronization still returns its identity conflict.
    """
    rows = ['<!--', '@beacon[', '  id=shared,', '  slice_labels=first,', '  kind=ast,',
            '  comment=first comment,', ']', '-->', '# Alpha', '',
            '<!--', '@beacon[', '  id=local,', '  slice_labels=old,', '  kind=ast,',
            '  show_span=true,', '  comment=disk text,', '  role=custom,',
            '  start_snippet=begin,', '  owner=dan,', ']', '-->', '# Beta']
    path = tmp_path / 'rehydrate.md'
    before = ('\r\n'.join(rows) + '\r\n').encode()
    path.write_bytes(before)
    stamp = path.stat().st_mtime_ns
    note = 'MD_PATH:\n# Beta\n---\nBEACON (MD AST)\nid: shared\nkind: ast\nshow_span: false\ncomment: restored,\nrole: stale\n\nSPAN TEXT\ncomment: phantom\n'
    refused = nmc.update_beacon_from_node_markdown(str(path), 'Beta #new', note)
    assert refused['error_kind'] == 'beacon_identity_conflict' and refused['candidates'], refused
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)
    result = nmc.update_beacon_from_node_markdown(str(path), 'Beta #new', note, intent='rehydrate')
    assert result['operation'] == 'updated_beacon' and result['beacon_id'] == 'shared', result
    assert result['removed_labels'] == ['old'] and not result['adoption_additive']
    expected = before.replace(b'id=local,', b'id=shared,').replace(b'slice_labels=old,', b'slice_labels=new,').replace(
        b'show_span=true,', b'show_span=false,').replace(b'comment=disk text,', b'comment="restored,",')
    assert path.read_bytes() == expected and b'role=stale' not in path.read_bytes()
    current_stamp = path.stat().st_mtime_ns
    again = nmc.update_beacon_from_node_markdown(str(path), 'Beta #new', note, intent='rehydrate')
    assert again['operation'] == 'noop' and again['noop_reason'] == 'already_satisfied', again
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (expected, current_stamp)


def test_rehydrate_pure_planner_creates_from_scoped_header_and_updates_kind_by_splice(tmp_path):
    path = tmp_path / 'pure.md'
    pristine = b'# Heading\n'
    path.write_bytes(pristine)
    note = 'MD_PATH:\n# Heading\n---\nBEACON (MD AST)\nid: note@heading\nkind: ast\nshow_span: false\ncomment: &amp;lt; once\n'
    snapshot = nmc.read_source_snapshot(path)
    selected = nmc.select_beacon_block_for_node(snapshot, nmc._beacon_selection_structures(snapshot, 'markdown'),
        {'language': 'markdown', 'md_path': ['# Heading'], 'note_beacon_id': 'note@heading', 'operation': 'rehydrate'})
    result = nmc.plan_beacon_node_mutation(snapshot, selected, {'kind': 'rehydrate', 'name': 'Heading #new', 'note': note})
    publish(result)
    assert path.read_bytes().endswith(pristine) and b'role=' not in path.read_bytes()
    fields = nmc._beacon_edit_records(nmc.read_source_snapshot(path), 'markdown')[0]['fields']
    assert fields['id'] == 'note@heading' and fields['show_span'] == 'false'
    # The one transport decode leaves &lt;, rather than recursively becoming <.
    assert bmc.parse_beacon_note_metadata(note)[0]['comment'] == '&lt; once'
    assert not fields.get('comment') and result['field_errors'][0]['error_kind'] == 'invalid_comment'
    before = path.read_bytes()
    # Missing kind on disk is implicit AST at this adjacent heading. The
    # explicit note kind is requested metadata, not a default on ordinary edits.
    path.write_bytes(before.replace(b'  kind=ast,\n', b''))
    current = path.read_bytes()
    result = nmc.update_beacon_from_node_markdown(str(path), 'Heading #new', note, intent='rehydrate')
    assert result['operation'] == 'updated_beacon', result
    assert path.read_bytes().replace(b'  kind=ast,\n', b'') == current


def test_original_vue_p5_legacy_bracket_comment_and_stale_beta_never_rebuild(tmp_path):
    path = tmp_path / 'p5.vue'
    before = raw(VUE)
    path.write_bytes(before)
    alpha_note = 'AST_QUALNAME: vue:script0:alpha\nBEACON (JS/TS AST)\nid: s0@a\n'
    alpha = nmc.update_beacon_from_node_vue(str(path), 'alpha #a0 #new', alpha_note)
    assert alpha['operation'] == 'updated_beacon', alpha
    expected = before.replace(b'slice_labels=a0,', b'slice_labels=a0,new,')
    assert path.read_bytes() == expected
    beta_note = 'AST_QUALNAME: vue:script1:beta\n'
    beta = nmc.update_beacon_from_node_vue(str(path), 'beta #new', beta_note)
    assert beta['operation'] == 'updated_beacon' and beta['beacon_id'] == 's1@b', beta
    assert beta['adoption_additive']
    expected = expected.replace(b'slice_labels=b1,', b'slice_labels=b1,new,')
    assert path.read_bytes() == expected
    stamp = path.stat().st_mtime_ns
    for _ in range(3):
        assert nmc.update_beacon_from_node_vue(str(path), 'alpha #a0 #new', alpha_note)['operation'] == 'noop'
        assert nmc.update_beacon_from_node_vue(str(path), 'beta #new', beta_note)['operation'] == 'noop'
        assert (path.read_bytes(), path.stat().st_mtime_ns) == (expected, stamp)
