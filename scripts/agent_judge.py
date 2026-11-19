"""File-based handoff to the current agent; no model client or browser operations."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from common import Failure, save_json
from judge import INSTRUCTIONS, SCHEMA, validate_decision
from conversation import MESSAGE_INSTRUCTIONS, MESSAGE_SCHEMA, validate_message_plan

IDENTITY = ('schema_version', 'kind', 'run_id', 'request_id', 'evidence_sha256')


def validate_reply(value, kind, evidence):
    if kind == 'message':
        return validate_message_plan(value, evidence)
    if kind == 'candidate':
        return validate_decision(value, [line['text'] for line in evidence['candidate_lines']])
    raise Failure('AGENT_JUDGE_INVALID_REQUEST')


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise Failure('AGENT_JUDGE_FILE_UNREADABLE') from None


def evidence_hash(evidence):
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def publish_once(path, value):
    """Publish a complete signal atomically, never replacing a prior submission."""
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise Failure('AGENT_JUDGE_ALREADY_SUBMITTED') from None
    finally:
        temporary.unlink(missing_ok=True)


def active_request(path):
    path = Path(path).resolve()
    if not re.fullmatch(r'request-\d{3}-[0-9a-f]{32}\.json', path.name):
        raise Failure('AGENT_JUDGE_INVALID_REQUEST_PATH')
    request, status = read_json(path), read_json(path.parent / 'status.json')
    if (not isinstance(request, dict) or not isinstance(status, dict)
            or request.get('schema_version') != 2
            or any(key not in request or request[key] != status.get(key) for key in IDENTITY)
            or status.get('status') != 'waiting_for_judgment'
            or status.get('request_file') != str(path)):
        raise Failure('AGENT_JUDGE_REQUEST_NOT_ACTIVE')
    try:
        if evidence_hash(request['evidence']) != request['evidence_sha256']:
            raise Failure('AGENT_JUDGE_EVIDENCE_MISMATCH')
        if datetime.now(timezone.utc) >= datetime.fromisoformat(request['expires_at']):
            raise Failure('AGENT_JUDGE_TIMEOUT')
    except (KeyError, TypeError, ValueError):
        raise Failure('AGENT_JUDGE_INVALID_REQUEST') from None
    return path, request


def submit(request_path, decision):
    path, request = active_request(request_path)
    validate_reply(decision, request['kind'], request['evidence'])
    envelope = {key: request[key] for key in IDENTITY}
    envelope['decision'] = decision
    publish_once(path.with_name(path.name.replace('request-', 'response-', 1)), envelope)
    # Submission is not a workflow success; the runner still validates and consumes it.
    return {'status': 'submitted', 'request_id': request['request_id']}


def cancel(request_path):
    path, request = active_request(request_path)
    publish_once(path.with_name(path.name.replace('request-', 'cancel-', 1)),
        {key: request[key] for key in IDENTITY})
    return {'status': 'cancel_requested', 'request_id': request['request_id']}


class AgentJudge:
    backend = 'agent'

    def __init__(self, directory, max_steps=6, timeout=600, poll_interval=0.25):
        self.directory = Path(directory).resolve()
        self.run_id = uuid.uuid4().hex
        self.calls = 0
        self.candidate_calls = 0
        self.message_calls = 0
        self.usage = {}  # Host-agent usage is unknown to this process, not zero tokens.
        self.limit = max_steps * 3
        self.message_limit = max_steps
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.prepared = False

    def ready(self):
        if not self.prepared:
            self.directory.mkdir(parents=True, exist_ok=False)
            save_json(self.directory / 'status.json', {'status': 'ready', 'run_id': self.run_id})
            self.prepared = True

    async def evaluate(self, candidate, accepted, *, context=None):
        if self.candidate_calls >= self.limit:
            raise Failure('JUDGE_CALL_BUDGET_EXHAUSTED')
        if len(candidate) + len(accepted) > 100000:
            raise Failure('JUDGE_INPUT_TOO_LARGE')
        lines = candidate.splitlines(keepends=True)
        evidence = {'accepted_text': accepted,
            'candidate_lines': [{'line': i + 1, 'text': text} for i, text in enumerate(lines)],
            'context': context or {}}
        self.candidate_calls += 1
        return await self._exchange('candidate', evidence)

    async def plan_message(self, context):
        if self.message_calls >= self.message_limit:
            raise Failure('JUDGE_MESSAGE_BUDGET_EXHAUSTED')
        self.message_calls += 1
        return await self._exchange('message', context)

    async def _exchange(self, kind, evidence):
        if len(json.dumps(evidence, ensure_ascii=False)) > 500000:
            raise Failure('JUDGE_INPUT_TOO_LARGE')
        self.ready()
        self.calls += 1
        identity = {'schema_version': 2, 'kind': kind, 'run_id': self.run_id,
            'request_id': uuid.uuid4().hex, 'evidence_sha256': evidence_hash(evidence)}
        suffix = f"{self.calls:03}-{identity['request_id']}.json"
        request_path = self.directory / ('request-' + suffix)
        response_path = self.directory / ('response-' + suffix)
        cancel_path = self.directory / ('cancel-' + suffix)
        request = {**identity,
            'instructions': MESSAGE_INSTRUCTIONS if kind == 'message' else INSTRUCTIONS,
            'decision_schema': MESSAGE_SCHEMA if kind == 'message' else SCHEMA,
            'evidence': evidence,
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=self.timeout)).isoformat()}
        status = {**identity, 'status': 'waiting_for_judgment',
            'request_file': str(request_path), 'expires_at': request['expires_at']}
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            save_json(request_path, request)
            save_json(self.directory / 'status.json', status)
            print(json.dumps(status, ensure_ascii=False), flush=True)
            while True:
                if cancel_path.exists():
                    signal = read_json(cancel_path)
                    if signal != identity:
                        raise Failure('AGENT_JUDGE_RESPONSE_MISMATCH')
                    raise asyncio.CancelledError()
                if asyncio.get_running_loop().time() >= deadline:
                    raise Failure('AGENT_JUDGE_TIMEOUT')
                if response_path.exists():
                    response = read_json(response_path)
                    if (not isinstance(response, dict) or set(response) != {*IDENTITY, 'decision'}
                            or any(response[key] != identity[key] for key in IDENTITY)):
                        raise Failure('AGENT_JUDGE_RESPONSE_MISMATCH')
                    decision = validate_reply(response['decision'], kind, evidence)
                    save_json(self.directory / 'status.json', {**status, 'status': 'resolved'})
                    return decision
                await asyncio.sleep(self.poll_interval)
        except BaseException as error:
            final_status = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
            code = error.code if isinstance(error, Failure) else 'USER_CANCELLED' if final_status == 'cancelled' else 'AGENT_JUDGE_FAILED'
            try:
                save_json(self.directory / 'status.json', {**status, 'status': final_status, 'code': code})
            except OSError:
                pass  # Never prevent the caller's account restoration.
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', help='Read the handoff status, not the final workflow result')
    status.add_argument('--directory', required=True)
    for name in ('submit', 'cancel'):
        command = commands.add_parser(name)
        command.add_argument('--request', required=True, help='Exact request file emitted by the runner')
        if name == 'submit':
            command.add_argument('--decision', required=True, help='UTF-8 JSON file matching this request\'s decision_schema')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            result = read_json(Path(args.directory) / 'status.json')
        elif args.command == 'submit':
            result = submit(args.request, read_json(args.decision))
        else:
            result = cancel(args.request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Failure, OSError) as error:
        print(json.dumps({'status': 'failed', 'code': error.code if isinstance(error, Failure) else 'AGENT_JUDGE_IO_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
