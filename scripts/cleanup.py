"""Journaled binding restoration and archival of this run's exact object IDs."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from common import Failure, save_json

SESSION_ERRORS = {'ACCOUNT_CHANGED', 'RESTORE_ACCOUNT_MISMATCH', 'CAI_HTTP_401', 'CAI_HTTP_403',
    'CAI_BROWSER_SESSION_CLOSED_OR_CHANGED', 'LOGIN_WINDOW_CLOSED'}


def read_state(state_dir):
    state = json.loads((state_dir / 'pending-restore.json').read_bytes())
    if state.get('schema_version', 1) not in (1, 2):
        raise Failure('UNSUPPORTED_RECOVERY_SCHEMA')
    return state


async def same_account(cai, state):
    me = await cai.me()
    if me['id'] != state['account_id']:
        raise Failure('RESTORE_ACCOUNT_MISMATCH')
    return me


def original_binding(settings, state):
    mapping = settings.get('personaOverrides', {})
    target = state['character_id']
    return (target in mapping, mapping.get(target)) == (state['original_present'], state['original_value'])


async def restore_binding(cai, state_dir):
    try:
        return await _restore_binding(cai, state_dir)
    except Exception as error:
        pending = state_dir / 'pending-restore.json'
        if pending.exists():
            state = read_state(state_dir)
            report = {'status': 'failed', 'code': error.code if isinstance(error, Failure) else 'RESTORE_FAILED'}
            if isinstance(error, Failure) and error.details:
                report['error_details'] = error.details
            if state.get('schema_version') == 2:
                state['binding_restore'] = report
                save_json(pending, state)
            if state.get('run_dir'):
                save_json(Path(state['run_dir']) / 'restore-result.json', report)
        raise


async def _restore_binding(cai, state_dir):
    pending = state_dir / 'pending-restore.json'
    if not pending.exists():
        return {'status': 'not_needed'}
    state = read_state(state_dir)
    await same_account(cai, state)
    attempted = state.get('binding_attempted', True)
    if attempted:
        settings = await cai.settings()
        if not original_binding(settings, state):
            mapping = settings.get('personaOverrides', {})
            if state['character_id'] not in mapping or mapping[state['character_id']] != state['test_persona_id']:
                raise Failure('RESTORE_BINDING_CONFLICT')
            await cai.bind(state['character_id'], state['original_value'], remove=not state['original_present'])
        if not original_binding(await cai.settings(), state):
            raise Failure('RESTORE_NOT_VERIFIED')
    report = {'status': 'verified' if attempted else 'not_needed', 'character_id': state['character_id'],
        'test_persona_id': state.get('test_persona_id'), 'restored_at': datetime.now(timezone.utc).isoformat()}
    save_json(Path(state['run_dir']) / 'restore-result.json', report)
    if state.get('schema_version') == 2:
        state['binding_restore'] = report
        save_json(pending, state)
    else:
        pending.unlink()  # Legacy journals authorize binding restoration only.
    return report


def validate_chat(chat, state):
    if (chat.get('chat_id') != state['chat_id'] or chat.get('character_id') != state['character_id']
            or str(chat.get('creator_id')) != state['account_id']):
        raise Failure('CLEANUP_CHAT_OWNER_MISMATCH')
    if not isinstance(chat.get('state'), str):
        raise Failure('CLEANUP_CHAT_SCHEMA_CHANGED')


def validate_persona(persona, state, me):
    if persona.get('external_id') != state['test_persona_id']:
        raise Failure('CLEANUP_PERSONA_ID_MISMATCH')
    owner = persona.get('user__id')
    if owner is not None:
        owned = str(owner) == state['account_id']
    else:
        owned = bool(persona.get('user__username')) and persona['user__username'] == me['username']
    if not owned:
        raise Failure('CLEANUP_PERSONA_OWNER_MISMATCH')


async def archive_persona(cai, state, me, checkpoint):
    object_id = state.get('test_persona_id')
    if not object_id:
        raise Failure('PERSONA_CREATION_UNCONFIRMED')

    def has_receipt():
        receipt = state.get('persona_archive_receipt', {})
        return (receipt.get('id') == object_id and receipt.get('status') == 'OK'
            and receipt.get('archived') is True)

    async def listed():
        personas = await cai.get_personas()
        match = next((p for p in personas if p['external_id'] == object_id), None)
        if match is not None:
            validate_persona(match, state, me)
        return match is not None

    checkpoint('persona_list_before')
    if not await listed():
        # Older journals have no POST receipt. Confirm absence, not how it happened.
        return {'status': 'archived' if has_receipt() else 'absent', 'id': object_id,
            'verification': 'refreshed_persona_list', 'already_absent': True}
    if has_receipt():
        # A stale/inconsistent list must not cause a confirmed mutation to be resent.
        raise Failure('CLEANUP_ARCHIVE_NOT_VERIFIED')
    checkpoint('persona_read_before')
    before = await cai.get_persona(object_id)
    validate_persona(before, state, me)
    checkpoint('persona_archive_submit')
    error = None
    try:
        response = await cai.archive_persona(before)
    except Failure as caught:
        if caught.code in SESSION_ERRORS:
            raise
        error = caught
    else:
        checkpoint('persona_archive_response')
        after = response.get('persona') if isinstance(response, dict) else None
        if (not isinstance(response, dict) or response.get('status') != 'OK'
                or not isinstance(after, dict) or after.get('archived') is not True):
            raise Failure('CLEANUP_PERSONA_ARCHIVE_INVALID_RESPONSE')
        validate_persona(after, state, me)
        if any(after.get(key) != before[key]
                for key in ('definition', 'name', 'participant__name') if key in before):
            raise Failure('CLEANUP_PERSONA_CONTENT_CHANGED')
        state['persona_archive_receipt'] = {'id': object_id, 'status': 'OK', 'archived': True}
        checkpoint('persona_archive_confirmed')  # Persist success before another request.
    checkpoint('persona_list_after')
    if await listed():
        raise error or Failure('CLEANUP_ARCHIVE_NOT_VERIFIED')
    result = {'status': 'archived' if has_receipt() else 'absent', 'id': object_id,
        'verification': 'refreshed_persona_list'}
    if error:
        result['submit_error'] = {'code': error.code, 'details': error.details}
    return result


async def archive_one(cai, state, kind, checkpoint):
    checkpoint('account')
    me = await same_account(cai, state)
    checkpoint('settings')
    settings = await cai.settings()
    if state.get('binding_attempted') and not original_binding(settings, state):
        raise Failure('RESTORE_BINDING_CONFLICT')
    if kind == 'chat':
        object_id = state.get('chat_id')
        if not object_id:
            return {'status': 'not_needed'}
        if object_id in state.get('initial_session_ids', []):
            raise Failure('CLEANUP_CHAT_IN_INITIAL_HISTORY')
        async def read():
            item = await cai.get_chat(object_id)
            validate_chat(item, state)
            return item
        archived = lambda item: item['state'] == 'STATE_ARCHIVED'
    else:
        object_id = state.get('test_persona_id')
        if not object_id:
            raise Failure('PERSONA_CREATION_UNCONFIRMED')
        if (settings.get('default_persona_id') == object_id
                or object_id in settings.get('personaOverrides', {}).values()):
            raise Failure('CLEANUP_PERSONA_STILL_REFERENCED')
        return await archive_persona(cai, state, me, checkpoint)
    checkpoint('chat_read_before')
    try:
        before = await read()
    except Failure as error:
        # A stale recovery journal may refer to a chat the server has already
        # removed. A 404 confirms there is no chat left to archive.
        if kind == 'chat' and error.code == 'CAI_HTTP_404':
            return {'status': 'absent', 'id': object_id, 'verification': 'chat_detail_404'}
        raise
    if archived(before):
        return {'status': 'archived', 'id': object_id, 'already_archived': True}
    error = None
    checkpoint('chat_archive_submit')
    try:
        await cai.archive_chat(object_id)
    except Failure as caught:
        if caught.code in SESSION_ERRORS:
            raise
        error = caught  # A timeout may have happened after the server committed.
    checkpoint('chat_read_after')
    after = await read()  # Chat details remain readable after archival.
    if not archived(after):
        raise error or Failure('CLEANUP_ARCHIVE_NOT_VERIFIED')
    return {'status': 'archived', 'id': object_id}


async def archive_pending(cai, state_dir):
    pending = state_dir / 'pending-restore.json'
    if not pending.exists():
        return {'status': 'not_needed'}
    state = read_state(state_dir)
    if state.get('schema_version') != 2:
        return {'status': 'not_needed'}
    if state.get('binding_restore', {}).get('status') not in ('verified', 'not_needed'):
        raise Failure('RESTORE_REQUIRED_BEFORE_ARCHIVE')
    results = state.setdefault('cleanup', {})
    for kind in ('chat', 'persona'):
        stage = 'start'
        def checkpoint(value):
            nonlocal stage
            stage = value
            state.setdefault('cleanup_stages', {})[kind] = stage
            save_json(pending, state)
        try:
            results[kind] = await archive_one(cai, state, kind, checkpoint)
        except Exception as error:
            results[kind] = {'status': 'failed', 'stage': stage,
                'code': error.code if isinstance(error, Failure) else 'CLEANUP_FAILED'}
            if isinstance(error, Failure) and error.details:
                results[kind]['error_details'] = error.details
            save_json(pending, state)
            if results[kind]['code'] in SESSION_ERRORS or results[kind]['code'] == 'RESTORE_BINDING_CONFLICT':
                break
        save_json(pending, state)
    complete = (results.get('chat', {}).get('status') in ('archived', 'absent', 'not_needed')
        and results.get('persona', {}).get('status') in ('archived', 'absent', 'not_needed'))
    report = {'status': 'verified' if complete else 'cleanup_failed', 'run_id': state['run_id'],
        'chat': results.get('chat', {'status': 'not_run'}), 'persona': results.get('persona', {'status': 'not_run'})}
    if state.get('persona_archive_receipt'):
        report['persona_archive_receipt'] = state['persona_archive_receipt']
    save_json(Path(state['run_dir']) / 'cleanup-result.json', report)
    if complete:
        pending.unlink()
    return report


async def restore(cai, state_dir):
    binding = await restore_binding(cai, state_dir)
    cleanup = await archive_pending(cai, state_dir)
    if cleanup['status'] == 'not_needed':
        return binding
    return {**cleanup, 'restore': binding}


async def finish(operation):
    """One Ctrl+C waits for recovery; a second forced interruption leaves the journal."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError:
        return await task, True
