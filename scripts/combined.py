"""Sequential, shared-session history export and Definition workflow."""
import asyncio
from pathlib import Path

from common import Failure, save_json
from delivery import build_persona, package_delivery
from history_export import export_history
from workflow import run
from cleanup import archive_pending, finish

FATAL_SESSION = {'HISTORY_ACCOUNT_MISMATCH', 'HISTORY_CHARACTER_MISMATCH', 'ACCOUNT_CHANGED',
    'ACCOUNT_HTTP_401', 'ACCOUNT_HTTP_403', 'CAI_HTTP_401', 'CAI_HTTP_403', 'LOGIN_REQUIRED',
    'WRONG_PAGE_ORIGIN', 'PAGE_BOOTSTRAP_NOT_FOUND', 'CAI_BROWSER_SESSION_CLOSED_OR_CHANGED'}
DEFINITION_OK = {'complete', 'candidate_complete', 'no_definition'}


def error_result(error):
    result = {'status': 'failed', 'code': error.code if isinstance(error, Failure) else 'UNEXPECTED_FAILURE'}
    if isinstance(error, Failure) and error.details:
        result['error_details'] = error.details
    return result


async def check_session(cai, account_id):
    page = cai.transport.page
    same = await page.evaluate('''token => {
      if (location.origin !== 'https://character.ai') return false;
      try { return JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps.token === token; }
      catch { return false; }
    }''', cai.token)
    if not same or (await cai.me())['id'] != account_id:
        raise Failure('ACCOUNT_CHANGED')


async def run_combined(cai, judge, target, destination, state_dir, config, *, direct_only=False, gold=None):
    destination.mkdir(parents=True, exist_ok=False)
    definition_dir = destination / 'definition'
    report = {'schema_version': 1, 'character_id': target, 'status': 'running', 'stage': 'account',
        'history': {'status': 'not_run'}, 'definition': {'status': 'not_run'}, 'output': str(destination)}
    data = None
    cancelled = False

    def checkpoint(stage):
        report['stage'] = stage
        save_json(destination / 'workflow_run.json', report)
        print('Combined phase: ' + stage, flush=True)

    try:
        checkpoint('account')
        account = await cai.me()
        report['account_id'] = account['id']
        checkpoint('history')
        try:
            report['history'], data = await export_history(cai.transport.page, target, account['id'], config['request_timeout'])
            if report['history'].get('account_id') != account['id']:
                data = None
                raise Failure('HISTORY_ACCOUNT_MISMATCH')
            if report['history'].get('character_id') != target:
                data = None
                raise Failure('HISTORY_CHARACTER_MISMATCH')
        except asyncio.CancelledError:
            report['history'] = {'status': 'cancelled', 'code': 'USER_CANCELLED'}
            raise
        except Exception as error:
            report['history'] = error_result(error)
            if report['history']['code'] in FATAL_SESSION:
                raise
        checkpoint('definition')
        await check_session(cai, account['id'])
        report['definition'] = await run(cai, judge, target, definition_dir, state_dir, config['max_steps'], direct_only,
            expected_account_id=account['id'], defer_cleanup=True,
            initial_session_ids=(report['history'].get('session_snapshot') or {}).get('session_ids', []))
        if report['definition'].get('account_id') != account['id'] or report['definition'].get('character_id') != target:
            raise Failure('ACCOUNT_CHANGED')
        cancelled = report['definition']['status'] == 'cancelled'
        if gold and report['definition'].get('definition_file'):
            record = report['definition']
            record['gold_exact_match'] = Path(gold).read_bytes() == (definition_dir / record['definition_file']).read_bytes()
            if record['gold_exact_match']:
                record['verification'] = 'verified_exact'
            save_json(definition_dir / 'run-result.json', record)
    except asyncio.CancelledError:
        cancelled = True
        report['code'] = 'USER_CANCELLED'
    except Exception as error:
        report.update({key: value for key, value in error_result(error).items() if key != 'status'})
        if report['definition']['status'] == 'not_run':
            report['definition'] = {'status': 'not_run', 'code': report['code']}
    complete = report['history']['status'] == 'complete' and report['definition']['status'] in DEFINITION_OK and not report.get('code')
    restore_failed = report['definition']['status'] == 'restore_failed'
    try:
        checkpoint('delivery')
        persona = build_persona(target, definition_dir, report['definition'], report['history'])
        report['delivery'] = package_delivery(destination, persona, report['history'], data, complete=complete and not cancelled and not restore_failed)
        report['status'] = 'complete' if complete else 'partial' if report['delivery'] else 'failed'
    except Exception as error:
        report.update(error_result(error))
    if restore_failed:
        report['cleanup'] = {'status': 'not_run', 'code': 'RESTORE_REQUIRED_BEFORE_ARCHIVE'}
    else:
        try:
            print('Combined phase: archive', flush=True)
            report['cleanup'], interrupted = await finish(archive_pending(cai, state_dir))
            cancelled = cancelled or interrupted
        except Exception as error:
            report['cleanup'] = {**error_result(error), 'status': 'cleanup_failed'}
    if cancelled:
        report['status'] = 'cancelled'
    if restore_failed:
        report['status'] = 'restore_failed'
    elif report['cleanup']['status'] == 'cleanup_failed':
        report['status'] = 'cleanup_failed'
    checkpoint('finished')
    return report
