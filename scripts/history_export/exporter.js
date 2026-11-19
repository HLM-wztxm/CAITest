/* Character.AI raw-history exporter. No packages or LLM; optional authorized archive-state restoration. */
module.exports = (function () {
  'use strict';
  const VERSION = '1.4.0';
  const ORIGIN = 'https://neo.character.ai';
  const encoder = new TextEncoder();
  class ExportError extends Error {
    constructor(code, context = {}) { super(code); this.code = code; this.context = context; }
  }
  const fail = (code, context) => { throw new ExportError(code, context); };
  const json = data => JSON.stringify(data, null, 2) + '\n';
  const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
    : value && typeof value === 'object' ? '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}'
      : JSON.stringify(value);
  async function sha256(value) {
    const bytes = typeof value === 'string' ? encoder.encode(value) : value;
    return Array.from(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  }
  function safeIssue(error, context = {}) {
    return {code: error instanceof ExportError ? error.code : 'UNEXPECTED_FAILURE', ...context,
      ...(error instanceof ExportError ? error.context : {})};
  }
  function checkNumbers(value) {
    if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) fail('UNSAFE_JSON_INTEGER');
    if (value && typeof value === 'object') for (const v of Object.values(value)) checkNumbers(v);
  }
  function checkObject(value, code) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) fail(code);
  }
  function nextCursor(data, kind) {
    const value = kind === 'chats' ? data.next_token : data.meta?.next_token;
    if (value == null || value === '') return null;
    if (typeof value !== 'string') fail('INVALID_CURSOR');
    return value;
  }
  function pageItems(data, key) {
    checkObject(data, 'INVALID_PAGE');
    // The live /chats/ endpoint returns {} for an exhausted page, not {chats:[]}.
    if (key === 'chats' && Object.keys(data).length === 0) return [];
    if (!Array.isArray(data[key])) fail('MISSING_PAGE_ARRAY', {field: key});
    if (key === 'turns' && (!data.meta || typeof data.meta !== 'object')) fail('MISSING_TURNS_META');
    return data[key];
  }
  function countValue(value) {
    if (value == null) return null;
    if (!/^\d+$/.test(String(value)) || !Number.isSafeInteger(Number(value))) fail('INVALID_TURN_COUNT');
    return Number(value);
  }
  function validateTurn(turn, sessionId) {
    checkObject(turn, 'INVALID_TURN');
    if (turn.turn_key?.chat_id !== sessionId || typeof turn.turn_key?.turn_id !== 'string' || !turn.turn_key.turn_id) fail('TURN_ID_MISMATCH');
    if (!turn.author || typeof turn.author !== 'object' || !Array.isArray(turn.candidates)) fail('INVALID_TURN_SHAPE');
    const ids = new Set();
    for (const c of turn.candidates) {
      if (!c || typeof c.candidate_id !== 'string' || !c.candidate_id || ids.has(c.candidate_id)) fail('INVALID_CANDIDATE_ID');
      ids.add(c.candidate_id);
      if ('raw_content' in c && typeof c.raw_content !== 'string') fail('INVALID_RAW_CONTENT');
      if (c.is_final === false) fail('CANDIDATE_STILL_GENERATING');
    }
    if (turn.candidates.length && (typeof turn.primary_candidate_id !== 'string' || !ids.has(turn.primary_candidate_id))) fail('SELECTED_CANDIDATE_MISSING');
    // Unknown candidate pagination fields cannot be silently ignored.
    if (Object.entries(turn).some(([k, v]) => /candidate.*(next_token|cursor)|has_more_candidates/i.test(k) && v)) fail('CANDIDATE_PAGINATION_UNSUPPORTED');
  }
  function selectedTextInventory(turns) {
    return turns.map(t => ({turn_id: t.turn_key.turn_id, primary_candidate_id: t.primary_candidate_id ?? null,
      candidates: t.candidates.map(c => ({candidate_id: c.candidate_id,
        ...('raw_content' in c ? {raw_content: c.raw_content} : {}), is_final: c.is_final ?? null}))}));
  }
  function localizedName(info, recent, locale) {
    const language = ({'zh-CN': 'cn', 'zh-TW': 'tw', 'zh-HK': 'tw', 'zh': 'cn'})[locale] || locale?.split('-')[0];
    const translations = recent?.character_translations?.name || info?.translations?.name;
    return translations?.[language] || recent?.character_name || info?.name || info?.participant__name || null;
  }
  function filenames(characters) {
    const used = new Set(['_manifest.json', '_evidence.jsonl', '_readme.txt', 'add_requests.json',
      ...characters.map((_, i) => `_raw_${i + 1}.json`)]);
    return characters.map(c => {
      let name = String(c.name).replace(/[\u0000-\u001f<>:"/\\|?*]/g, '_').replace(/[ .]+$/g, '');
      if (!name) name = 'unnamed';
      if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) name = '_' + name;
      name = Array.from(name).slice(0, 80).join('');
      let file = name + '.json', suffix = 1;
      while (used.has(file.normalize('NFC').toLowerCase())) file = name + ' [' + c.id.slice(0, 12).replace(/[^\w-]/g, '_') + '-' + suffix++ + '].json';
      used.add(file.normalize('NFC').toLowerCase());
      return file;
    });
  }
  function crc32(bytes) {
    let c = 0xffffffff;
    for (const byte of bytes) {
      c ^= byte;
      for (let i = 0; i < 8; i++) c = (c >>> 1) ^ ((c & 1) ? 0xedb88320 : 0);
    }
    return (c ^ 0xffffffff) >>> 0;
  }
  function zipStore(files) {
    if (files.size > 65535) fail('ZIP64_REQUIRED');
    const chunks = [], central = [];
    let offset = 0, centralSize = 0;
    for (const [name, text] of files) {
      if (name.includes('/') || name.includes('\\') || name === '.' || name === '..') fail('UNSAFE_ZIP_PATH');
      const fn = encoder.encode(name), data = typeof text === 'string' ? encoder.encode(text) : text;
      if (fn.length > 65535 || data.length >= 0xffffffff || offset + data.length >= 0xffffffff) fail('ZIP64_REQUIRED');
      const crc = crc32(data), local = new Uint8Array(30 + fn.length), lv = new DataView(local.buffer);
      lv.setUint32(0, 0x04034b50, true); lv.setUint16(4, 20, true); lv.setUint16(6, 0x0800, true);
      lv.setUint16(12, 33, true); lv.setUint32(14, crc, true); lv.setUint32(18, data.length, true); lv.setUint32(22, data.length, true);
      lv.setUint16(26, fn.length, true); local.set(fn, 30);
      const cd = new Uint8Array(46 + fn.length), cv = new DataView(cd.buffer);
      cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true); cv.setUint16(8, 0x0800, true);
      cv.setUint16(14, 33, true); cv.setUint32(16, crc, true); cv.setUint32(20, data.length, true); cv.setUint32(24, data.length, true);
      cv.setUint16(28, fn.length, true); cv.setUint32(42, offset, true); cd.set(fn, 46);
      chunks.push(local, data); central.push(cd); offset += local.length + data.length; centralSize += cd.length;
    }
    if (offset + centralSize >= 0xffffffff) fail('ZIP64_REQUIRED');
    const end = new Uint8Array(22), ev = new DataView(end.buffer);
    ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, files.size, true); ev.setUint16(10, files.size, true);
    ev.setUint32(12, centralSize, true); ev.setUint32(16, offset, true);
    const result = new Uint8Array(offset + centralSize + end.length);
    let at = 0;
    for (const chunk of [...chunks, ...central, end]) { result.set(chunk, at); at += chunk.length; }
    return result;
  }
  function memoryString(value, code) {
    if (typeof value !== 'string' || !value.trim() || Array.from(value).length > 512) fail(code);
    return value;
  }
  async function toMemoryAddRequest(character, session) {
    if (session.status !== 'complete') fail('MEMORY_SESSION_INCOMPLETE');
    const creator = session.metadata?.creator_id;
    const humanIds = [...new Set(session.turns.filter(t => t.author?.is_human === true).map(t => t.author.author_id))];
    const owner = creator != null ? String(creator) : humanIds.length === 1 ? humanIds[0] : null;
    memoryString(owner, 'MEMORY_USER_ID_MISSING');
    const user_id = memoryString('character-ai:' + owner, 'MEMORY_USER_ID_INVALID');
    const session_id = memoryString(session.session_id, 'MEMORY_SESSION_ID_INVALID');
    const messages = session.turns.map(turn => {
      const human = turn.author?.is_human === true;
      if (!human && turn.author?.author_id !== character.id) fail('MEMORY_UNKNOWN_SPEAKER');
      const role = memoryString(human ? turn.author.name : character.name, 'MEMORY_SPEAKER_NAME_MISSING');
      const candidate = turn.candidates.find(c => c.candidate_id === turn.primary_candidate_id);
      if (!candidate) fail('MEMORY_SELECTED_CANDIDATE_MISSING');
      if (typeof candidate.raw_content !== 'string') fail('MEMORY_SELECTED_TEXT_MISSING');
      const message = {role, content: candidate.raw_content};
      if (turn.create_time != null) {
        if (typeof turn.create_time !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(turn.create_time)) fail('MEMORY_TIMESTAMP_INVALID');
        const timestamp = Date.parse(turn.create_time);
        if (!Number.isSafeInteger(timestamp) || timestamp < 100000000000 || timestamp > 32503680000000) fail('MEMORY_TIMESTAMP_INVALID');
        message.timestamp = timestamp;
      }
      return message;
    });
    const payload = {user_id, session_id, messages};
    return {request_id: 'cai-add-' + await sha256(canonical(payload)), ...payload};
  }
  // Shared by live exports and offline reformatting of verified snapshots.
  async function formatExport(sourceManifest, roleFiles, evidenceText) {
    const manifest = {...sourceManifest, schema_version: 2, exporter_version: VERSION,
      formatted_at: new Date().toISOString(), characters: [], files: [], issues: [...sourceManifest.issues]};
    const files = new Map(), roleNames = filenames(roleFiles.map(f => f.character)), allRequests = [], conversionIssues = [];
    for (let i = 0; i < roleFiles.length; i++) {
      const f = roleFiles[i], name = roleNames[i], rawName = `_raw_${i + 1}.json`, requests = [];
      for (const session of f.sessions) {
        try { requests.push(await toMemoryAddRequest(f.character, session)); }
        catch (error) { conversionIssues.push(safeIssue(error, {character_id: f.character.id, session_id: session.session_id})); }
      }
      files.set(name, json(requests));
      files.set(rawName, json(f));
      allRequests.push(...requests);
      manifest.characters.push({id: f.character.id, name: f.character.name, file: name, raw_file: rawName, status: f.status,
        sessions: f.sessions.length, turns: f.sessions.reduce((n,s) => n+s.turn_count,0),
        candidates: f.sessions.reduce((n,s) => n+s.candidate_count,0),
        session_ids: f.sessions.map(s=>s.session_id), add_requests: requests.length});
    }
    manifest.memory_add = {format: 'memory-plugin-leaderboard-add-v1', file: 'add_requests.json',
      requests: allRequests.length, messages: allRequests.reduce((n,r) => n+r.messages.length,0), issues: conversionIssues,
      role_policy: 'human: per-message author.name (Persona); AI: exported webpage character name',
      content_policy: 'selected primary_candidate_id raw_content, unchanged; all candidates retained in raw files',
      timestamp_policy: 'turn.create_time to Unix milliseconds; absent timestamps omitted',
      user_id_policy: 'character-ai:<session creator_id>; unique human author_id fallback',
      request_id_policy: 'cai-add- plus SHA256 of canonical {user_id, session_id, messages}'};
    manifest.issues.push(...conversionIssues);
    if (conversionIssues.length) manifest.status = 'partial';
    files.set('add_requests.json', json(allRequests));
    files.set('_evidence.jsonl', evidenceText);
    files.set('_README.txt', `Export scope: ${manifest.scope_type === 'character' ? 'character ' + manifest.target_character_id : 'account'}\n` +
      'Character.AI → memory-plugin Add\n\n' +
      'add_requests.json is the native request array accepted by scripts/leaderboard/add_memory.py; CHARACTER_NAME.json is the subset for that character in the same format.\n' +
      'Each original session corresponds to one request. messages are ordered from oldest to newest and include role, content, and timestamp when available (Unix milliseconds).\n' +
      'The user role uses the Persona name from each history message. The AI role uses the character name shown on the website, or the localized API name when the character is not in the sidebar.\n' +
      'content includes only the version selected by primary_candidate_id, preserving the original text, whitespace, Markdown, and Unicode, including greetings and test chats.\n' +
      '_raw_N.json preserves the original sessions/turns, all candidates, selection markers, and metadata. See raw_file in _manifest.json for the mapping.\n' +
      'No Add request is generated for incomplete sessions or sessions with missing names, selected text, or user IDs, or invalid timestamps. See memory_add.issues for details; the overall result is marked partial.\n' +
      'user_id is character-ai:<ORIGINAL_ACCOUNT_ID>, and session_id preserves the original session ID. request_id is a stable digest derived from the request content.\n' +
      'complete means only that enumeration of the declared scope, two-pass verification of the original text, and format conversion passed. This is not a transactional snapshot.\n' +
      '_evidence.jsonl stores successful HTTP JSON responses and hashes, without request credentials. Only readable history is exported; deleted data is not recovered. Attachments are retained in the raw backup; media files are not downloaded.\n' +
      'These files are prepared locally only. The exporter does not call memory-plugin or submit memories.\n');
    for (const [name,text] of files) manifest.files.push({name, bytes: encoder.encode(text).length, sha256: await sha256(text)});
    manifest.totals = {characters: roleFiles.length, sessions: roleFiles.reduce((n,f)=>n+f.sessions.length,0),
      turns: roleFiles.reduce((n,f)=>n+f.sessions.reduce((m,s)=>m+s.turn_count,0),0),
      candidates: roleFiles.reduce((n,f)=>n+f.sessions.reduce((m,s)=>m+s.candidate_count,0),0),
      multi_candidate_turns: roleFiles.reduce((n,f)=>n+f.sessions.reduce((m,s)=>m+s.turns.filter(t=>t.candidates?.length>1).length,0),0)};
    files.set('_manifest.json', json(manifest));
    return {manifest, files, zip: zipStore(files)};
  }
  async function exportAccount(options) {
    const characterId = options.characterId;
    if (characterId !== undefined && (typeof characterId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(characterId))) fail('INVALID_CHARACTER_ID');
    const scoped = characterId !== undefined;
    const sessionScope = options.sessionScope ?? 'live';
    if (!['live', 'initial'].includes(sessionScope) || (sessionScope === 'initial' && !scoped)) fail('INVALID_SESSION_SCOPE');
    const initialOnly = sessionScope === 'initial';
    let initialIds = new Set();
    const postSnapshotIds = new Set();
    const selectedSidebar = (options.sidebar || []).filter(c => !scoped || c.id === characterId);
    let token = options.token;
    if (typeof token !== 'string' || !token || /[\r\n]/.test(token)) fail('LOGIN_REQUIRED');
    const doFetch = options.fetch || globalThis.fetch;
    const pause = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    const limit = options.sessionPageSize ?? 50, maxPages = options.maxPages ?? 10000;
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) fail('INVALID_PAGE_SIZE');
    const signal = options.signal, started = new Date().toISOString(), evidence = [], issues = [], requestLog = [];
    const authorizedArchives = new Set(options.archivedSessionIds || []), archiveAccess = [];
    const manifest = {schema_version: 1, exporter_version: VERSION, started_at: started, finished_at: null,
      status: 'in_progress', scope: scoped ? 'all accessible one-on-one histories for the selected character' : 'all accessible one-on-one account histories plus recent/sidebar characters',
      scope_type: scoped ? 'character' : 'account', target_character_id: characterId ?? null,
      session_scope: sessionScope, session_snapshot: null,
      snapshot: 'non_transactional_two_pass_read', raw_text_policy: 'raw_content strings unchanged; no trimming, translation or Unicode normalization',
      version_policy: 'all candidates returned for every turn; primary_candidate_id is the server selection',
      excluded: ['deleted/inaccessible records', 'hidden prompts and internal memory', 'media binary files'],
      enumeration: {sidebar: selectedSidebar, session_page_size: limit}, characters: [], issues, files: [], requests: requestLog,
      archive_access: archiveAccess};
    let totalEvidenceBytes = 0;
    const checkAbort = () => { if (signal?.aborted) fail('ABORTED'); };
    const notify = value => { if (options.onProgress) { try { options.onProgress(value); } catch (_) { /* observers cannot break export */ } } };
    async function request(path, params = {}, body, phase = '', cleanup = false) {
      if (!cleanup) checkAbort();
      const isNames = path === '/character/v1/get_character_infos';
      const archiveMatch = /^\/chat\/([A-Za-z0-9_-]+)\/(unarchive|archive)$/.exec(path);
      if (archiveMatch && !authorizedArchives.has(archiveMatch[1])) fail('ARCHIVE_PERMISSION_REQUIRED');
      if (!(isNames || archiveMatch || path === '/chats/' || path === '/chats/recent/' || /^\/turns\/[A-Za-z0-9_-]+\/$/.test(path))) fail('ENDPOINT_NOT_ALLOWED');
      if (body !== undefined && !isNames) fail('WRITE_REQUEST_FORBIDDEN');
      const url = new URL(path, ORIGIN);
      for (const [k, v] of Object.entries(params)) if (v != null) url.searchParams.set(k, String(v));
      const method = archiveMatch ? 'PATCH' : isNames ? 'POST' : 'GET';
      const attempts = archiveMatch ? 1 : 3; // Never automatically repeat a state-changing request.
      for (let attempt = 0; attempt < attempts; attempt++) {
        if (!cleanup) checkAbort();
        await pause(options.requestDelayMs ?? 180);
        const controller = new AbortController(), onAbort = () => controller.abort();
        if (!cleanup) signal?.addEventListener('abort', onAbort, {once: true});
        const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 30000);
        let response, text;
        try {
          response = await doFetch(url.href, {method, redirect: 'error', credentials: 'omit', signal: controller.signal,
            headers: {Accept: 'application/json', Authorization: 'Token ' + token, ...(isNames ? {'Content-Type': 'application/json'} : {})},
            ...(isNames ? {body: JSON.stringify(body)} : {})});
          // Materialize body while timeout and abort protection are still active.
          text = await response.text();
        } catch (_) { if (!cleanup) checkAbort(); fail('NETWORK_OR_TIMEOUT', {phase, path}); }
        finally { clearTimeout(timeout); signal?.removeEventListener('abort', onAbort); }
        requestLog.push({phase, path, method, status: response.status, attempt: attempt + 1});
        if ([429, 502, 503, 504].includes(response.status) && attempt < attempts - 1) {
          const raw = response.headers.get('retry-after');
          const wait = raw ? (/^\d+(\.\d+)?$/.test(raw) ? Number(raw) * 1000 : Date.parse(raw) - Date.now()) : 1000 * 2 ** attempt;
          if (!Number.isFinite(wait) || wait > 60000) fail('RETRY_LATER', {phase, status: response.status});
          await pause(Math.max(wait, 500)); continue;
        }
        if (!response.ok) fail([401,403].includes(response.status) ? 'AUTH_OR_ACCESS_DENIED' : 'HTTP_ERROR', {phase, path, status: response.status});
        let data;
        try { data = text === '' && archiveMatch ? {} : JSON.parse(text); } catch (_) { fail('NON_JSON_RESPONSE', {phase, path}); }
        checkObject(data, 'INVALID_API_OBJECT'); checkNumbers(data);
        if ('error' in data || 'errors' in data) fail('API_ERROR', {phase, path});
        // Reject scope violations before persisting the response or following its IDs.
        if (scoped && path === '/chats/' && Array.isArray(data.chats) && data.chats.some(c => c.character_id !== characterId)) fail('WRONG_CHARACTER_HISTORY', {phase});
        if (scoped && isNames && Array.isArray(data.characters) && data.characters.some(c => c.external_id !== characterId)) fail('WRONG_CHARACTER_INFO', {phase});
        totalEvidenceBytes += encoder.encode(text).length;
        if (!cleanup && totalEvidenceBytes > (options.maxEvidenceBytes ?? 200 * 1024 * 1024)) fail('EXPORT_MEMORY_LIMIT');
        const record = {index: evidence.length, phase, method, path, query: params, ...(body ? {body} : {}),
          captured_at: new Date().toISOString(), sha256: await sha256(text), response_text: text};
        evidence.push(record);
        return {data, evidence_index: record.index};
      }
      fail('RETRIES_EXHAUSTED');
    }
    async function pages(path, params, key, kind, phase, validate = () => {}, cleanup = false) {
      const result = {items: [], evidence_indices: [], pages: 0, terminal_cursor: null, exhausted: false, duplicates: 0, conflicts: []};
      const cursors = new Set(), found = new Map();
      let cursor = null;
      try {
        for (let page = 0; page < maxPages; page++) {
          const {data, evidence_index} = await request(path, {...params, ...(cursor ? {next_token: cursor} : {})}, undefined, phase, cleanup);
          result.evidence_indices.push(evidence_index); result.pages++;
          const items = pageItems(data, key);
          for (const item of items) {
            checkObject(item, 'INVALID_LIST_ITEM');
            const id = key === 'turns' ? item.turn_key?.turn_id : item.chat_id;
            if (typeof id !== 'string' || !id) fail('MISSING_RECORD_ID');
            // Keep the raw record even when its shape or selection is incomplete.
            if (found.has(id)) {
              result.duplicates++;
              if (canonical(found.get(id)) !== canonical(item)) result.conflicts.push(item);
            } else { found.set(id, item); result.items.push(item); }
            validate(item);
          }
          const next = nextCursor(data, kind);
          result.terminal_cursor = next;
          if (!next) { result.exhausted = true; break; }
          if (next === cursor || cursors.has(next)) fail('CURSOR_LOOP', {phase});
          cursors.add(next); cursor = next;
        }
        if (!result.exhausted) fail('PAGE_LIMIT_REACHED', {phase});
        if (result.conflicts.length) fail('DUPLICATE_RECORD_CHANGED', {phase});
      } catch (error) { result.error = safeIssue(error, {phase}); }
      return result;
    }
    const chatParams = {num_preview_turns: 0, include_turn_count: true, num_summaries: 0, limit,
      ...(scoped ? {character_ids: characterId} : {})};
    const chatValid = c => { if (typeof c.character_id !== 'string' || !c.character_id) fail('MISSING_CHARACTER_ID'); countValue(c.turn_count); };
    const reportPageError = p => { if (p.error) issues.push(p.error); };
    const roleFiles = [];
    try {
      notify({phase: 'enumerate', message: scoped ? 'Reading all history sessions for the target character' : 'Reading account characters and all history sessions'});
      const all = await pages('/chats/', chatParams, 'chats', 'chats', 'account_initial', chatValid);
      manifest.enumeration.account_initial = {...all, items: all.items.map(c => c.chat_id)}; reportPageError(all);
      initialIds = new Set(all.items.map(c => c.chat_id));
      if (initialOnly && all.exhausted && !all.error) {
        manifest.session_snapshot = {captured_at: new Date().toISOString(), session_ids: [...initialIds]};
        manifest.scope = 'all accessible one-on-one histories in the selected character initial session inventory';
        // The caller may now create a separate test chat, never mutate these sessions.
        notify({phase: 'snapshot_ready', ...manifest.session_snapshot});
      }
      const recent = scoped ? {items: [], evidence_indices: [], pages: 0, exhausted: true, skipped: true}
        : await pages('/chats/recent/', {include_restricted: true}, 'chats', 'recent', 'recent_initial', chatValid);
      manifest.enumeration.recent = {...recent, items: recent.items.map(c => c.chat_id)}; reportPageError(recent);
      if (!all.exhausted || !recent.exhausted) issues.push({code: 'ENUMERATION_INCOMPLETE'});
      const byId = new Map(), sidebar = new Map(selectedSidebar.map(c => [c.id, c.name]));
      if (scoped) byId.set(characterId, {id: characterId, sessions: new Map()});
      for (const c of [...all.items, ...recent.items]) {
        if (c.type && c.type !== 'TYPE_ONE_ON_ONE') { issues.push({code: 'UNSUPPORTED_CHAT_TYPE', chat_id: c.chat_id, type: c.type}); continue; }
        if (c.character_id && !byId.has(c.character_id)) byId.set(c.character_id, {id: c.character_id, sessions: new Map()});
        if (c.character_id) byId.get(c.character_id).sessions.set(c.chat_id, c);
      }
      for (const [id] of sidebar) if (!byId.has(id)) byId.set(id, {id, sessions: new Map()});
      const ids = [...byId.keys()], names = new Map();
      for (let start = 0; start < ids.length; start += 25) {
        try {
          const {data} = await request('/character/v1/get_character_infos', {}, {external_ids: ids.slice(start, start + 25)}, 'character_names');
          if (!Array.isArray(data.characters)) fail('INVALID_CHARACTER_NAMES');
          for (const c of data.characters) if (c.external_id) names.set(c.external_id, c);
        } catch (error) { issues.push(safeIssue(error, {phase: 'character_names'})); }
      }
      const archived = all.items.filter(c => c.state === 'STATE_ARCHIVED');
      if (archived.length && options.authorizeArchived) {
        const granted = await options.authorizeArchived(archived.map(c => ({session_id:c.chat_id,
          character_name:sidebar.get(c.character_id)||localizedName(names.get(c.character_id),recent.items.find(r=>r.character_id===c.character_id),options.locale||'en')||c.character_id,
          created_at:c.create_time,storage:c.storage||null})));
        if (!Array.isArray(granted) || granted.some(id=>!archived.some(c=>c.chat_id===id))) fail('INVALID_ARCHIVE_AUTHORIZATION');
        for (const id of granted) authorizedArchives.add(id);
      }
      let roleIndex = 0;
      for (const role of byId.values()) {
        checkAbort(); roleIndex++;
        const recentRecord = recent.items.find(c => c.character_id === role.id), info = names.get(role.id);
        role.name = sidebar.get(role.id) || localizedName(info, recentRecord, options.locale || 'en') || role.id;
        const file = {schema_version: 1, exporter_version: VERSION, exported_at: started,
          character: {id: role.id, name: role.name, source_name: info?.name || recentRecord?.character_name || null,
            name_source: sidebar.has(role.id) ? 'visible_sidebar' : 'api_localized', present_in_recent_sidebar: !!recentRecord},
          status: 'in_progress', issues: [], sessions: []};
        roleFiles.push(file);
        if (!sidebar.has(role.id) && !info?.name && !recentRecord?.character_name) file.issues.push({code: 'CHARACTER_NAME_UNAVAILABLE'});
        notify({phase: 'character', character: role.name, index: roleIndex, total: ids.length});
        const history = await pages('/chats/', {...chatParams, character_ids: role.id}, 'chats', 'chats', 'history:' + role.id, c => {
          chatValid(c); if (c.character_id !== role.id) fail('WRONG_CHARACTER_HISTORY');
        });
        file.history_pagination = {...history, items: history.items.map(c => c.chat_id)};
        if (history.error) file.issues.push(history.error);
        for (const c of history.items) {
          if (initialOnly) {
            if (!initialIds.has(c.chat_id)) postSnapshotIds.add(c.chat_id);
            // Keep initial metadata/counts so later mutations are still detected.
          } else role.sessions.set(c.chat_id, c);
        }
        for (const c of role.sessions.values()) {
          const id = c.chat_id, expected = countValue(c.turn_count);
          const session = {session_id: id, name: c.name ?? null, metadata: c, status: 'in_progress',
            expected_turn_count: expected, turn_count: 0, candidate_count: 0, issues: [], turns: []};
          file.sessions.push(session);
          notify({phase: 'session', character: role.name, session_id: id});
          if (c.state === 'STATE_ARCHIVED' && authorizedArchives.has(id)) {
            const access={session_id:id,character_id:role.id,original_state:c.state,original_storage:c.storage||null,
              unarchive_requested_at:new Date().toISOString(),restore_status:'pending'};
            archiveAccess.push(access); // Record before sending: even an ambiguous timeout requires cleanup.
            try {
              await request('/chat/'+encodeURIComponent(id)+'/unarchive',{},undefined,'unarchive:'+id);
              const check=await pages('/chats/',{...chatParams,character_ids:role.id},'chats','chats','unarchive_check:'+id,chatValid);
              const current=check.items.find(x=>x.chat_id===id);
              if(check.error||!check.exhausted||current?.state!=='STATE_ACTIVE')fail('UNARCHIVE_NOT_VERIFIED');
              access.unarchive_verified=true;access.storage_after_unarchive=current.storage||null;
              session.access={temporarily_unarchived:true,original_state:c.state,original_storage:c.storage||null};
              // Cold restoration can make a previously unavailable count visible.
              session.expected_turn_count=countValue(current.turn_count);
            } catch(error) {session.issues.push(safeIssue(error,{session_id:id}));session.status='partial';continue;}
          }
          const path = '/turns/' + encodeURIComponent(id) + '/';
          const first = await pages(path, {}, 'turns', 'turns', 'turns_initial:' + id, t => validateTurn(t, id));
          session.turns = [...first.items].reverse();
          session.pagination = {...first, items: undefined};
          session.turn_count = session.turns.length;
          session.candidate_count = session.turns.reduce((n, t) => n + (t.candidates?.length || 0), 0);
          if (first.error) session.issues.push(first.error);
          if (session.expected_turn_count !== null && session.expected_turn_count !== session.turn_count) session.issues.push({code: 'TURN_COUNT_MISMATCH', expected:session.expected_turn_count, actual: session.turn_count});
          if (first.exhausted && !first.error) {
            const second = await pages(path, {}, 'turns', 'turns', 'turns_verify:' + id, t => validateTurn(t, id));
            session.verification = {pagination: {...second, items: undefined}, equal: false,
              raw_turns_sha256: await sha256(canonical(session.turns)),
              text_and_selection_sha256: await sha256(canonical(selectedTextInventory(session.turns)))};
            if (second.error) session.issues.push(second.error);
            session.verification.equal = second.exhausted && !second.error && canonical(session.turns) === canonical([...second.items].reverse());
            if (!session.verification.equal) session.issues.push({code: 'SESSION_CHANGED_DURING_EXPORT'});
          }
          session.status = session.issues.length ? 'partial' : 'complete';
        }
        file.sessions.sort((a, b) => String(a.metadata.create_time || '').localeCompare(String(b.metadata.create_time || '')) || a.session_id.localeCompare(b.session_id));
        file.status = file.issues.length || file.sessions.some(s => s.status !== 'complete') ? 'partial' : 'complete';
      }
      const final = await pages('/chats/', chatParams, 'chats', 'chats', 'account_final', chatValid);
      manifest.enumeration.account_final = {...final, items: final.items.map(c => c.chat_id)}; reportPageError(final);
      if (initialOnly) for (const c of final.items) if (!initialIds.has(c.chat_id)) postSnapshotIds.add(c.chat_id);
      manifest.enumeration.post_snapshot_session_ids = [...postSnapshotIds].sort();
      const relevantFinal = initialOnly ? final.items.filter(c => initialIds.has(c.chat_id)) : final.items;
      const inventory = rows => rows.map(c => [c.chat_id, c.character_id, authorizedArchives.has(c.chat_id)?'authorized_archive_access':c.turn_count ?? null]).sort((a,b) => a[0].localeCompare(b[0]));
      manifest.enumeration.stable = canonical(inventory(all.items)) === canonical(inventory(relevantFinal));
      if (!manifest.enumeration.stable) issues.push({code: scoped ? 'CHARACTER_CHANGED_DURING_EXPORT' : 'ACCOUNT_CHANGED_DURING_EXPORT'});
      const exportedIds = new Set(roleFiles.flatMap(f => f.sessions.map(s => s.session_id)));
      const missing = relevantFinal.filter(c => !exportedIds.has(c.chat_id)).map(c => c.chat_id);
      if (missing.length) issues.push({code: 'SESSIONS_NOT_EXPORTED', session_ids: missing});
    } catch (error) { issues.push(safeIssue(error)); }
    finally {
      for(const access of archiveAccess) {
        try {
          notify({phase:'restore_archive',session_id:access.session_id});
          // Cleanup intentionally ignores cancellation; it must restore original archive state.
          await request('/chat/'+encodeURIComponent(access.session_id)+'/archive',{},undefined,'rearchive:'+access.session_id,true);
          const check=await pages('/chats/',{...chatParams,character_ids:access.character_id},'chats','chats','rearchive_check:'+access.session_id,chatValid,true);
          const restored=check.items.find(c=>c.chat_id===access.session_id);
          if(check.error||!check.exhausted||restored?.state!==access.original_state)fail('REARCHIVE_NOT_VERIFIED');
          access.restore_status='verified';access.restored_at=new Date().toISOString();access.storage_after_restore=restored.storage||null;
        }catch(error){access.restore_status='failed';access.error=safeIssue(error);issues.push(safeIssue(error,{session_id:access.session_id,action:'restore_archive'}));}
      }
      token = null;
    }
    for (const file of roleFiles) {
      if (file.status === 'in_progress') file.status = 'partial';
      for (const s of file.sessions) if (s.status === 'in_progress') s.status = 'partial';
    }
    manifest.finished_at = new Date().toISOString();
    manifest.incomplete_sessions=roleFiles.flatMap(f=>f.sessions.filter(s=>s.status!=='complete').map(s=>({character:f.character.name,session_id:s.session_id,issues:s.issues})));
    manifest.status = issues.length || roleFiles.some(f => f.status !== 'complete') ? 'partial' : 'complete';
    if (!roleFiles.length && !manifest.enumeration.account_initial?.exhausted) manifest.status = 'partial';
    const result = await formatExport(manifest, roleFiles, evidence.map(record => JSON.stringify(record)).join('\n') + '\n');
    notify({phase: 'done', status: result.manifest.status, totals: result.manifest.totals});
    return result;
  }

  return {VERSION, ExportError, exportAccount, formatExport, toMemoryAddRequest, zipStore, crc32, sha256, canonical, filenames, validateTurn};
})();
