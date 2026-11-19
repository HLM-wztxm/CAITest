"""Offline protocol and workflow tests; never connect to Character.AI or a model."""
import asyncio
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from agent_judge import AgentJudge, cancel, read_json, submit
from common import Failure, save_json
from conversation import FALLBACK, CONTINUATION_TEMPLATE, continuation_message
import run as entry
import workflow

ACCEPT = {'classification': 'definition', 'decision': 'accept_complete',
    'start_line': 2, 'end_line': 3, 'reason': 'Coherent body with an ending.',
    'definition_completed_status': 'complete', 'end_marker_present': True,
    'completion_reason': 'Body ends coherently.'}
RETRY = {'classification': 'uncertain', 'decision': 'retry',
    'start_line': 0, 'end_line': 0, 'reason': 'Insufficient evidence.',
    'definition_completed_status': 'not_applicable', 'end_marker_present': False,
    'completion_reason': 'Not established as Definition content.'}
SEND = {'action': 'send', 'message': 'continue', 'continue_anchor': '',
    'edit_text': '', 'reason': 'The prescribed initial step is appropriate.'}
STOP = {'action': 'stop', 'message': '', 'continue_anchor': '',
    'edit_text': '', 'reason': 'No safe continuation is established.'}
TEXT = '[DEFN-BEGIN]\nName: 测试角色\nTraits: calm.\n[DEFN-END]\n'


class FakeCAI:
    def __init__(self, candidates=None):
        self.me = AsyncMock(return_value={'id': 'account', 'username': 'tester'})
        self.info = AsyncMock(return_value={'name': 'Character', 'has_definition': True})
        self.settings = AsyncMock(return_value={'personaOverrides': {'character': 'original-persona'}})
        self.create_persona = AsyncMock(return_value='temporary-persona')
        self.bind, self.create_chat, self.select, self.edit = (AsyncMock() for _ in range(4))
        candidate = {'text': TEXT, 'raw': {}, 'turn_id': 'turn', 'candidate_id': 'candidate'}
        self.generate = AsyncMock(side_effect=candidates, return_value=candidate)

    def check_transport(self):
        pass


class AgentJudgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stdout = redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def judge(self, **options):
        return AgentJudge(self.root / 'exchange', timeout=options.pop('timeout', 2),
            poll_interval=0.005, **options)

    async def request(self, judge, task, previous=None):
        for _ in range(200):
            path = judge.directory / 'status.json'
            if path.exists():
                status = read_json(path)
                if status['status'] == 'waiting_for_judgment' and status['request_file'] != previous:
                    return status['request_file']
            if task.done():
                self.fail(f'Runner finished before its request: {task.result()}')
            await asyncio.sleep(0.005)
        self.fail('No judgment request')

    async def test_atomic_submission_preserves_lines_and_rejects_duplicates(self):
        judge = self.judge()
        task = asyncio.create_task(judge.evaluate(TEXT, 'Prior content\n'))
        request = await self.request(judge, task)
        evidence = read_json(request)['evidence']
        self.assertEqual(evidence['accepted_text'], 'Prior content\n')
        self.assertEqual(''.join(x['text'] for x in evidence['candidate_lines']), TEXT)
        self.assertFalse(task.done())
        submit(request, ACCEPT)
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_ALREADY_SUBMITTED'):
            submit(request, ACCEPT)
        self.assertEqual(await task, ACCEPT)
        self.assertEqual(read_json(judge.directory / 'status.json')['status'], 'resolved')
        self.assertEqual(judge.usage, {})

    async def test_invalid_decision_can_be_corrected_without_consumption(self):
        judge = self.judge()
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        request = await self.request(judge, task)
        for invalid in ({**ACCEPT, 'end_line': 99}, {**ACCEPT, 'start_line': True},
                        {**RETRY, 'start_line': 1}, {**ACCEPT, 'text': 'rewritten'},
                        {**ACCEPT, 'definition_completed_status': 'incomplete'}):
            with self.assertRaises(Failure):
                submit(request, invalid)
        self.assertEqual(list(judge.directory.glob('response-*.json')), [])
        submit(request, ACCEPT)
        self.assertEqual(await task, ACCEPT)

    async def test_old_request_cannot_decide_next_candidate(self):
        judge = self.judge()
        first = asyncio.create_task(judge.evaluate(TEXT, ''))
        old_request = await self.request(judge, first)
        submit(old_request, RETRY)
        await first
        second = asyncio.create_task(judge.evaluate(TEXT, 'new accepted context'))
        new_request = await self.request(judge, second, old_request)
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_REQUEST_NOT_ACTIVE'):
            submit(old_request, ACCEPT)
        self.assertFalse(second.done())
        submit(new_request, ACCEPT)
        self.assertEqual(await second, ACCEPT)

    async def test_runner_rejects_mismatched_envelope(self):
        judge = self.judge()
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        request = await self.request(judge, task)
        submit(request, ACCEPT)
        response_path = next(judge.directory.glob('response-*.json'))
        response = read_json(response_path)
        response['evidence_sha256'] = 'stale candidate'
        save_json(response_path, response)
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_RESPONSE_MISMATCH'):
            await task

    async def test_modified_evidence_cannot_be_submitted(self):
        judge = self.judge()
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        request_path = await self.request(judge, task)
        original = read_json(request_path)
        altered = {**original, 'evidence': {**original['evidence'], 'accepted_text': 'tampered'}}
        save_json(request_path, altered)
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_EVIDENCE_MISMATCH'):
            submit(request_path, ACCEPT)
        save_json(request_path, original)
        cancel(request_path)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_timeout_and_task_cancel_close_pending_request(self):
        judge = self.judge(timeout=0.03)
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        request = await self.request(judge, task)
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_TIMEOUT'):
            await task
        with self.assertRaisesRegex(Failure, 'AGENT_JUDGE_REQUEST_NOT_ACTIVE'):
            submit(request, ACCEPT)
        self.assertEqual(read_json(judge.directory / 'status.json')['code'], 'AGENT_JUDGE_TIMEOUT')
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        await self.request(judge, task, request)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(read_json(judge.directory / 'status.json')['status'], 'cancelled')

    async def test_decision_budget_is_bounded(self):
        judge = self.judge(max_steps=0)
        with self.assertRaisesRegex(Failure, 'JUDGE_CALL_BUDGET_EXHAUSTED'):
            await judge.evaluate(TEXT, '')
        self.assertFalse(judge.directory.exists())

    async def test_cli_submits_utf8_bom_decision(self):
        judge = self.judge(timeout=10)
        task = asyncio.create_task(judge.evaluate(TEXT, ''))
        request = await self.request(judge, task)
        decision_path = self.root / 'decision.json'
        decision_path.write_text(json.dumps(ACCEPT, ensure_ascii=False), encoding='utf-8-sig')
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(SCRIPTS / 'agent_judge.py'), 'submit', '--request', request,
            '--decision', str(decision_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)['status'], 'submitted')
        self.assertEqual(await task, ACCEPT)

    async def test_workflow_waits_then_extracts_original_text_and_cleans_up(self):
        await self.check_workflow('accept')

    async def test_workflow_timeout_restores_and_never_selects(self):
        await self.check_workflow('timeout')

    async def test_workflow_cancel_restores_and_never_selects(self):
        await self.check_workflow('cancel')

    async def check_workflow(self, outcome):
        judge = self.judge(timeout=0.1 if outcome == 'timeout' else 2)
        cai = FakeCAI()
        destination = self.root / 'definition'
        with patch.object(workflow, 'restore_binding', AsyncMock(return_value={'status': 'verified'})) as restore, \
                patch.object(workflow, 'archive_pending', AsyncMock(return_value={'status': 'verified'})) as archive:
            task = asyncio.create_task(workflow.run(cai, judge, 'character', destination, self.root / 'state'))
            request = await self.request(judge, task)
            self.assertEqual(read_json(request)['kind'], 'message')
            cai.create_persona.assert_not_awaited()
            submit(request, SEND)
            request = await self.request(judge, task, request)
            self.assertEqual(read_json(request)['kind'], 'candidate')
            cai.select.assert_not_awaited()
            restore.assert_not_awaited()
            archive.assert_not_awaited()
            if outcome == 'accept':
                submit(request, ACCEPT)
            elif outcome == 'cancel':
                cancel(request)
            result = await task
            restore.assert_awaited_once()
            archive.assert_awaited_once()
        if outcome == 'accept':
            self.assertEqual(result['status'], 'candidate_complete')
            self.assertEqual(result['verification'], 'unverified')
            self.assertEqual((destination / 'definition-candidate.txt').read_bytes(),
                'Name: 测试角色\nTraits: calm.\n'.encode('utf-8'))
            cai.select.assert_awaited_once()
        else:
            self.assertEqual(result['status'], 'cancelled' if outcome == 'cancel' else 'failed')
            if outcome == 'timeout':
                self.assertEqual(result['code'], 'AGENT_JUDGE_TIMEOUT')
            cai.select.assert_not_awaited()
        self.assertEqual(result['judge_backend'], 'agent')

    def start_workflow(self, *, candidates=None, max_steps=6):
        judge, cai = self.judge(max_steps=max_steps), FakeCAI(candidates)
        destination = self.root / 'definition'
        for name in ('restore_binding', 'archive_pending'):
            patcher = patch.object(workflow, name, AsyncMock(return_value={'status': 'verified'}))
            patcher.start()
            self.addCleanup(patcher.stop)
        task = asyncio.create_task(workflow.run(cai, judge, 'character', destination,
            self.root / 'state', max_steps=max_steps))
        return judge, cai, destination, task

    async def initial_message(self, judge, task):
        request = await self.request(judge, task)
        self.assertEqual(read_json(request)['evidence']['phase'], 'initial')
        submit(request, SEND)
        return await self.request(judge, task, request)

    async def test_initial_stop_prevents_account_mutation(self):
        judge, cai, _, task = self.start_workflow()
        request = await self.request(judge, task)
        submit(request, STOP)
        result = await task
        self.assertEqual(result['reason'], 'agent_stopped_before_initial_message')
        cai.create_persona.assert_not_awaited()
        cai.bind.assert_not_awaited()
        cai.generate.assert_not_awaited()

    async def test_direct_export_skips_agent_and_conversation(self):
        judge, cai = self.judge(), FakeCAI()
        result = await workflow.run(cai, judge, 'character', self.root / 'definition',
            self.root / 'state', direct_only=True)
        self.assertEqual(result['status'], 'not_readable')
        self.assertEqual(judge.calls, 0)
        self.assertFalse(judge.directory.exists())
        cai.create_persona.assert_not_awaited()
        cai.generate.assert_not_awaited()

    async def test_original_binding_is_read_after_initial_agent_wait(self):
        judge, cai, _, task = self.start_workflow()
        request = await self.request(judge, task)
        cai.settings.assert_not_awaited()
        cai.settings.return_value = {'personaOverrides': {'character': 'new-user-choice'}}
        submit(request, SEND)
        request = await self.request(judge, task, request)
        state = read_json(self.root / 'state' / 'pending-restore.json')
        self.assertEqual(state['original_value'], 'new-user-choice')
        submit(request, ACCEPT)
        await task

    async def test_partial_accept_retains_body_without_another_message(self):
        judge, cai, destination, task = self.start_workflow()
        request = await self.initial_message(judge, task)
        submit(request, {**ACCEPT, 'decision': 'accept_partial',
            'definition_completed_status': 'uncertain', 'end_marker_present': False,
            'completion_reason': 'No localized truncation despite an uncertain ending.'})
        result = await task
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['definition_completed_status'], 'uncertain')
        self.assertFalse(result['end_marker_present'])
        self.assertTrue((destination / 'definition-candidate.txt').is_file())
        cai.generate.assert_awaited_once()
        self.assertEqual(judge.message_calls, 1)

    async def test_markerless_ending_waits_for_three_variants_before_accepting(self):
        await self.check_markerless_comparison(confirmed=True)

    async def test_inconclusive_ending_comparison_retains_original_as_partial(self):
        await self.check_markerless_comparison(confirmed=False)

    async def check_markerless_comparison(self, *, confirmed):
        texts = [TEXT.replace('[DEFN-END]\n', ''), 'Alternative ending A.',
            'Alternative ending B, possibly more content.']
        candidates = [{'text': text, 'raw': {}, 'turn_id': 'same-turn',
            'candidate_id': str(i)} for i, text in enumerate(texts)]
        judge, cai, destination, task = self.start_workflow(candidates=candidates)
        request = await self.initial_message(judge, task)
        submit(request, {**ACCEPT, 'end_marker_present': False})
        request = await self.request(judge, task, request)
        evidence = read_json(request)['evidence']
        self.assertEqual(cai.generate.await_count, 3)
        self.assertEqual(evidence['accepted_text'], '')
        self.assertEqual(''.join(x['text'] for x in evidence['candidate_lines']), texts[0])
        self.assertEqual(evidence['context']['candidate_attempt'], 1)
        self.assertEqual([x['text'] for x in evidence['context']['comparison_candidates']], texts)
        self.assertEqual(read_json(destination / 'run-result.json')['steps'][0]
            ['ending_comparison']['status'], 'pending')
        self.assertFalse((destination / 'definition-candidate.txt').exists())
        cai.select.assert_not_awaited()
        decision = {**ACCEPT, 'end_marker_present': False,
            'completion_reason': 'Compared all three endings and their structure.'}
        if not confirmed:
            decision.update(decision='accept_partial', definition_completed_status='uncertain')
        submit(request, decision)
        result = await task
        self.assertEqual(result['status'], 'candidate_complete' if confirmed else 'partial')
        self.assertEqual(result['steps'][0]['ending_comparison']['status'],
            'complete' if confirmed else 'inconclusive')
        self.assertEqual(result['accepted'][0]['attempt'], 1)
        self.assertEqual((destination / 'definition-candidate.txt').read_text(encoding='utf-8'),
            'Name: 测试角色\nTraits: calm.\n')
        cai.select.assert_awaited_once_with(result['chat_id'], candidates[0])
        for i in (1, 2):
            self.assertEqual(cai.generate.await_args_list[i].args[3], candidates[i - 1])
        self.assertEqual(judge.message_calls, 1)
        self.assertEqual(judge.candidate_calls, 2)

    async def test_continuation_uses_agent_anchor_and_exact_message(self):
        fragment = 'She holds a map of the old city beside the\n'
        candidates = [
            {'text': '[DEFN-BEGIN]\n' + fragment, 'raw': {}, 'turn_id': 'one', 'candidate_id': 'one'},
            {'text': 'gate. She is patient.\n[DEFN-END]\n', 'raw': {}, 'turn_id': 'two', 'candidate_id': 'two'}]
        judge, cai, _, task = self.start_workflow(candidates=candidates)
        request = await self.initial_message(judge, task)
        context = read_json(request)['evidence']['context']
        self.assertEqual(context['user_message'], 'continue')
        self.assertEqual(context['candidates_remaining'], 2)
        submit(request, {**ACCEPT, 'decision': 'accept_continue', 'end_line': 2,
            'definition_completed_status': 'incomplete', 'end_marker_present': False,
            'completion_reason': 'The sentence ends at a clear truncated preposition.'})
        request = await self.request(judge, task, request)
        evidence = read_json(request)['evidence']
        self.assertEqual(evidence['phase'], 'continuation')
        self.assertEqual(evidence['latest_fragment'], fragment)
        cai.generate.assert_awaited_once()  # No next send before the message plan.
        anchor = 'map of the old city beside the'  # Seven words: not the old code's last-six choice.
        message = continuation_message(anchor)
        submit(request, {**SEND, 'message': message, 'continue_anchor': anchor})
        request = await self.request(judge, task, request)
        self.assertEqual(cai.generate.await_args_list[1].args[2], message)
        self.assertEqual(read_json(request)['evidence']['context']['user_message'], message)
        submit(request, {**ACCEPT, 'start_line': 1, 'end_line': 1})
        result = await task
        self.assertEqual(result['status'], 'candidate_complete')
        self.assertEqual(result['message_plans'][1]['continue_anchor'], anchor)
        self.assertEqual(result['judge_message_calls'], 2)
        self.assertEqual(result['judge_candidate_calls'], 2)

    async def test_fallback_waits_for_agent_then_edits_only_current_ai_reply(self):
        judge, cai, _, task = self.start_workflow()
        request = await self.initial_message(judge, task)
        for _ in range(3):
            submit(request, {**RETRY, 'classification': 'role_play'})
            request = await self.request(judge, task, request)
        evidence = read_json(request)['evidence']
        self.assertEqual(evidence['phase'], 'fallback')
        self.assertEqual(evidence['classifications'], ['role_play'] * 3)
        cai.edit.assert_not_awaited()
        self.assertEqual(cai.generate.await_count, 3)
        submit(request, {**SEND, 'edit_text': FALLBACK})
        request = await self.request(judge, task, request)
        cai.edit.assert_awaited_once()
        self.assertEqual(cai.edit.await_args.args[2], FALLBACK)
        self.assertEqual(cai.generate.await_count, 4)
        submit(request, ACCEPT)
        result = await task
        self.assertEqual(result['steps'][0]['manual_edit']['source'], 'manual_edit')
        self.assertEqual(result['steps'][1]['user_message'], 'continue')

    async def test_step_limit_does_not_request_or_send_extra_continuation(self):
        judge, cai, _, task = self.start_workflow(max_steps=1)
        request = await self.initial_message(judge, task)
        submit(request, {**ACCEPT, 'decision': 'accept_continue',
            'definition_completed_status': 'incomplete'})
        result = await task
        self.assertEqual(result['reason'], 'step_budget_exhausted')
        self.assertEqual(judge.message_calls, 1)
        cai.generate.assert_awaited_once()

    async def test_rejected_continuations_never_enter_edit_fallback(self):
        judge, cai, _, task = self.start_workflow(max_steps=2)
        request = await self.initial_message(judge, task)
        submit(request, {**ACCEPT, 'decision': 'accept_continue',
            'definition_completed_status': 'incomplete'})
        request = await self.request(judge, task, request)
        anchor = 'Traits: calm.'
        submit(request, {**SEND, 'continue_anchor': anchor, 'message': continuation_message(anchor)})
        for _ in range(3):
            request = await self.request(judge, task, request)
            submit(request, {**RETRY, 'classification': 'role_play'})
        result = await task
        self.assertEqual(result['reason'], 'candidate_budget_exhausted')
        self.assertEqual(cai.generate.await_count, 4)
        cai.edit.assert_not_awaited()
        self.assertEqual(judge.message_calls, 2)

    async def test_message_validation_rejects_wrong_anchor_and_template(self):
        judge = self.judge()
        context = {'phase': 'continuation', 'accepted_text': TEXT, 'latest_fragment': 'actual final words',
            'classifications': [], 'continuation_template': CONTINUATION_TEMPLATE, 'fallback_edit': FALLBACK}
        task = asyncio.create_task(judge.plan_message(context))
        request = await self.request(judge, task)
        with self.assertRaisesRegex(Failure, 'JUDGE_INVALID_CONTINUATION_MESSAGE'):
            submit(request, {**SEND, 'continue_anchor': 'invented', 'message': continuation_message('invented')})
        with self.assertRaisesRegex(Failure, 'JUDGE_INVALID_CONTINUATION_MESSAGE'):
            submit(request, {**SEND, 'continue_anchor': 'final words', 'message': 'unbounded custom action'})
        with self.assertRaises(Failure):
            submit(request, ACCEPT)  # Candidate decision cannot authorize a message.
        submit(request, STOP)
        self.assertEqual(await task, STOP)


