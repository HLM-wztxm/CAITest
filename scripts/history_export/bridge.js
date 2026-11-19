// Function body appended after the vendored core by __init__.py. Credentials stay in Chrome.
const notify = value => { void globalThis[options.progress](value).catch(() => {}); };
let token;
try {
  if (location.origin !== 'https://character.ai') return {error: 'WRONG_PAGE_ORIGIN'};
  if (!globalThis.crypto?.subtle || typeof fetch !== 'function'
      || typeof TextEncoder !== 'function' || typeof AbortController !== 'function')
    return {error: 'BROWSER_RUNTIME_UNAVAILABLE'};
  let props;
  try { props = JSON.parse(document.getElementById('__NEXT_DATA__')?.textContent).props.pageProps; }
  catch { return {error: 'PAGE_BOOTSTRAP_NOT_FOUND'}; }
  token = props.token;
  if (typeof token !== 'string' || !token || /[\r\n]/.test(token)) return {error: 'LOGIN_REQUIRED'};
  notify({phase: 'account'});
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  let response, account;
  try {
    response = await fetch('https://neo.character.ai/user/', {method: 'GET', credentials: 'omit',
      redirect: 'error', headers: {Accept: 'application/json', Authorization: 'Token ' + token}, signal: controller.signal});
    if (!response.ok) return {error: 'ACCOUNT_HTTP_' + response.status};
    const data = await response.json();
    account = data?.user?.user;
    if (!['string', 'number'].includes(typeof account?.id)) return {error: 'ACCOUNT_RESPONSE_INVALID'};
  } catch { return {error: 'ACCOUNT_NETWORK_OR_RESPONSE_FAILED'}; }
  finally { clearTimeout(timer); }

  if (String(account.id) !== options.accountId) return {error: 'HISTORY_ACCOUNT_MISMATCH'};

  // Restrict this runner to reading histories and names, even though the core also
  // supports explicitly authorized archive changes for other callers.
  const readFetch = (url, init) => {
    const u = new URL(url), method = init?.method || 'GET';
    const allowed = u.origin === 'https://neo.character.ai'
      && (method === 'GET' && (['/chats/', '/chats/recent/'].includes(u.pathname)
          || /^\/turns\/[A-Za-z0-9_-]+\/$/.test(u.pathname))
        || method === 'POST' && u.pathname === '/character/v1/get_character_infos');
    if (!allowed) throw new Error('READ_ENDPOINT_REQUIRED');
    return fetch(url, init);
  };
  const sidebar = Array.from(document.querySelectorAll('[role="option"][id*="-option-"]'))
    .map(el => ({id: el.id.split('-option-').pop(), name: el.querySelector('img')?.alt}))
    .filter(item => /^[A-Za-z0-9_-]+$/.test(item.id) && typeof item.name === 'string' && item.name);
  const exported = await module.exports.exportAccount({token, fetch: readFetch, sidebar,
    locale: props.locale || document.documentElement.lang || 'en',
    ...(options.characterId ? {characterId: options.characterId, sessionScope: 'initial'} : {}),
    archivedSessionIds: [], sessionPageSize: 2, timeoutMs: options.timeoutMs, onProgress: notify});
  const m = exported.manifest;
  return {zip: exported.zip, summary: {account_id: String(account.id), export_status: m.status,
    totals: m.totals, issues: m.issues, incomplete_sessions: m.incomplete_sessions,
    zip_bytes: exported.zip.length, zip_sha256: await module.exports.sha256(exported.zip)}};
} catch (error) {
  return {error: error instanceof module.exports.ExportError ? error.code : 'EXPORT_FAILED'};
} finally {
  token = null;
}
