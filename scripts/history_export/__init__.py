"""Export verified pre-existing history on the caller's logged-in Chrome page."""
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import uuid
import zipfile

from common import Failure
from .transfer import save_zip
from .verify_export import verify

HERE = Path(__file__).resolve().parent


async def export_history(page, target, account_id, timeout):
    binding = '__cai_history_progress_' + uuid.uuid4().hex

    def progress(value):
        phase = value.get('phase') if isinstance(value, dict) else None
        if phase in {'account', 'enumerate', 'snapshot_ready', 'character', 'session', 'done'}:
            print('History phase: ' + phase, flush=True)

    await page.expose_function(binding, progress)
    expression = ('async (options) => { const module = {exports: {}};\n'
        + (HERE / 'exporter.js').read_text(encoding='utf-8') + '\n'
        + (HERE / 'bridge.js').read_text(encoding='utf-8') + '\n}')
    handle = await page.evaluate_handle(expression, {'characterId': target, 'accountId': account_id,
        'progress': binding, 'timeoutMs': timeout * 1000})
    try:
        summary = await handle.evaluate('(r) => r.error ? {error:r.error} : r.summary')
        if summary.get('error'):
            raise Failure(summary['error'])
        if summary['account_id'] != account_id:
            raise Failure('HISTORY_ACCOUNT_MISMATCH')
        with tempfile.TemporaryDirectory(prefix='cai-combined-history-') as directory:
            scratch = Path(directory).resolve()
            if not scratch.is_relative_to(Path(tempfile.gettempdir()).resolve()):
                raise Failure('UNEXPECTED_TEMP_DIRECTORY')
            path = await save_zip(handle, scratch, summary['zip_bytes'], summary['zip_sha256'])
            try:
                report = verify(path)
            except Exception:
                raise Failure('EXPORT_VERIFICATION_FAILED') from None
            if report['scope_type'] != 'character' or report['target_character_id'] != target or len(report['characters']) != 1:
                raise Failure('HISTORY_CHARACTER_MISMATCH')
            character = report['characters'][0]
            with zipfile.ZipFile(path) as archive:
                data = archive.read(character['file'])
            requests = json.loads(data)
            if any(item['user_id'] != 'character-ai:' + account_id for item in requests):
                raise Failure('HISTORY_ACCOUNT_MISMATCH')
        result = {key: summary[key] for key in ('account_id', 'export_status', 'totals', 'issues', 'incomplete_sessions')}
        result.update(character_id=target, character_name=character['name'], verification=report['status'],
            status='complete' if report['status'] == 'passed' else 'partial',
            session_snapshot=report.get('session_snapshot'))
        # An entirely unreadable history must not masquerade as confirmed empty history.
        return result, data if requests or result['status'] == 'complete' else None
    finally:
        with suppress(Exception):
            await handle.dispose()