class EntryTests(unittest.TestCase):
    def test_removed_options_fail_before_starting_workflow(self):
        for flags in (['--judge', 'api'], ['--judge', 'agent'], ['--env-file', 'old.env']):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()) as errors, \
                    patch.object(entry, 'execute', AsyncMock()) as execute:
                with self.assertRaises(SystemExit) as caught:
                    entry.main(['character', *flags])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn('unrecognized arguments', errors.getvalue())
                execute.assert_not_called()

    def test_default_and_export_entry_use_agent_judge(self):
        @asynccontextmanager
        async def session(*args):
            yield object()

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch.object(entry, 'open_cai_session', session), \
                patch.object(entry, 'run_combined', AsyncMock(return_value={'status': 'complete'})) as combined, \
                patch.dict('os.environ', {'CHARACTER_AI_TOKEN': ''}):
            for flags in ([], ['--export-only'], ['--direct-only']):
                self.assertEqual(entry.main(['character', '--state-dir', directory,
                    '--out', str(Path(directory) / 'output'), *flags]), 0)
                self.assertIsInstance(combined.call_args.args[1], AgentJudge)
                self.assertEqual(combined.call_args.kwargs['direct_only'], bool(flags))

    def test_default_output_stays_at_skill_root(self):
        @asynccontextmanager
        async def session(*args):
            yield object()

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch.object(entry, 'open_cai_session', session), \
                patch.object(entry, 'run_combined', AsyncMock(return_value={'status': 'complete'})) as combined, \
                patch.dict('os.environ', {'CHARACTER_AI_TOKEN': ''}):
            self.assertEqual(entry.main(['character', '--state-dir', directory]), 0)
            self.assertEqual(combined.call_args.args[3].parent, SKILL_ROOT / 'output' / 'character')


if __name__ == '__main__':
    unittest.main()
