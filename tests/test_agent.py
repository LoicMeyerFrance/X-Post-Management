"""Tests for the assistant: the MCP server, the CLI bridge and the API routes.

No model is ever called and no tweet is ever sent. The MCP server is exercised
both in-process and as a real child process speaking JSON-RPC over a pipe to a
live Flask server, which is the path that actually runs in production.

    python tests/test_agent.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

TEST_HOME = tempfile.mkdtemp(prefix='xpm-agent-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, 'server')
sys.path.insert(0, SERVER_DIR)

import agent                # noqa: E402
import app as appmod        # noqa: E402
import config               # noqa: E402
import database             # noqa: E402
import mcp_server           # noqa: E402

# Never touch the real credential store: the service name is all that separates
# this run from the user's own saved password and key.
config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'

LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


# --- MCP protocol, in process ---------------------------------------------

def test_mcp_handshake():
    section('MCP handshake')

    reply = mcp_server.dispatch({
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18',
                   'clientInfo': {'name': 'test', 'version': '1'}},
    })
    result = reply.get('result', {})
    check('initialize answers', reply.get('id') == 1 and 'result' in reply, reply)
    check('echoes a protocol version it knows',
          result.get('protocolVersion') == '2025-06-18', result.get('protocolVersion'))
    check('declares the tools capability',
          'tools' in result.get('capabilities', {}), result.get('capabilities'))
    check('names itself', result.get('serverInfo', {}).get('name') == 'xpost',
          result.get('serverInfo'))
    check('ships instructions for the model', bool(result.get('instructions')))

    # An unknown version must come back as one we do support, not as an error.
    reply = mcp_server.dispatch({
        'jsonrpc': '2.0', 'id': 2, 'method': 'initialize',
        'params': {'protocolVersion': '1999-01-01'},
    })
    version = reply.get('result', {}).get('protocolVersion')
    check('falls back to a supported version for an unknown one',
          version in mcp_server.SUPPORTED_PROTOCOLS, version)

    check('notifications get no reply',
          mcp_server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None)
    check('ping answers empty',
          mcp_server.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}).get('result') == {})

    reply = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 4, 'method': 'nope/nope'})
    check('unknown method is a protocol error',
          reply.get('error', {}).get('code') == mcp_server.METHOD_NOT_FOUND, reply)

    reply = mcp_server.dispatch({'id': 5, 'method': 'initialize'})
    check('a message without jsonrpc:2.0 is rejected',
          reply.get('error', {}).get('code') == mcp_server.INVALID_REQUEST, reply)

    # An unknown *notification* must stay silent rather than answer an error.
    check('unknown notification stays silent',
          mcp_server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/whatever'}) is None)


def test_mcp_tool_gating():
    section('MCP publish gating')

    os.environ.pop('XPM_AGENT_ALLOW_PUBLISH', None)
    names = [t['name'] for t in mcp_server.visible_tools()]
    check('manual mode hides publish_now', 'publish_now' not in names, names)
    check('manual mode hides schedule_on_x', 'schedule_on_x' not in names)
    check('manual mode still offers create_post', 'create_post' in names)
    check('manual mode still offers list_posts', 'list_posts' in names)

    # Hiding is a hint; calling it anyway must still be refused.
    result = mcp_server.handle_tools_call({'name': 'publish_now', 'arguments': {'id': 1}})
    check('publish_now refused when hidden', result.get('isError') is True, result)
    check('refusal explains what to do instead',
          'draft' in result['content'][0]['text'].lower(), result['content'][0]['text'][:80])

    os.environ['XPM_AGENT_ALLOW_PUBLISH'] = '1'
    names = [t['name'] for t in mcp_server.visible_tools()]
    check('auto mode exposes publish_now', 'publish_now' in names, names)
    check('auto mode exposes schedule_on_x', 'schedule_on_x' in names)
    os.environ.pop('XPM_AGENT_ALLOW_PUBLISH', None)

    reply = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/list'})
    tools = reply.get('result', {}).get('tools', [])
    check('tools/list returns the catalogue', len(tools) >= 5, len(tools))
    check('no internal keys leak into the wire format',
          all(set(t) <= {'name', 'title', 'description', 'inputSchema'} for t in tools),
          [sorted(t) for t in tools][:1])
    check('every tool has an object input schema',
          all(t['inputSchema'].get('type') == 'object' for t in tools))
    check('every tool is described', all(len(t.get('description', '')) > 20 for t in tools))

    reply = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                                 'params': {'name': 'does_not_exist', 'arguments': {}}})
    check('unknown tool is a protocol error',
          reply.get('error', {}).get('code') == mcp_server.INVALID_PARAMS, reply)


def test_mcp_argument_validation():
    section('MCP argument validation')

    # No HTTP call should happen for these: they fail before the request.
    result = mcp_server.handle_tools_call({'name': 'get_post', 'arguments': {}})
    check('missing id reported as a tool error', result.get('isError') is True, result)

    result = mcp_server.handle_tools_call({'name': 'get_post', 'arguments': {'id': 'abc'}})
    check('non-numeric id reported as a tool error', result.get('isError') is True, result)

    result = mcp_server.handle_tools_call({'name': 'create_post', 'arguments': {}})
    check('create_post with nothing refused', result.get('isError') is True, result)

    result = mcp_server.handle_tools_call(
        {'name': 'create_post', 'arguments': {'text': 'hi', 'status': 'posted'}})
    check('create_post refuses a status the agent may not set',
          result.get('isError') is True, result)

    result = mcp_server.handle_tools_call(
        {'name': 'create_post', 'arguments': {'text': 'hi', 'media_path': '/nope/zzz.png'}})
    check('create_post refuses a missing file', result.get('isError') is True, result)

    result = mcp_server.handle_tools_call({'name': 'update_post', 'arguments': {'id': 1}})
    check('update_post with no fields refused', result.get('isError') is True, result)

    result = mcp_server.handle_tools_call({'name': 'list_posts', 'arguments': 'nope'})
    check('non-object arguments refused', result.get('isError') is True, result)


def test_create_post_note_matches_the_mode():
    """What create_post says about publishing has to match what actually happens.

    The note used to claim nothing was public until published, in every mode. In
    automatic mode the app publishes immediately, so the agent believed the tool,
    called publish_now on a post already on its way, and got a 409 it then had to
    explain to the user.
    """
    section('what create_post tells the agent')

    # Exercise the real function; only the HTTP call is stubbed out.
    real_request = mcp_server._request
    mcp_server._request = lambda *a, **k: {'id': 7, 'status': 'draft'}
    saved = os.environ.get('XPM_AGENT_ALLOW_PUBLISH')
    try:
        os.environ.pop('XPM_AGENT_ALLOW_PUBLISH', None)
        manual = mcp_server.tool_create_post({'text': 'hello'})['note']
        os.environ['XPM_AGENT_ALLOW_PUBLISH'] = '1'
        auto = mcp_server.tool_create_post({'text': 'hello'})['note']
    finally:
        mcp_server._request = real_request
        if saved is None:
            os.environ.pop('XPM_AGENT_ALLOW_PUBLISH', None)
        else:
            os.environ['XPM_AGENT_ALLOW_PUBLISH'] = saved

    check('manual mode says the user approves it', 'approves it' in manual, manual)
    check('manual mode does not claim it was sent',
          'publishes it by itself' not in manual, manual)
    check('auto mode says the app sends it', 'publishes it by itself' in auto, auto)
    check('auto mode tells it not to publish again',
          'Do NOT call publish_now' in auto, auto)
    check('the two notes differ', auto != manual)
    # Whichever mode, the note must not contradict itself.
    check('auto mode never says nothing is public',
          'Nothing is public' not in auto, auto)


def test_mcp_multipart():
    section('MCP multipart encoding')

    path = os.path.join(TEST_HOME, 'sample.png')
    with open(path, 'wb') as handle:
        handle.write(b'\x89PNG\r\n\x1a\n' + b'x' * 32)

    body, content_type = mcp_server._multipart({'text': 'hello', 'status': 'draft'},
                                               'image', path)
    check('content type carries a boundary', 'boundary=' in content_type, content_type)
    boundary = content_type.split('boundary=')[1]
    check('body closes with the final boundary',
          body.endswith(f'--{boundary}--\r\n'.encode()), body[-40:])
    check('text field present', b'name="text"' in body)
    check('file field carries a filename', b'filename="sample.png"' in body)
    check('file bytes are included', b'\x89PNG' in body)
    check('png detected as image/png', b'Content-Type: image/png' in body, content_type)

    # A None value must be skipped rather than written as "None".
    body, _ = mcp_server._multipart({'text': 'x', 'scheduled_at': None})
    check('None fields are omitted', b'scheduled_at' not in body)


# --- the CLI bridge -------------------------------------------------------

def test_agent_command():
    section('CLI command building')

    fake_cli = os.path.join(TEST_HOME, 'claude-fake')
    with open(fake_cli, 'w', encoding='utf-8') as handle:
        handle.write('#!/bin/sh\n')

    # No key stored -> subscription path, which must NOT use --bare.
    agent.set_api_key('')
    argv = agent.build_command('hello', cli_path=fake_cli, version=(2, 1, 283))
    check('prompt passed with -p', '-p' in argv and 'hello' in argv, argv[:4])
    check('streams newline-delimited json',
          argv[argv.index('--output-format') + 1] == 'stream-json')
    check('asks for partial messages', '--include-partial-messages' in argv)
    check('subscription run does not pass --bare', '--bare' not in argv, argv)
    check('mcp config attached', '--mcp-config' in argv)
    check('system prompt appended', '--append-system-prompt' in argv)
    check('manual mode sets no auto permission mode', '--permission-mode' not in argv, argv)
    prompt = argv[argv.index('--append-system-prompt') + 1]
    check('manual mode tells the agent nothing is public',
          'Nothing you create is public' in prompt, prompt[-120:])
    check('manual mode points at the tick in the conversation',
          'tick and a cross' in prompt, prompt[-120:])

    allowed = argv[argv.index('--allowedTools') + 1].split(',')
    check('tool names use the mcp__server__tool form',
          all(name.startswith('mcp__xpost__') for name in allowed), allowed)
    check('manual mode does not pre-approve publish_now',
          'mcp__xpost__publish_now' not in allowed, allowed)
    check('manual mode pre-approves create_post', 'mcp__xpost__create_post' in allowed)

    # Confinement. --allowedTools only pre-approves, so without these the session
    # would still carry Bash, Read, WebFetch and friends - and the read-only Bash
    # set runs with no prompt in every permission mode.
    check('every built-in tool is dropped',
          argv[argv.index('--tools') + 1] == '', argv[argv.index('--tools') + 1])
    check('other MCP configurations are ignored', '--strict-mcp-config' in argv, argv)
    denied = argv[argv.index('--disallowedTools') + 1].split(',')
    check('manual mode denies publish_now outright',
          'mcp__xpost__publish_now' in denied, denied)
    check('manual mode denies schedule_on_x outright',
          'mcp__xpost__schedule_on_x' in denied, denied)
    check('the deny list never covers a tool the agent needs',
          not any(name in denied for name in
                  (f'mcp__xpost__{t}' for t in agent.DRAFT_TOOLS)), denied)

    cfg = json.loads(argv[argv.index('--mcp-config') + 1])
    server = cfg['mcpServers']['xpost']
    check('mcp config names a stdio command', bool(server.get('command')), server)
    check('mcp config has no url (stdio, not http)', 'url' not in server, server)
    check('manual mode does not grant publishing to the mcp child',
          'XPM_AGENT_ALLOW_PUBLISH' not in server.get('env', {}), server.get('env'))
    check('mcp child is told where the api lives',
          server['env'].get('XPM_API_BASE', '').startswith('http://127.0.0.1'), server['env'])
    check('mcp child inherits the test home',
          server['env'].get('XPM_HOME') == TEST_HOME, server['env'].get('XPM_HOME'))
    # The path must point at the code, not at the data directory: XPM_HOME moves
    # the latter, and a config built from it launches a child that cannot import
    # its own module. The CLI reports that only as "Connection closed".
    check('mcp module is importable from the configured path',
          os.path.isfile(os.path.join(server['env'].get('PYTHONPATH', ''), 'mcp_server.py')),
          server['env'].get('PYTHONPATH'))

    # Auto mode.
    argv = agent.build_command('hi', auto=True, cli_path=fake_cli, version=(2, 1, 283))
    check('auto mode sets permission-mode auto',
          argv[argv.index('--permission-mode') + 1] == 'auto', argv)
    allowed = argv[argv.index('--allowedTools') + 1].split(',')
    check('auto mode pre-approves publish_now', 'mcp__xpost__publish_now' in allowed)
    cfg = json.loads(argv[argv.index('--mcp-config') + 1])
    check('auto mode grants publishing to the mcp child',
          cfg['mcpServers']['xpost']['env'].get('XPM_AGENT_ALLOW_PUBLISH') == '1')
    check('auto mode does not deny the publish tools',
          '--disallowedTools' not in argv, argv)
    # Auto mode is about publishing, not about handing over the machine.
    check('auto mode still drops every built-in tool',
          argv[argv.index('--tools') + 1] == '', argv[argv.index('--tools') + 1])
    check('auto mode still ignores other MCP configurations',
          '--strict-mcp-config' in argv, argv)
    prompt = argv[argv.index('--append-system-prompt') + 1]
    check('auto mode tells the agent posts go out immediately',
          'goes out to X immediately' in prompt, prompt[-140:])
    check('auto mode does not talk about awaiting approval',
          'waiting for them' not in prompt, prompt[-140:])
    # The app sends what the agent creates. Reaching for publish_now as well only
    # races with it, and the loser gets a 409 it then has to explain.
    check('auto mode tells it not to publish what it just created',
          'publish_now or schedule_on_x for a post you just created' in prompt,
          prompt[-260:])
    check('and says when those tools are for',
          'already existed before this request' in prompt, prompt[-260:])

    # Version gating for --permission-prompts.
    argv = agent.build_command('hi', cli_path=fake_cli, version=(2, 1, 258))
    check('old CLI is not given --permission-prompts', '--permission-prompts' not in argv)
    argv = agent.build_command('hi', cli_path=fake_cli, version=(2, 1, 259))
    check('new CLI is given --permission-prompts none',
          argv[argv.index('--permission-prompts') + 1] == 'none')
    argv = agent.build_command('hi', cli_path=fake_cli, version=None)
    check('unknown version plays it safe', '--permission-prompts' not in argv)

    # Session continuity.
    argv = agent.build_command('hi', session_id='abc-123', cli_path=fake_cli, version=(2, 1, 283))
    check('resumes a known session', argv[argv.index('--resume') + 1] == 'abc-123')

    # An over-long message is refused before a process is started.
    try:
        agent.build_command('x' * (agent.MAX_PROMPT_CHARS + 1), cli_path=fake_cli)
        check('over-long prompt refused', False, 'no error raised')
    except agent.AgentError as exc:
        check('over-long prompt refused', 'too long' in str(exc).lower(), str(exc))

    # With no CLI anywhere, the run must fail with an actionable message rather
    # than a FileNotFoundError from Popen. This machine probably has a real CLI,
    # so pretend it does not.
    real_find = agent.find_cli
    agent.find_cli = lambda: ''
    try:
        agent.build_command('hi')
        check('missing CLI refused', False, 'no error raised')
    except agent.AgentError as exc:
        message = str(exc).lower()
        check('missing CLI refused', 'not installed' in message, str(exc))
        check('missing CLI message says how to install it', 'npm install' in message, str(exc))
    finally:
        agent.find_cli = real_find


def test_agent_key_handling():
    section('API key handling')

    stored = agent.set_api_key('sk-ant-test-KEY-value')
    if not stored:
        check('credential store available (skipped: none on this machine)', True)
        return

    check('key stored', agent.get_api_key() == 'sk-ant-test-KEY-value')
    check('has_api_key true', agent.has_api_key())
    check('auth mode switches to the key', agent.auth_mode() == 'api_key')

    env = agent.child_env('http://127.0.0.1:5000')
    check('key injected into the child environment',
          env.get('ANTHROPIC_API_KEY') == 'sk-ant-test-KEY-value')
    check('key kept out of this process environment',
          os.environ.get('ANTHROPIC_API_KEY') is None, os.environ.get('ANTHROPIC_API_KEY'))

    fake_cli = os.path.join(TEST_HOME, 'claude-fake')
    argv = agent.build_command('hi', cli_path=fake_cli, version=(2, 1, 283))
    check('a stored key switches the run to --bare', '--bare' in argv, argv)

    agent.set_api_key('')
    check('key cleared', agent.get_api_key() == '')
    env = agent.child_env('http://127.0.0.1:5000')
    check('cleared key is not passed to the child', 'ANTHROPIC_API_KEY' not in env)


def test_web_access():
    """Looking things up is opt-in, and grants the web tools only."""
    section('web access')

    fake_cli = os.path.join(TEST_HOME, 'claude-fake')
    agent.set_api_key('')

    # Off: no built-in tool of any kind.
    argv = agent.build_command('hi', cli_path=fake_cli, version=(2, 1, 283))
    check('off by default: every built-in is dropped',
          argv[argv.index('--tools') + 1] == '', argv[argv.index('--tools') + 1])
    allowed = argv[argv.index('--allowedTools') + 1].split(',')
    check('no web tool is pre-approved',
          not any(name in ('WebSearch', 'WebFetch') for name in allowed), allowed)
    prompt = argv[argv.index('--append-system-prompt') + 1]
    check('the agent is told it has no web access',
          'no web access' in prompt, prompt[-160:])
    check('and told not to assert unverified facts',
          'as if you had checked it' in prompt, prompt[-160:])

    # On: the two web tools, and still nothing else.
    argv = agent.build_command('hi', cli_path=fake_cli, version=(2, 1, 283),
                               web_access=True)
    tools = argv[argv.index('--tools') + 1].split(',')
    check('on: the web tools are available', sorted(tools) == ['WebFetch', 'WebSearch'],
          tools)
    for forbidden in ('Bash', 'Read', 'Write', 'Edit', 'Task', 'Glob', 'Grep'):
        check(f'{forbidden} is still not granted', forbidden not in tools, tools)

    allowed = argv[argv.index('--allowedTools') + 1].split(',')
    # Both need permission by default; available but unapproved would be denied.
    check('WebSearch is pre-approved', 'WebSearch' in allowed, allowed)
    check('WebFetch is pre-approved', 'WebFetch' in allowed, allowed)
    check('the app tools are still there', 'mcp__xpost__create_post' in allowed, allowed)
    check('publishing is still not granted in manual mode',
          'mcp__xpost__publish_now' not in allowed, allowed)

    prompt = argv[argv.index('--append-system-prompt') + 1]
    check('the agent is told to check before writing',
          'check a claim before it goes into a post' in prompt, prompt[-200:])
    check('and to say when a search settles nothing',
          'settles nothing' in prompt, prompt[-200:])
    check('it is still told it has no shell or files',
          'no shell, no file access' in prompt, prompt[-200:])

    # The two switches are independent.
    argv = agent.build_command('hi', auto=True, cli_path=fake_cli, version=(2, 1, 283))
    check('auto mode alone does not grant the web',
          argv[argv.index('--tools') + 1] == '', argv[argv.index('--tools') + 1])
    argv = agent.build_command('hi', auto=False, cli_path=fake_cli,
                               version=(2, 1, 283), web_access=True)
    check('web access alone does not grant publishing',
          '--permission-mode' not in argv, argv)
    check('and still denies the publish tools outright',
          'mcp__xpost__publish_now' in argv[argv.index('--disallowedTools') + 1])


def test_setup_flow():
    """Installing and signing in without the user opening a terminal."""
    section('guided setup')

    argv, shown = agent.install_command()
    check('the install command is the official one-liner',
          'claude.ai/install' in shown, shown)
    check('no npm and no node in it',
          'npm' not in shown and 'node' not in shown.lower(), shown)
    check('argv runs it through a real shell interpreter',
          argv[0] in ('powershell', 'bash'), argv)
    check('the displayed command matches what will run',
          shown.split()[-1] in ' '.join(argv) or shown in ' '.join(argv), (shown, argv))

    # auth_status must degrade rather than raise when there is no CLI.
    real_find = agent.find_cli
    agent.find_cli = lambda: ''
    try:
        auth = agent.auth_status()
        check('no CLI means the auth state is unknown, not a crash',
              auth == {'known': False, 'logged_in': False}, auth)
        ok, detail = agent.start_login()
        check('signing in without a CLI is refused', ok is False, detail)
        check('and says why', 'not installed' in detail.lower(), detail)
    finally:
        agent.find_cli = real_find

    # A CLI that answers with nonsense must not be read as signed in.
    real_run = agent.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ''
            self.returncode = 0

    agent.find_cli = lambda: os.path.join(TEST_HOME, 'claude-fake')
    try:
        agent.subprocess.run = lambda *a, **k: FakeCompleted('not json at all')
        check('unparseable auth output is not treated as signed in',
              agent.auth_status()['logged_in'] is False)
        agent.subprocess.run = lambda *a, **k: FakeCompleted(
            '{"loggedIn": true, "authMethod": "claude.ai", '
            '"subscriptionType": "max", "email": "someone@example.com", '
            '"orgId": "should-not-be-exposed"}')
        auth = agent.auth_status()
        check('a signed-in CLI is reported', auth['logged_in'] is True, auth)
        check('with its plan', auth['plan'] == 'max', auth)
        check('the org id is not passed on', 'orgId' not in auth, auth)

        agent.subprocess.run = lambda *a, **k: FakeCompleted('{"loggedIn": false}')
        check('a signed-out CLI is reported', agent.auth_status()['logged_in'] is False)

        # "ready" has to mean a turn would actually run.
        agent.set_api_key('')
        agent.subprocess.run = lambda *a, **k: FakeCompleted('{"loggedIn": false}')
        state = agent.status()
        check('installed but signed out is not ready', state['ready'] is False, state['ready'])
        check('and the interface is told the binary is there',
              state['cli_installed'] is True)
        check('the install command is offered to the interface',
              'claude.ai/install' in state['install_command'], state['install_command'])

        agent.subprocess.run = lambda *a, **k: FakeCompleted('{"loggedIn": true}')
        check('installed and signed in is ready', agent.status()['ready'] is True)

        # A stored API key makes the sign-in unnecessary.
        agent.subprocess.run = lambda *a, **k: FakeCompleted('{"loggedIn": false}')
        if agent.set_api_key('sk-ant-ready-check'):
            check('a key alone also makes it ready', agent.status()['ready'] is True)
            agent.set_api_key('')
        else:
            check('a key alone also makes it ready (no credential store)', True)
    finally:
        agent.subprocess.run = real_run
        agent.find_cli = real_find


def test_session_store():
    section('conversation continuity')

    agent.clear_session()
    check('no session to begin with', agent.load_session_id() == '')
    agent.save_session_id('sess-1')
    check('session id persisted', agent.load_session_id() == 'sess-1')
    agent.save_session_id('')
    check('empty id does not wipe the stored one', agent.load_session_id() == 'sess-1')
    agent.clear_session()
    check('session cleared', agent.load_session_id() == '')


def test_failure_messages():
    section('failure explanations')

    agent.set_api_key('')
    check('stop reported plainly', agent._explain_failure(143, '') == 'Stopped.')
    check('not-signed-in explained',
          'sign in' in agent._explain_failure(1, 'Error: not logged in').lower())
    check('unknown option explained',
          'update' in agent._explain_failure(1, 'error: unknown option --bare').lower())
    check('silent failure still says something',
          'exited with code' in agent._explain_failure(2, ''))


# --- the HTTP routes ------------------------------------------------------

def test_agent_routes(client):
    section('assistant API routes')

    r = client.get('/api/agent/status', headers=LOCAL)
    check('status returns 200', r.status_code == 200, r.status_code)
    body = r.get_json()
    for field in ('cli_installed', 'auth_mode', 'auto_approve', 'running', 'install_hint'):
        check(f'status reports {field}', field in body, sorted(body))

    # The status endpoint reports *whether* a key exists, never its value.
    sentinel = 'sk-ant-sentinel-do-not-echo'
    if agent.set_api_key(sentinel):
        dump = json.dumps(client.get('/api/agent/status', headers=LOCAL).get_json())
        check('status never echoes the stored key', sentinel not in dump, dump)
        check('status still reports that a key is set',
              client.get('/api/agent/status', headers=LOCAL).get_json()['has_api_key'] is True)
        agent.set_api_key('')
    else:
        check('status never echoes the stored key (no credential store)', True)

    r = client.post('/api/agent/auto', json={'auto_approve': True}, headers=LOCAL)
    check('auto mode can be turned on', r.get_json().get('auto_approve') is True, r.get_json())
    check('auto mode persisted server-side', appmod.agent_auto_approve() is True)

    r = client.post('/api/agent/auto', json={'auto_approve': False}, headers=LOCAL)
    check('auto mode can be turned off', r.get_json().get('auto_approve') is False)
    check('manual is the stored state again', appmod.agent_auto_approve() is False)

    r = client.post('/api/agent/auto', json={}, headers=LOCAL)
    check('auto endpoint rejects a payload without the flag', r.status_code == 400, r.status_code)

    # On unless turned off: an install that never expressed a choice gets it.
    import json as _json
    prefs_path = appmod.PREFERENCES_PATH
    saved = appmod.read_json_file(prefs_path)
    stripped = {k: v for k, v in saved.items() if k != appmod.AGENT_WEB_KEY}
    appmod.write_json_file(prefs_path, stripped)
    check('web access is allowed by default', appmod.agent_web_access() is True)
    check('and the status says so',
          client.get('/api/agent/status', headers=LOCAL).get_json().get('web_access') is True)
    appmod.write_json_file(prefs_path, {**stripped, appmod.AGENT_WEB_KEY: 'false'})
    check('an explicit refusal is honoured', appmod.agent_web_access() is False)
    appmod.write_json_file(prefs_path, saved)
    del _json

    r = client.post('/api/agent/web', json={'web_access': True}, headers=LOCAL)
    check('web access can be allowed', r.get_json().get('web_access') is True, r.get_json())
    check('and is stored server-side', appmod.agent_web_access() is True)
    check('status reports it',
          client.get('/api/agent/status', headers=LOCAL).get_json().get('web_access') is True)
    r = client.post('/api/agent/web', json={'web_access': False}, headers=LOCAL)
    check('web access can be refused again', r.get_json().get('web_access') is False)
    check('and that is the stored state', appmod.agent_web_access() is False)
    r = client.post('/api/agent/web', json={}, headers=LOCAL)
    check('web endpoint rejects a payload without the flag', r.status_code == 400, r.status_code)
    r = client.post('/api/agent/web', json={'web_access': True},
                    headers={'Host': '127.0.0.1:5000', 'Origin': 'https://evil.com'})
    check('cross-origin cannot grant web access', r.status_code == 403, r.status_code)

    # The child is told where to call back. Hardcoding the default port breaks
    # every install where 5000 was busy and pick_port() chose something else.
    with appmod.app.test_request_context('/api/agent/chat', headers={'Host': '127.0.0.1:61234'}):
        base = appmod._api_base()
    check('the mcp child is told the port actually serving the request',
          base == 'http://127.0.0.1:61234', base)
    with appmod.app.test_request_context('/api/agent/chat', headers={'Host': 'localhost:7000'}):
        base = appmod._api_base()
    check('localhost is accepted as a callback host',
          base == 'http://localhost:7000', base)
    with appmod.app.test_request_context('/api/agent/chat', headers={'Host': 'evil.com'}):
        base = appmod._api_base()
    check('a non-loopback Host never becomes the callback url',
          base == f'http://127.0.0.1:{appmod.DEFAULT_PORT}', base)

    r = client.post('/api/agent/chat', json={'message': ''}, headers=LOCAL)
    check('empty message refused', r.status_code == 400, r.status_code)

    r = client.post('/api/agent/chat', json={}, headers=LOCAL)
    check('missing message refused', r.status_code == 400, r.status_code)

    r = client.post('/api/agent/stop', headers=LOCAL)
    check('stop works with nothing running',
          r.status_code == 200 and r.get_json() == {'stopped': False}, r.get_json())

    r = client.post('/api/agent/reset', headers=LOCAL)
    check('reset works', r.status_code == 200 and r.get_json().get('reset') is True)

    r = client.post('/api/agent/key', json={'api_key': 'x' * 500}, headers=LOCAL)
    check('absurdly long key refused', r.status_code == 400, r.status_code)

    r = client.post('/api/agent/key', json={'api_key': 'bad\nkey'}, headers=LOCAL)
    check('key with a newline refused', r.status_code == 400, r.status_code)

    r = client.post('/api/agent/key', json='nope', headers=LOCAL)
    check('non-object payload refused', r.status_code == 400, r.status_code)

    # The security guards must cover the new routes too.
    r = client.get('/api/agent/status', headers={'Host': 'evil.com'})
    check('foreign Host blocked on the agent routes', r.status_code == 403, r.status_code)
    r = client.post('/api/agent/chat', headers={'Host': '127.0.0.1:5000',
                                                'Origin': 'https://evil.com'},
                    json={'message': 'publish something'})
    check('cross-origin chat blocked', r.status_code == 403, r.status_code)
    r = client.post('/api/agent/key', headers={**LOCAL, 'Sec-Fetch-Site': 'cross-site'},
                    json={'api_key': 'sk-ant-evil'})
    check('cross-site key write blocked', r.status_code == 403, r.status_code)


# --- the real child process, against a live server ------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _rpc(process, message):
    process.stdin.write(json.dumps(message) + '\n')
    process.stdin.flush()
    line = process.stdout.readline()
    return json.loads(line) if line.strip() else None


def test_mcp_subprocess_live():
    """The path that actually runs: a child process, a pipe, and real HTTP."""
    section('MCP server as a child process against a live API')

    port = _free_port()
    base = f'http://127.0.0.1:{port}'

    server = threading.Thread(
        target=lambda: appmod.app.run(host='127.0.0.1', port=port, debug=False,
                                      use_reloader=False, threaded=True),
        daemon=True)
    server.start()

    # Wait for it to answer rather than sleeping a fixed amount.
    import urllib.error
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(f'{base}/api/health', timeout=1).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    else:
        check('live server started', False, 'never answered /api/health')
        return
    check('live server started', True)

    env = dict(os.environ)
    env['XPM_API_BASE'] = base
    env['XPM_HOME'] = TEST_HOME
    env['PYTHONPATH'] = SERVER_DIR
    env.pop('XPM_AGENT_ALLOW_PUBLISH', None)

    process = subprocess.Popen([sys.executable, '-m', 'mcp_server'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, text=True,
                               encoding='utf-8', bufsize=1)
    try:
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '2025-06-18',
                                          'clientInfo': {'name': 'test', 'version': '1'}}})
        check('child answers initialize', reply and reply.get('id') == 1, reply)

        process.stdin.write(json.dumps({'jsonrpc': '2.0',
                                        'method': 'notifications/initialized'}) + '\n')
        process.stdin.flush()

        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        names = [t['name'] for t in reply['result']['tools']]
        check('child lists tools over the pipe', 'create_post' in names, names)
        check('child hides publishing in manual mode', 'publish_now' not in names, names)

        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                               'params': {'name': 'get_limits', 'arguments': {}}})
        result = reply['result']
        check('get_limits reached the live API over HTTP', result.get('isError') is False, result)
        payload = result.get('structuredContent', {})
        check('get_limits reports the character limit',
              payload.get('max_characters') in (280, 25000), payload)
        check('get_limits says publishing is off', payload.get('publishing_allowed') is False)

        # Create a post through the agent's own tool, then read it back.
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                               'params': {'name': 'create_post',
                                          'arguments': {'text': 'from the agent',
                                                        'scheduled_at': '2030-01-01T10:00'}}})
        result = reply['result']
        check('create_post succeeded end to end', result.get('isError') is False, result)
        new_id = result.get('structuredContent', {}).get('id')
        check('create_post returned an id', isinstance(new_id, int), result)

        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                               'params': {'name': 'list_posts',
                                          'arguments': {'status': 'scheduled'}}})
        payload = reply['result'].get('structuredContent', {})
        listed = payload.get('posts', [])
        ids = [p['id'] for p in listed]
        check('the new post shows up in the calendar view', new_id in ids, ids)
        check('scheduled time round-tripped',
              any(p['id'] == new_id and p['scheduled_at'] for p in listed), payload)

        # A media upload through the child, with real bytes.
        png = os.path.join(TEST_HOME, 'agent-media.png')
        with open(png, 'wb') as handle:
            handle.write(b'\x89PNG\r\n\x1a\n' + b'y' * 64)
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
                               'params': {'name': 'create_post',
                                          'arguments': {'text': 'with media',
                                                        'media_path': png}}})
        result = reply['result']
        check('create_post with an image succeeded', result.get('isError') is False, result)
        media_id = result.get('structuredContent', {}).get('id')
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                               'params': {'name': 'get_post', 'arguments': {'id': media_id}}})
        check('the stored post kept its media',
              bool(reply['result']['structuredContent'].get('media')),
              reply['result']['structuredContent'])

        # Video, which is the whole reason media_path takes a path rather than
        # base64: a 512MB file has no business going through a JSON payload.
        # ISO-BMFF: the 'ftyp' box sits at offset 4, which is what the API checks.
        mp4 = os.path.join(TEST_HOME, 'clip.mp4')
        with open(mp4, 'wb') as handle:
            handle.write(b'\x00\x00\x00\x20ftypisom' + b'\x00' * 256)
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 20, 'method': 'tools/call',
                               'params': {'name': 'create_post',
                                          'arguments': {'text': 'a clip',
                                                        'media_path': mp4,
                                                        'scheduled_at': '2030-02-02T08:00'}}})
        result = reply['result']
        check('create_post accepts a video', result.get('isError') is False, result)
        video_id = result.get('structuredContent', {}).get('id')
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 21, 'method': 'tools/call',
                               'params': {'name': 'get_post', 'arguments': {'id': video_id}}})
        stored = reply['result']['structuredContent']
        check('the video is stored with its extension',
              str(stored.get('media', '')).endswith('.mp4'), stored)
        check('the video post kept its schedule', bool(stored.get('scheduled_at')), stored)

        # A .mp4 whose bytes are not a video must be refused, as for images.
        fake_video = os.path.join(TEST_HOME, 'fake.mp4')
        with open(fake_video, 'wb') as handle:
            handle.write(b'definitely not an mp4 file at all')
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 22, 'method': 'tools/call',
                               'params': {'name': 'create_post',
                                          'arguments': {'text': 'nope',
                                                        'media_path': fake_video}}})
        check('a fake video is refused', reply['result'].get('isError') is True,
              reply['result'])

        # A file that is not really an image must be refused by the API, and the
        # refusal must reach the agent as a tool error rather than a crash.
        fake = os.path.join(TEST_HOME, 'not-really.png')
        with open(fake, 'wb') as handle:
            handle.write(b'this is not a png')
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
                               'params': {'name': 'create_post',
                                          'arguments': {'text': 'nope', 'media_path': fake}}})
        check('a disguised file is refused through the whole chain',
              reply['result'].get('isError') is True, reply['result'])

        # Publishing must be refused by the child, not just hidden.
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                               'params': {'name': 'publish_now',
                                          'arguments': {'id': new_id}}})
        check('the child refuses publish_now in manual mode',
              reply['result'].get('isError') is True, reply['result'])

        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                               'params': {'name': 'delete_post', 'arguments': {'id': new_id}}})
        check('delete_post works over the pipe', reply['result'].get('isError') is False,
              reply['result'])

        # Malformed input on the wire must not kill the loop.
        process.stdin.write('this is not json\n')
        process.stdin.flush()
        line = process.stdout.readline()
        parsed = json.loads(line) if line.strip() else {}
        check('bad JSON answered with a parse error',
              parsed.get('error', {}).get('code') == mcp_server.PARSE_ERROR, parsed)
        reply = _rpc(process, {'jsonrpc': '2.0', 'id': 11, 'method': 'ping'})
        check('the child survives bad input', reply and reply.get('result') == {}, reply)

        stderr_so_far = ''
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        check('child exits when stdin closes', process.returncode is not None,
              process.returncode)


def test_mcp_offline_behaviour():
    section('MCP server with no app to talk to')

    saved = mcp_server.API_BASE
    mcp_server.API_BASE = f'http://127.0.0.1:{_free_port()}'
    try:
        result = mcp_server.handle_tools_call({'name': 'list_posts', 'arguments': {}})
        check('an unreachable app is a tool error, not a crash',
              result.get('isError') is True, result)
        check('the error names the app',
              'running' in result['content'][0]['text'].lower(),
              result['content'][0]['text'][:120])
    finally:
        mcp_server.API_BASE = saved


def main():
    print('=' * 62)
    print('  Assistant tests (MCP server, CLI bridge, API routes)')
    print('=' * 62)
    print(f'  test home: {TEST_HOME}')

    database.init_db()
    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    test_mcp_handshake()
    test_mcp_tool_gating()
    test_mcp_argument_validation()
    test_create_post_note_matches_the_mode()
    test_mcp_multipart()
    test_agent_command()
    test_agent_key_handling()
    test_web_access()
    test_setup_flow()
    test_session_store()
    test_failure_messages()
    test_agent_routes(client)
    test_mcp_subprocess_live()
    test_mcp_offline_behaviour()

    print('\n' + '=' * 62)
    print(f'  {len(passed)} passed, {len(failed)} failed')
    if failed:
        print('\n  Failures:')
        for name in failed:
            print(f'    - {name}')
    print('=' * 62)
    return 1 if failed else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        config.delete_secret(config.KEYRING_ANTHROPIC_KEY)
        config.delete_password()
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
