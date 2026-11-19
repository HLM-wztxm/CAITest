"""Character.AI HTTP/WebSocket adapter. One command per connection, no mutation retries.

Protocol reference: PyCharacterAI 2.2.50 method source (unofficial).
The normal login path keeps Chrome open for authenticated HTTP/WebSocket traffic.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import uuid
from urllib.parse import urlsplit
from common import Failure, request_json
from login import wait_for_login


def character_id(value):
    if '://' in value:
        url = urlsplit(value)
        match = re.fullmatch(r'/chat/([A-Za-z0-9_-]+)/?', url.path)
        if url.scheme != 'https' or url.netloc != 'character.ai' or not match:
            raise Failure('USE_CHARACTER_CHAT_LINK')
        value = match[1]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise Failure('INVALID_CHARACTER_ID')
    return value


@asynccontextmanager
async def open_cai_session(state_dir, target=None, channel='chrome', timeout=90, login_timeout=600):
    token = os.environ.get('CHARACTER_AI_TOKEN')
    if token:
        if any(c in token for c in '\r\n'):
            raise Failure('INVALID_LOGIN_TOKEN')
        client = CAI(token, timeout)
        try:
            yield client
        finally:
            client.token = None
        return
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise Failure('INSTALL_REQUIREMENTS_FIRST') from None
    from browser import BrowserTransport
    async with async_playwright() as playwright:
        context = None
        client = None
        original_auth_cookies = None
        try:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(state_dir / 'browser-profile'), channel=channel, headless=False)
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto('https://character.ai/', wait_until='domcontentloaded', timeout=60000)
                except BrowserTimeout:
                    pass  # A login/challenge page may still be usable.
                print(f'Complete login and any CAPTCHA in the separate Chrome window (up to {login_timeout} seconds). Keep it open after signing in; it closes automatically when the task ends.', flush=True)
                print('If Google sign-in is rejected, use the email address of your existing account. Open email verification links in this Chrome window; new tabs are supported.', flush=True)
                page, token = await wait_for_login(context, login_timeout)
                if target:
                    # A target can be a new character with no entry in Recent Chats.
                    # Wait for the target chat UI rather than requiring a sidebar row.
                    await page.goto('https://character.ai/chat/' + target,
                        wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_selector('#chat-input-textarea', timeout=30000)
                    page, token = await wait_for_login(context, login_timeout)
                original_auth_cookies = [c for c in await context.cookies('https://neo.character.ai/')
                    if c['name'] == 'HTTP_AUTHORIZATION' and c['domain'] == 'neo.character.ai']
                await context.add_cookies([{'name': 'HTTP_AUTHORIZATION', 'value': 'Token ' + token,
                    'url': 'https://neo.character.ai/', 'secure': True, 'httpOnly': True, 'sameSite': 'None'}])
                client = CAI(token, timeout, BrowserTransport(page, timeout, token))
            except BrowserTimeout:
                raise Failure('LOGIN_TIMEOUT_OR_PAGE_SCHEMA_CHANGED') from None
            except TimeoutError:
                raise Failure('LOGIN_TIMEOUT_OR_PAGE_SCHEMA_CHANGED') from None
            except Failure:
                raise
            except Exception:
                raise Failure('BROWSER_LOGIN_FAILED') from None
            yield client
        finally:
            if client:
                client.token = None
            token = None
            if context:
                if original_auth_cookies is not None:
                    try:
                        await context.clear_cookies(name='HTTP_AUTHORIZATION', domain='neo.character.ai')
                        if original_auth_cookies:
                            await context.add_cookies(original_auth_cookies)
                    except Exception:
                        pass  # The user may have already closed the browser.
                await context.close()


class CAI:
    def __init__(self, token, timeout=90, transport=None):
        self.token = token
        self.timeout = timeout
        self.account_id = None
        self.transport = transport

    def check_transport(self):
        if self.transport:
            return
        try:
            from websockets.asyncio.client import connect  # noqa: F401
        except ImportError:
            raise Failure('INSTALL_REQUIREMENTS_FIRST') from None

    async def http(self, path, body=None, plus=False, *, method=None, allow_empty=False):
        origin = 'https://plus.character.ai' if plus else 'https://neo.character.ai'
        options = {'method': method, 'allow_empty': allow_empty} if method is not None or allow_empty else {}
        if self.transport:
            return await self.transport.request(origin + path, self.token, body, **options)
        return await request_json(origin + path, headers={'Authorization': 'Token ' + self.token},
            body=body, timeout=self.timeout, service='CAI', **options)

    async def me(self):
        data = await self.http('/user/')
        try:
            user = data['user']['user']
            account_id = user['id']
            if type(account_id) not in (str, int) or not str(account_id):
                raise ValueError()
            self.account_id = str(account_id)
            username = user['username']
            if not self.account_id or not isinstance(username, str) or not username:
                raise ValueError()
            return {'id': self.account_id, 'username': username}
        except (KeyError, TypeError, ValueError):
            raise Failure('ACCOUNT_SCHEMA_CHANGED') from None

    async def info(self, target):
        data = await self.http('/character/v1/get_character_info', {'external_id': target, 'lang': 'en'})
        char = data.get('character') if isinstance(data, dict) else None
        if not isinstance(char, dict) or char.get('external_id') != target or data.get('status') != 'OK':
            raise Failure('CHARACTER_RESPONSE_INVALID')
        return char

    async def settings(self):
        data = await self.http('/chat/user/settings/', plus=True)
        if not isinstance(data, dict) or not any(k in data for k in ('personaOverrides', 'default_persona_id')) or not isinstance(data.get('personaOverrides', {}), dict):
            raise Failure('SETTINGS_SCHEMA_CHANGED')
        return data

    async def bind(self, target, persona, *, remove=False):
        settings = await self.settings()
        mapping = settings.setdefault('personaOverrides', {})
        if remove:
            mapping.pop(target, None)
        else:
            mapping[target] = persona
        result = await self.http('/chat/user/update_settings/', settings, plus=True)
        if not isinstance(result, dict) or result.get('success') is not True:
            raise Failure('PERSONA_BIND_FAILED')
        check = await self.settings()
        actual = check.get('personaOverrides', {})
        if (target in actual, actual.get(target)) != ((False, None) if remove else (True, persona)):
            raise Failure('PERSONA_BIND_NOT_VERIFIED')

    async def create_persona(self, name, definition, *, identifier=None):
        if not 3 <= len(name) <= 20 or not 0 < len(definition) <= 750:
            raise Failure('INVALID_TEST_PERSONA')
        body = {'avatar_file_name': '', 'avatar_rel_path': '', 'base_img_prompt': '', 'categories': [],
            'copyable': False, 'definition': definition, 'description': 'Definition recovery test persona.',
            'greeting': 'Hello', 'identifier': identifier or 'id:' + str(uuid.uuid4()), 'img_gen_enabled': False,
            'name': name, 'strip_img_prompt_from_msg': False, 'title': name, 'visibility': 'PRIVATE', 'voice_id': ''}
        data = await self.http('/chat/persona/create/', body, plus=True)
        if not isinstance(data, dict) or not isinstance(data.get('persona'), dict):
            raise Failure('PERSONA_CREATE_NOT_CONFIRMED')
        persona = data['persona']
        pid = persona.get('external_id')
        if data.get('status') != 'OK' or not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', pid):
            raise Failure('PERSONA_CREATE_NOT_CONFIRMED')
        return pid

    async def get_chat(self, chat_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', chat_id):
            raise Failure('INVALID_TEST_CHAT_ID')
        data = await self.http('/chat/' + chat_id + '/')
        chat = data.get('chat') if isinstance(data, dict) else None
        if not isinstance(chat, dict) or chat.get('chat_id') != chat_id:
            raise Failure('CLEANUP_CHAT_SCHEMA_CHANGED')
        return chat

    async def archive_chat(self, chat_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', chat_id):
            raise Failure('INVALID_TEST_CHAT_ID')
        return await self.http('/chat/' + chat_id + '/archive', {}, method='PATCH', allow_empty=True)

    async def get_persona(self, persona_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', persona_id):
            raise Failure('INVALID_TEST_PERSONA_ID')
        data = await self.http('/chat/persona/?id=' + persona_id, plus=True)
        persona = data.get('persona') if isinstance(data, dict) else None
        if not isinstance(persona, dict) or persona.get('external_id') != persona_id:
            raise Failure('CLEANUP_PERSONA_SCHEMA_CHANGED')
        return persona

    async def get_personas(self):
        data = await self.http('/chat/personas/?force_refresh=1', plus=True)
        # Absence is evidence only for a complete, recognized list response.
        if (not isinstance(data, dict) or set(data) - {'personas', 'status'}
                or data.get('status', 'OK') != 'OK' or not isinstance(data.get('personas'), list)):
            raise Failure('CLEANUP_PERSONA_LIST_SCHEMA_CHANGED')
        ids = []
        for item in data['personas']:
            pid = item.get('external_id') if isinstance(item, dict) else None
            if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', pid):
                raise Failure('CLEANUP_PERSONA_LIST_SCHEMA_CHANGED')
            ids.append(pid)
        if len(ids) != len(set(ids)):
            raise Failure('CLEANUP_PERSONA_LIST_SCHEMA_CHANGED')
        return data['personas']

    async def archive_persona(self, persona):
        # Preserve the object's current contents; change only the archive flag.
        if not isinstance(persona, dict) or not re.fullmatch(r'[A-Za-z0-9_-]+', persona.get('external_id', '')):
            raise Failure('INVALID_TEST_PERSONA_ID')
        return await self.http('/chat/persona/update/', {**persona, 'archived': True}, plus=True, allow_empty=True)

    async def ws(self, command, payload, accept):
        if self.transport:
            connection = self.transport.connect()
        else:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise Failure('INSTALL_REQUIREMENTS_FIRST') from None
            connection = connect('wss://neo.character.ai/ws/', origin='https://character.ai',
                additional_headers={'Cookie': 'HTTP_AUTHORIZATION=Token ' + self.token},
                open_timeout=min(30, self.timeout), close_timeout=5, max_size=2 * 1024 * 1024)
        rid = str(uuid.uuid4())
        try:
            async with asyncio.timeout(self.timeout):
                async with connection as ws:
                    await ws.send(json.dumps({'command': command, 'request_id': rid,
                        'origin_id': 'web-next', 'payload': payload}))
                    for _ in range(2000):
                        event = json.loads(await ws.recv())
                        if not isinstance(event, dict):
                            raise Failure('CAI_WS_INVALID_EVENT')
                        if event.get('request_id') not in (None, rid):
                            continue
                        if event.get('command') in ('neo_error', 'filter_user_input_self_harm'):
                            raise Failure('CAI_COMMAND_REJECTED')
                        result = accept(event)
                        if result is not None:
                            return result
                    raise Failure('CAI_EVENT_LIMIT')
        except Failure:
            raise
        except TimeoutError:
            raise Failure('CAI_COMMAND_TIMEOUT_UNCERTAIN') from None
        except Exception:
            raise Failure('CAI_CONNECTION_OR_PROTOCOL_FAILED') from None

    async def create_chat(self, target, chat_id):
        def accept(event):
            chat = event.get('chat', {})
            if event.get('command') == 'create_chat_response' and chat.get('chat_id') == chat_id and chat.get('character_id') == target:
                return chat_id
        return await self.ws('create_chat', {'chat': {'chat_id': chat_id, 'creator_id': self.account_id,
            'visibility': 'VISIBILITY_PRIVATE', 'character_id': target, 'type': 'TYPE_ONE_ON_ONE'},
            'with_greeting': False}, accept)

    async def generate(self, target, chat_id, text, previous=None, observe=lambda raw: None):
        payload = {'character_id': target, 'previous_annotations': {}, 'selected_language': '',
            'tts_enabled': False, 'user_name': ''}
        if previous:
            command = 'generate_turn_candidate'
            payload['turn_key'] = {'chat_id': chat_id, 'turn_id': previous['turn_id']}
            old_ids = set(previous['seen_ids'])
        else:
            command = 'create_and_generate_turn'
            cid = str(uuid.uuid4())
            payload.update(num_candidates=1, turn={'author': {'author_id': self.account_id, 'is_human': True, 'name': ''},
                'candidates': [{'candidate_id': cid, 'raw_content': text}], 'primary_candidate_id': cid,
                'turn_key': {'chat_id': chat_id, 'turn_id': str(uuid.uuid4())}})
            old_ids = set()

        def accept(event):
            raw = event.get('turn')
            if event.get('command') not in ('add_turn', 'update_turn') or not isinstance(raw, dict):
                return None
            key = raw.get('turn_key', {})
            if key.get('chat_id') != chat_id or raw.get('author', {}).get('is_human'):
                return None
            if raw.get('author', {}).get('author_id') != target or (previous and key.get('turn_id') != previous['turn_id']):
                raise Failure('CAI_TURN_TARGET_MISMATCH')
            observe(raw)
            candidates = raw.get('candidates', [])
            eligible = [c for c in candidates if c.get('candidate_id') not in old_ids and c.get('is_final') is True]
            if not eligible:
                return None
            selected = next((c for c in eligible if c.get('candidate_id') == raw.get('primary_candidate_id')), None)
            if selected is None and len(eligible) == 1:
                selected = eligible[0]
            if not selected or not isinstance(selected.get('raw_content'), str) or not key.get('turn_id'):
                raise Failure('CAI_CANDIDATE_AMBIGUOUS')
            if selected.get('safety_truncated'):
                raise Failure('CAI_CANDIDATE_FILTERED')
            return {'turn_id': key['turn_id'], 'candidate_id': selected['candidate_id'], 'text': selected['raw_content'],
                'seen_ids': sorted(old_ids | {c['candidate_id'] for c in candidates}), 'raw': raw}
        return await self.ws(command, payload, accept)

    async def select(self, chat_id, candidate):
        await self.ws('update_primary_candidate', {'candidate_id': candidate['candidate_id'],
            'turn_key': {'chat_id': chat_id, 'turn_id': candidate['turn_id']}},
            lambda event: True if event.get('command') == 'ok' else None)
        data = await self.http('/turns/' + chat_id + '/')
        turn = next((t for t in data.get('turns', []) if t.get('turn_key', {}).get('turn_id') == candidate['turn_id']), None)
        if not turn or turn.get('primary_candidate_id') != candidate['candidate_id']:
            raise Failure('CANDIDATE_SELECTION_NOT_VERIFIED')

    async def edit(self, chat_id, candidate, text):
        def accept(event):
            turn = event.get('turn', {})
            if event.get('command') == 'update_turn' and turn.get('turn_key') == {'chat_id': chat_id, 'turn_id': candidate['turn_id']}:
                if any(c.get('raw_content') == text for c in turn.get('candidates', [])):
                    return True
        return await self.ws('edit_turn_candidate', {'turn_key': {'chat_id': chat_id, 'turn_id': candidate['turn_id']},
            'current_candidate_id': candidate['candidate_id'], 'new_candidate_raw_content': text}, accept)
