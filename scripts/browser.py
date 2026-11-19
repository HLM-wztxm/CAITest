"""Fixed HTTP/WebSocket operations in the user's logged-in Chrome session."""
import asyncio
import re
import uuid
from urllib.parse import urlsplit
from common import Failure


HTTP = """async arg => {
  if (location.origin !== 'https://character.ai') return {error: 'CAI_BROWSER_SESSION_CLOSED_OR_CHANGED'};
  if (arg.enforceSession) {
    try {
      if (JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps.token !== arg.token)
        return {error: 'ACCOUNT_CHANGED'};
    } catch { return {error: 'ACCOUNT_CHANGED'}; }
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), arg.timeout);
  try {
    const headers = {Accept: 'application/json', Authorization: 'Token ' + arg.token};
    if (arg.body !== null) headers['Content-Type'] = 'application/json';
    const r = await fetch(arg.url, {method: arg.method, headers,
      body: arg.body === null ? undefined : JSON.stringify(arg.body), credentials: 'omit',
      redirect: 'error', signal: controller.signal});
    const mime = (r.headers.get('content-type') || '').split(';')[0].toLowerCase();
    if (!r.ok) return {error: 'CAI_HTTP_' + r.status, details: {http_status: r.status,
      response_type: mime === 'application/json' ? 'json' : mime === 'text/html' ? 'html' : 'other',
      cloudflare_challenge: r.headers.get('cf-mitigated') === 'challenge'}};
    const reader = r.body ? r.body.getReader() : null;
    const chunks = []; let length = 0;
    while (reader) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 4 * 1024 * 1024) { await reader.cancel(); return {error:'CAI_RESPONSE_TOO_LARGE'}; }
      chunks.push(value);
    }
    if (length === 0 && arg.allowEmpty) return {data: null};
    const bytes = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    try { return {data: JSON.parse(new TextDecoder().decode(bytes))}; }
    catch { return {error: 'CAI_INVALID_JSON'}; }
  } catch (e) {
    return {error: e.name === 'AbortError' ? 'CAI_BROWSER_TIMEOUT_UNCERTAIN' : 'CAI_BROWSER_NETWORK_OR_CORS'};
  } finally { clearTimeout(timer); }
}"""

OPEN = """arg => new Promise((resolve, reject) => {
  if (location.origin !== 'https://character.ai') { reject(new Error('WRONG_ORIGIN')); return; }
  if (arg.token !== null) {
    try {
      if (JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps.token !== arg.token) {
        resolve(false); return;
      }
    } catch { resolve(false); return; }
  }
  const socket = new WebSocket('wss://neo.character.ai/ws/');
  const state = {socket, queue: [], pending: null, failed: false};
  window[arg.key] = state;
  const timer = setTimeout(() => { socket.close(); reject(new Error('WS_OPEN_TIMEOUT')); }, arg.timeout);
  const fail = () => {
    state.failed = true; clearTimeout(timer);
    if (state.pending) { state.pending.reject(new Error('WS_CLOSED')); state.pending = null; }
    reject(new Error('WS_CLOSED'));
  };
  socket.onopen = () => { clearTimeout(timer); resolve(true); };
  socket.onerror = fail;
  socket.onclose = fail;
  socket.onmessage = event => {
    if (typeof event.data !== 'string' || new TextEncoder().encode(event.data).length > 2 * 1024 * 1024) {
      fail(); socket.close(); return;
    }
    if (state.pending) { const pending = state.pending; state.pending = null; pending.resolve(event.data); }
    else if (state.queue.length < 2000) state.queue.push(event.data);
    else { fail(); socket.close(); }
  };
})"""

RECEIVE = """key => {
  const state = window[key];
  if (!state) throw new Error('WS_MISSING');
  if (state.queue.length) return state.queue.shift();
  if (state.failed) throw new Error('WS_CLOSED');
  return new Promise((resolve, reject) => { state.pending = {resolve, reject}; });
}"""


class BrowserSocket:
    def __init__(self, page, timeout, token=None):
        self.page = page
        self.timeout = timeout
        self.token = token
        self.key = '__cai_runner_' + uuid.uuid4().hex

    async def __aenter__(self):
        try:
            opened = await self.page.evaluate(OPEN, {'key': self.key, 'timeout': min(30, self.timeout) * 1000, 'token': self.token})
            if opened is False:
                raise Failure('ACCOUNT_CHANGED')
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        try:
            await self.page.evaluate("key => { const s=window[key]; if(s) { s.socket.close(); delete window[key]; } }", self.key)
        except Exception:
            pass  # A closed browser needs no further socket cleanup.

    async def send(self, data):
        await self.page.evaluate("arg => { const s=window[arg.key]; if(!s || s.socket.readyState!==1) throw new Error('WS_CLOSED'); s.socket.send(arg.data); }",
            {'key': self.key, 'data': data})

    async def recv(self):
        return await self.page.evaluate(RECEIVE, self.key)


class BrowserTransport:
    def __init__(self, page, timeout, token=None):
        self.page = page
        self.timeout = timeout
        self.token = token

    def connect(self):
        return BrowserSocket(self.page, self.timeout, self.token)

    async def request(self, url, token, body=None, *, method=None, allow_empty=False):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc not in ('plus.character.ai', 'neo.character.ai'):
            raise Failure('CAI_ENDPOINT_NOT_ALLOWED')
        method = method or ('GET' if body is None else 'POST')
        archive_endpoint = (parsed.netloc == 'neo.character.ai' and method == 'PATCH'
            and re.fullmatch(r'/chat/[A-Za-z0-9_-]+/archive', parsed.path) and not parsed.query)
        persona_archive = parsed.netloc == 'plus.character.ai' and method == 'POST' and parsed.path == '/chat/persona/update/'
        if method not in ('GET', 'POST', 'PATCH') or method == 'PATCH' and not archive_endpoint:
            raise Failure('CAI_METHOD_NOT_ALLOWED')
        if allow_empty and not (archive_endpoint or persona_archive):
            raise Failure('CAI_EMPTY_RESPONSE_NOT_ALLOWED')
        origin = urlsplit(self.page.url)
        if origin.scheme != 'https' or origin.netloc != 'character.ai':
            raise Failure('CAI_BROWSER_SESSION_CLOSED_OR_CHANGED')
        task = asyncio.create_task(self.page.evaluate(HTTP,
            {'url': url, 'token': token, 'body': body, 'method': method, 'allowEmpty': allow_empty,
             'timeout': self.timeout * 1000, 'enforceSession': self.token is not None}))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Wait for an in-flight mutation before starting Persona restoration.
            try:
                await task
            except Exception:
                pass
            raise
        except Exception:
            raise Failure('CAI_BROWSER_SESSION_CLOSED_OR_CHANGED') from None
        if result.get('error'):
            raise Failure(result['error'], result.get('details'))
        return result['data']
