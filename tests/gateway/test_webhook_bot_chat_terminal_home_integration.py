"""Routed webhook terminal HOME must survive native Bot Chat FIFO delivery.

Unlike the separate HOME and Bot Chat tests, this composes the real foreground
terminal result with a held native recipient in a multiplexed gateway.
"""
import asyncio
import hashlib
import hmac
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

import aiohttp
import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket
from tests.gateway.test_webhook_bot_chat_integration import WebhookModel, rows
from tests.waiting import async_wait_until


class HomeModel(WebhookModel):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        messages = body.get('messages', [])
        text = next((m.get('content', '') for m in reversed(messages) if m['role'] == 'user'), '')
        message = {'role': 'assistant', 'content': 'RECEIVER_ACK'}
        finish = 'stop'
        if text == 'BLOCK_STARTED':
            self.server.blocked.set()
            self.server.release.wait(180)
            self.server.hold_finished.set()
        elif text in ('SOURCE_ONE', 'SOURCE_TWO'):
            recent = list(reversed(messages))
            last_user = next(i for i, m in enumerate(recent) if m['role'] == 'user')
            tool = next((m for m in recent[:last_user] if m['role'] == 'tool'), None)
            if tool is None:
                command = "printf 'OBSERVED_HOME=%s|HERMES_HOME=%s|HERMES_REAL_HOME=%s|PWD=%s\\n' \"$HOME\" \"$HERMES_HOME\" \"$HERMES_REAL_HOME\" \"$PWD\""
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'home-tool', 'type': 'function', 'function': {'name': 'terminal',
                    'arguments': json.dumps({'command': command, 'background': False})}}]}
                finish = 'tool_calls'
            else:
                result = json.loads(tool['content'])
                # The final is made from the actual tool output, never the expected paths.
                message['content'] = text + ':' + result.get('output', '').strip()
        elif isinstance(text, str) and 'Webhook automation' in text:
            message['content'] = 'RECEIVER_ACK_' + ('ONE' if 'SOURCE_ONE:' in text else 'TWO')
        payload = json.dumps({'id': 'local', 'choices': [{'index': 0, 'message': message,
            'finish_reason': finish}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
            'total_tokens': 15}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            if message.get('tool_calls'):
                message['tool_calls'][0]['index'] = 0
            payload = ('data: ' + json.dumps({'id': 'local', 'choices': [{'index': 0,
                'delta': message, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('mode', ['profile', 'real'])
def test_routed_terminal_home_is_delivered_in_native_fifo(tmp_path, monkeypatch, mode):
    from gateway.webhooks.presentation import result_id
    from tools.bot_live_delivery import read_delivery_result

    root = Path(__file__).resolve().parents[2]
    launch = tmp_path / 'state'
    owner = launch / 'profiles' / 'secondary'
    real, workspace, runtime = (tmp_path / name for name in ('real', 'workspace', 'runtime'))
    owner.mkdir(mode=0o700, parents=True)
    (owner / 'home').mkdir(mode=0o700)
    for directory in (real, workspace, runtime):
        directory.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(real))
    monkeypatch.setenv('HERMES_HOME', str(owner))
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/proc/self/cwd/runtime')
    peer = ThreadingHTTPServer(('127.0.0.1', 0), HomeModel)
    peer.requests = []
    peer.blocked, peer.release, peer.hold_finished = threading.Event(), threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{peer.server_port}/v1'
    model = {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': base}
    auxiliary = {'title_generation': {'enabled': False}}
    cfg = {'model': model, 'gateway': {'multiplex_profiles': True},
        'platforms': {'webhook': {'enabled': True, 'extra': {'host': '127.0.0.1', 'port': port,
            'secret': 'owned-secret', 'routes': {'fixture': {'prompt': '{text}',
            'deliver': 'bot-chat', 'profile': 'secondary'}}}}},
        'auxiliary': auxiliary, 'terminal': {'cwd': str(workspace)},
        'streaming': {'enabled': False}, 'platform_toolsets': {'gui': [], 'webhook': ['terminal']}}
    (launch / 'config.yaml').write_text(json.dumps(cfg))
    (owner / 'config.yaml').write_text(json.dumps({'model': model, 'auxiliary': auxiliary,
        'terminal': {'cwd': str(workspace), 'home_mode': mode},
        'streaming': {'enabled': False}, 'platform_toolsets': {'gui': [], 'webhook': ['terminal']}}))
    env = child_env() | {'HOME': str(real), 'USERPROFILE': str(real), 'HERMES_REAL_HOME': str(real),
        'HERMES_HOME': str(launch), 'TERMINAL_HOME_MODE': 'real' if mode == 'profile' else 'profile',
        'PYTHONPATH': str(root), 'OPENAI_API_KEY': 'loopback-only', 'OPENAI_BASE_URL': base,
        'PYTHONUNBUFFERED': '1', 'XDG_RUNTIME_DIR': '/proc/self/cwd/runtime'}

    async def scenario(desc):
        async with websocket(launch, desc) as default_ws:
            sentinel = await rpc(default_ws, 'session.create', request_id='default-target', source='gui', cwd=str(launch), toolsets=[])
            assert 'result' in sentinel, sentinel
            renamed = await rpc(default_ws, 'session.mutate', session_id=sentinel['result']['session_id'],
                request_id='default-title', expected_revision=sentinel['result']['revision'],
                operation='rename', payload={'title': 'Bot Chat'})
            assert 'result' in renamed, renamed
        async with websocket(owner, desc, control_home=launch) as ws, aiohttp.ClientSession() as client:
            created = await rpc(ws, 'session.create', request_id='target', source='gui', cwd=str(workspace), toolsets=[])
            assert 'result' in created, created
            sid = created['result']['session_id']
            renamed = await rpc(ws, 'session.mutate', session_id=sid, request_id='title',
                expected_revision=created['result']['revision'], operation='rename', payload={'title': 'Bot Chat'})
            assert 'result' in renamed, renamed
            held = await rpc(ws, 'prompt.submit', session_id=sid, input_id='held', text='BLOCK_STARTED')
            assert 'result' in held, held
            assert await asyncio.to_thread(peer.blocked.wait, 30)
            keys, target_rows = [], []
            expected_home = owner / 'home' if mode == 'profile' else real
            observed = f'OBSERVED_HOME={expected_home}|HERMES_HOME={owner}|HERMES_REAL_HOME={real}|PWD={workspace}'
            for event, text in [('event-one', 'SOURCE_ONE'), ('event-two', 'SOURCE_TWO')]:
                body = json.dumps({'text': text}).encode()
                headers = {'X-GitHub-Delivery': event, 'X-GitHub-Event': 'push',
                    'X-Hub-Signature-256': 'sha256=' + hmac.new(b'owned-secret', body, hashlib.sha256).hexdigest()}
                async with client.post(f'http://127.0.0.1:{port}/p/secondary/webhooks/fixture', data=body, headers=headers) as response:
                    assert response.status == 202, await response.text()
                await async_wait_until(lambda: any(r['request_id'] == event and r['status'] == 'terminal' for r in rows(owner)),
                    timeout=40, what=event + ' source completion')
                source, = [r for r in rows(owner) if r['request_id'] == event]
                assert source['outcome'] == 'completed'
                key = result_id(owner, source['admission_id'])
                await async_wait_until(lambda: (read_delivery_result(owner, key) or {}).get('status') == 'queued',
                    timeout=40, what=event + ' queued native receipt')
                queued = read_delivery_result(owner, key)
                assert queued['session_id'] == sid and (text + ':' + observed) in queued['message']
                target, = [r for r in rows(owner) if r['request_id'] == 'bot:' + key]
                assert target['target_session_id'] == sid and target['status'] == 'queued'
                keys.append(key)
                target_rows.append(target)
            assert len(set(keys)) == 2
            assert not peer.hold_finished.is_set()
            assert all((read_delivery_result(owner, key) or {}).get('status') == 'queued' for key in keys)
            assert len({r['admission_id'] for r in target_rows}) == 2
            assert target_rows[0]['seq'] < target_rows[1]['seq']
            assert rows(launch) == [] and sid != sentinel['result']['session_id']
            assert all(read_delivery_result(launch, key) is None for key in keys)
            peer.release.set()
            for key in keys:
                await async_wait_until(lambda: (read_delivery_result(owner, key) or {}).get('status') == 'settled',
                    timeout=40, what='native settlement ' + key)
            receipts = [read_delivery_result(owner, key) for key in keys]
            assert [r['reply'] for r in receipts] == ['RECEIVER_ACK_ONE', 'RECEIVER_ACK_TWO']
            assert all(r['profile_home'] == str(owner) and r['session_id'] == sid for r in receipts)
            settled_rows = [next(r for r in rows(owner) if r['request_id'] == 'bot:' + key) for key in keys]
            assert all(r['status'] == 'terminal' and r['outcome'] == 'completed' for r in settled_rows)
            assert len([r for r in rows(owner) if r['request_id'].startswith('bot:')]) == 2
            inputs = [next((m.get('content', '') for m in reversed(req['messages']) if m['role'] == 'user'), '')
                for req in peer.requests if req.get('messages')]
            delivered = [text for text in inputs if isinstance(text, str) and 'Webhook automation' in text]
            assert len(delivered) == 2
            assert all(label + ':' + observed in text for label, text in zip(('SOURCE_ONE', 'SOURCE_TWO'), delivered))
            print('NATIVE_HOME_BOTCHAT_PROOF ' + json.dumps({
                'mode': mode, 'observed_terminal': observed, 'target_session_id': sid,
                'source_admissions': [r['admission_id'] for r in rows(owner)
                    if r['request_id'] in ('event-one', 'event-two')],
                'native_admissions': [r['admission_id'] for r in settled_rows],
                'native_seq': [r['seq'] for r in settled_rows],
                'delivery_ids': keys, 'states': [r['status'] for r in receipts],
                'replies': [r['reply'] for r in receipts], 'model_delivery_order': ['ONE', 'TWO'],
            }, sort_keys=True))

    try:
        with daemon(tmp_path, launch, env, barrier=False) as (_, desc):
            try:
                asyncio.run(scenario(desc))
            except BaseException:
                print('DIAGNOSTIC ROWS', json.dumps(rows(owner)))
                print('DIAGNOSTIC MODEL', json.dumps(peer.requests))
                print('DIAGNOSTIC LOG', (launch / 'restart.log').read_text())
                raise
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
