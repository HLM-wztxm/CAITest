"""Offline validation independent of the JavaScript exporter; Python standard library only."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
import zipfile
from datetime import datetime, timezone


def sha(data):
    return hashlib.sha256(data).hexdigest()


def memory_request(character, session):
    """Independently derive the native Add payload from verified raw messages."""
    def text(value, code):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(code)
        return value

    if session['status'] != 'complete':
        raise ValueError('MEMORY_SESSION_INCOMPLETE')
    creator = session.get('metadata', {}).get('creator_id')
    human_ids = {t['author'].get('author_id') for t in session['turns'] if t['author'].get('is_human') is True}
    owner = str(creator) if creator is not None else next(iter(human_ids)) if len(human_ids) == 1 else None
    text(owner, 'MEMORY_USER_ID_MISSING')
    user_id = text('character-ai:' + owner, 'MEMORY_USER_ID_INVALID')
    session_id = text(session['session_id'], 'MEMORY_SESSION_ID_INVALID')
    messages = []
    for turn in session['turns']:
        human = turn['author'].get('is_human') is True
        if not human and turn['author'].get('author_id') != character['id']:
            raise ValueError('MEMORY_UNKNOWN_SPEAKER')
        role = text(turn['author'].get('name') if human else character['name'], 'MEMORY_SPEAKER_NAME_MISSING')
        candidate = next((c for c in turn['candidates'] if c['candidate_id'] == turn.get('primary_candidate_id')), None)
        if candidate is None:
            raise ValueError('MEMORY_SELECTED_CANDIDATE_MISSING')
        if not isinstance(candidate.get('raw_content'), str):
            raise ValueError('MEMORY_SELECTED_TEXT_MISSING')
        message = {'role': role, 'content': candidate['raw_content']}
        if turn.get('create_time') is not None:
            value = turn['create_time']
            try:
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})', value):
                    raise ValueError()
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
                timestamp = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
                if not 100_000_000_000 <= timestamp <= 32_503_680_000_000:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise ValueError('MEMORY_TIMESTAMP_INVALID') from None
            message['timestamp'] = timestamp
        messages.append(message)
    payload = {'user_id': user_id, 'session_id': session_id, 'messages': messages}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'request_id': 'cai-add-' + sha(encoded), **payload}


def verify(path):
    checks = 0
    def check(condition, description):
        nonlocal checks
        if not condition:
            raise ValueError(description)
        checks += 1

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        check(len(names) == len(set(names)), 'duplicate ZIP entry')
        check(all('/' not in n and '\\' not in n and n not in ('.', '..') for n in names), 'unsafe ZIP path')
        check(archive.testzip() is None, 'ZIP CRC mismatch')
        contents = {n: archive.read(n) for n in names}
    m = json.loads(contents['_manifest.json'])
    check(m.get('schema_version') in (1, 2), 'unsupported export schema')
    memory_format = m['schema_version'] == 2
    scope_type = m.get('scope_type', 'account')  # legacy v1.1 archives
    target = m.get('target_character_id')
    check(scope_type in ('account', 'character'), 'invalid scope type')
    scoped = scope_type == 'character'
    session_scope = m.get('session_scope', 'live')
    check(session_scope in ('live', 'initial'), 'invalid session scope')
    initial_only = session_scope == 'initial'
    check(not initial_only or scoped, 'initial session scope requires a character')
    check((isinstance(target, str) and re.fullmatch(r'[A-Za-z0-9_-]+', target)) if scoped else target is None,
          'invalid target character ID')
    complete = m['status'] == 'complete'
    check(set(names) == {f['name'] for f in m['files']} | {'_manifest.json'}, 'manifest file inventory mismatch')
    for f in m['files']:
        data = contents[f['name']]
        check(len(data) == f['bytes'] and sha(data) == f['sha256'], 'file SHA256/length mismatch: ' + f['name'])
    evidence = [json.loads(line) for line in contents['_evidence.jsonl'].decode('utf-8').splitlines() if line]
    phases = {}
    for i, e in enumerate(evidence):
        check(e['index'] == i, 'evidence index mismatch')
        check(sha(e['response_text'].encode('utf-8')) == e['sha256'], 'response SHA256 mismatch')
        phases.setdefault(e['phase'], []).append(e)
        if not scoped and e['phase'] in ('account_initial', 'account_final'):
            check('character_ids' not in e['query'], 'account enumeration is filtered')
        if scoped:
            check(e['path'] != '/chats/recent/', 'unscoped recent enumeration')
            data = json.loads(e['response_text'] or '{}')
            if e['path'] == '/chats/':
                check(e['query'].get('character_ids') == target, 'unscoped history request')
                check(all(c.get('character_id') == target for c in data.get('chats', [])), 'out-of-scope history evidence')
            if e['path'] == '/character/v1/get_character_infos':
                check(e.get('body', {}).get('external_ids') == [target], 'out-of-scope character request')
                check(all(c.get('external_id') == target for c in data.get('characters', [])), 'out-of-scope character evidence')

    if scoped:
        check(all(c['id'] == target for c in m['characters']), 'out-of-scope character file')
        check(all(c['id'] == target for c in m['enumeration']['sidebar']), 'out-of-scope sidebar')
        check(all(c['character_id'] == target for c in m['archive_access']), 'out-of-scope archive access')
        if complete:
            check(len(m['characters']) == 1, 'missing target character')

    def rows(phase, key):
        output = {}
        for e in phases.get(phase, []):
            data = json.loads(e['response_text'] or '{}')
            for row in data.get(key, []):
                rid = row['turn_key']['turn_id'] if key == 'turns' else row['chat_id']
                if rid in output:
                    check(output[rid] == row or not complete, 'conflicting source records')
                output[rid] = row
        return output

    def pagination(receipt, phase, key):
        records = phases.get(phase, [])
        check(receipt['evidence_indices'] == [e['index'] for e in records], 'pagination evidence mismatch')
        cursor = None
        for e in records:
            check(e['query'].get('next_token') == cursor, 'pagination cursor chain mismatch')
            data = json.loads(e['response_text'] or '{}')
            cursor = data.get('next_token') if key == 'chats' else data.get('meta', {}).get('next_token')
            cursor = cursor or None
        if complete:
            check(receipt['exhausted'] and cursor is None and not receipt.get('error'), 'unterminated pagination')

    initial_ids = set(rows('account_initial', 'chats'))
    if initial_only:
        snapshot = m.get('session_snapshot')
        if complete or snapshot is not None:
            check(isinstance(snapshot, dict), 'missing initial session snapshot')
            ids = snapshot.get('session_ids', [])
            check(len(ids) == len(set(ids)) and set(ids) == initial_ids, 'initial session snapshot differs from evidence')
            check(bool(snapshot.get('captured_at')), 'missing snapshot capture time')
            pagination(m['enumeration']['account_initial'], 'account_initial', 'chats')
        check(all(not e['path'].startswith('/turns/') or e['path'].split('/')[2] in initial_ids
                  for e in evidence), 'post-snapshot session messages read')

    all_sessions, totals, characters = set(), {'characters':0, 'sessions':0, 'turns':0, 'candidates':0, 'multi_candidate_turns':0}, []
    expected_add, conversion_issues = [], []
    for entry in m['characters']:
        role = json.loads(contents[entry['raw_file'] if memory_format else entry['file']])
        role_add = []
        check(role['character']['id'] == entry['id'] and role['character']['name'] == entry['name'], 'character mapping mismatch')
        totals['characters'] += 1
        characters.append({'name':entry['name'], 'file':entry['file'], 'sessions':len(role['sessions'])})
        pagination(role['history_pagination'], 'history:' + entry['id'], 'chats')
        role_ids = {s['session_id'] for s in role['sessions']}
        if initial_only:
            check(role_ids.issubset(initial_ids), 'post-snapshot session exported')
        check(set(entry['session_ids']) == role_ids and entry['sessions'] == len(role_ids), 'character session inventory mismatch')
        check(entry['turns'] == sum(len(s['turns']) for s in role['sessions']), 'character turn count mismatch')
        check(entry['candidates'] == sum(len(t['candidates']) for s in role['sessions'] for t in s['turns']), 'character candidate count mismatch')
        if complete:
            check(role['status'] == 'complete' and not role['issues'], 'incomplete character')
            expected_ids = set(rows('history:' + entry['id'], 'chats'))
            if initial_only:
                expected_ids &= initial_ids
            check(expected_ids.issubset(role_ids), 'character history omitted')
        for s in role['sessions']:
            sid = s['session_id']
            check(s['metadata']['character_id'] == entry['id'], 'session character mismatch')
            check(sid not in all_sessions, 'duplicate exported session')
            all_sessions.add(sid)
            totals['sessions'] += 1
            totals['turns'] += len(s['turns'])
            check(s['turn_count'] == len(s['turns']), 'turn count mismatch')
            initial = rows('turns_initial:' + sid, 'turns')
            actual = {t['turn_key']['turn_id']:t for t in s['turns']}
            check(len(actual) == len(s['turns']), 'duplicate exported turn')
            check(actual == initial, 'original turn objects differ from raw response')
            check(s['turns'] == list(reversed(list(initial.values()))), 'message order mismatch')
            if 'pagination' in s:
                pagination(s['pagination'], 'turns_initial:' + sid, 'turns')
            if s['status'] == 'complete':
                check(s['verification']['equal'], 'missing second-pass verification')
                check(actual == rows('turns_verify:' + sid, 'turns'), 'second-pass originals differ')
                pagination(s['verification']['pagination'], 'turns_verify:' + sid, 'turns')
                check(s['expected_turn_count'] is None or s['expected_turn_count'] == len(actual), 'server turn count mismatch')
            if complete:
                check(s['status'] == 'complete' and not s['issues'], 'incomplete session')
            for t in s['turns']:
                cs = t['candidates']
                totals['candidates'] += len(cs)
                totals['multi_candidate_turns'] += int(len(cs) > 1)
                if complete:
                    check(not cs or t['primary_candidate_id'] in {c['candidate_id'] for c in cs}, 'selected candidate missing')
                    check(all('raw_content' not in c or isinstance(c['raw_content'], str) for c in cs), 'invalid original text')
            if memory_format:
                try:
                    role_add.append(memory_request(role['character'], s))
                except ValueError as error:
                    conversion_issues.append({'code': str(error), 'character_id': entry['id'], 'session_id': sid})
        if memory_format:
            check(json.loads(contents[entry['file']]) == role_add, 'named Add payload differs from selected raw messages')
            check(entry['add_requests'] == len(role_add), 'character Add request count mismatch')
            expected_add.extend(role_add)
    if memory_format:
        info = m['memory_add']
        check(info['format'] == 'memory-plugin-leaderboard-add-v1', 'invalid Add format')
        check(json.loads(contents[info['file']]) == expected_add, 'combined Add payload differs from selected raw messages')
        check(info['requests'] == len(expected_add), 'Add request count mismatch')
        check(info['messages'] == sum(len(r['messages']) for r in expected_add), 'Add message count mismatch')
        check(info['issues'] == conversion_issues, 'Add conversion issues mismatch')
        check(all(issue in m['issues'] for issue in conversion_issues), 'unreported Add conversion issue')
        check(len({r['request_id'] for r in expected_add}) == len(expected_add), 'duplicate Add request ID')
        if complete:
            check(not conversion_issues and len(expected_add) == totals['sessions'], 'incomplete Add conversion')
    check(totals == m['totals'], 'overall counts mismatch')
    if complete:
        check(not m['issues'] and not m['incomplete_sessions'], 'manifest contains errors')
        check(m['enumeration']['stable'], 'unstable history inventory')
        for phase, name in [('account_initial','account_initial'), ('account_final','account_final')]:
            pagination(m['enumeration'][name], phase, 'chats')
            expected_ids = set(rows(phase, 'chats'))
            if initial_only:
                expected_ids &= initial_ids
            check(expected_ids.issubset(all_sessions), 'account sessions omitted')
        if initial_only:
            check(all_sessions == initial_ids, 'initial sessions omitted')
            later_ids = set(rows('account_final', 'chats')) | set(rows('history:' + target, 'chats'))
            check(set(m['enumeration'].get('post_snapshot_session_ids', [])) == later_ids - initial_ids,
                  'post-snapshot session inventory mismatch')
        authorized = {a['session_id'] for a in m['archive_access']}
        def inventory(phase):
            return sorted((c['chat_id'], c['character_id'],
                           'authorized_archive_access' if c['chat_id'] in authorized else c.get('turn_count'))
                          for c in rows(phase, 'chats').values()
                          if not initial_only or c['chat_id'] in initial_ids)
        check(inventory('account_initial') == inventory('account_final'), 'history inventory changed')
        for e in m['archive_access']:
            check(e['restore_status'] == 'verified', 'archive restoration not verified')
            restored = rows('rearchive_check:' + e['session_id'], 'chats')[e['session_id']]
            check(restored['state'] == e['original_state'], 'archive state differs')
        by_id = {e['id']:e['name'] for e in m['characters']}
        for sidebar in m['enumeration']['sidebar']:
            check(by_id.get(sidebar['id']) == sidebar['name'], 'sidebar name mismatch')
    return {'status':'passed' if complete else 'partial_verified', 'export_status':m['status'], 'checks':checks,
            'scope_type':scope_type, 'target_character_id':target,
            'session_scope': session_scope, 'session_snapshot': m.get('session_snapshot'),
            'totals':totals, 'characters':characters, 'archive_access':m['archive_access'],
            'raw_response_objects_equal':True, 'memory_add_verified':memory_format,
            'zip_sha256':sha(Path(path).read_bytes())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_path')
    parser.add_argument('--report')
    args = parser.parse_args()
    try:
        result = verify(args.zip_path)
        exit_code = 0 if result['status'] == 'passed' else 2
    except Exception as error:
        result = {'status':'failed', 'error':str(error)}
        exit_code = 1
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as stream:
            stream.write(output + '\n')
    print(output)
    sys.exit(exit_code)
