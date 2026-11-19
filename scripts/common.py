"""Small local primitives; credentials never enter errors or output files."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request


class Failure(Exception):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


def save_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def request_json(url, *, headers=None, body=None, timeout=90, service='HTTP', method=None, allow_empty=False):
    def execute():
        request = urllib.request.Request(url, headers={'Content-Type': 'application/json',
            'Accept': 'application/json', **(headers or {})},
            data=None if body is None else json.dumps(body).encode('utf-8'), method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise Failure(service + '_RESPONSE_TOO_LARGE')
                if not data and allow_empty:
                    return None
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError):
                    raise Failure(service + '_INVALID_JSON') from None
        except urllib.error.HTTPError as error:
            # Classify the response without logging bodies, cookies or arbitrary headers.
            mime = error.headers.get('Content-Type', '').split(';')[0].lower()
            details = {'http_status': error.code,
                'response_type': 'json' if mime == 'application/json' else 'html' if mime == 'text/html' else 'other',
                'cloudflare_challenge': error.headers.get('cf-mitigated') == 'challenge'}
            error.close()
            raise Failure(f'{service}_HTTP_{error.code}', details) from None
        except urllib.error.URLError as error:
            kind = 'DNS_FAILED' if isinstance(error.reason, socket.gaierror) else 'NETWORK_FAILED'
            raise Failure(service + '_' + kind) from None
        except (TimeoutError, OSError):
            raise Failure(service + '_NETWORK_OR_TIMEOUT') from None
    # Do not race restoration against a cancelled thread that may still be posting a mutation.
    task = asyncio.create_task(asyncio.to_thread(execute))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Failure('ANOTHER_RUN_IS_ACTIVE') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
