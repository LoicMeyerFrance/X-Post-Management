"""Tests for running a chosen agent CLI.

The assistant can be driven by more than one command-line agent. Which one is a
user choice; what it is allowed to do is not. Every provider that can be selected
has to be confined the same way - only this app's tools, no shell, no other MCP
server - and one that cannot be must not be selectable at all.

    python tests/test_providers.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import tomllib

TEST_HOME = tempfile.mkdtemp(prefix='xpm-providers-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, 'server')
sys.path.insert(0, SERVER_DIR)

import agent               # noqa: E402
import app as appmod       # noqa: E402
import config              # noqa: E402
import providers           # noqa: E402

config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'

LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def test_catalogue():
    section('the agents on offer')

    check('Claude Code is offered', providers.get('claude')['available'] is True)
    check('Gemini is offered', providers.get('gemini')['available'] is True)

    # Codex is selectable, but it cannot be confined as far as the others: its
    # shell and file tools are core to it and have no disable switch. That is
    # said next to the choice rather than hidden behind identical wording.
    codex = providers.get('codex')
    check('Codex is selectable', codex['available'] is True)
    check('and carries a caveat', len(codex.get('caveat', '')) > 60, codex.get('caveat'))
    check('the caveat names what it can still do',
          'read files' in codex['caveat'].lower(), codex['caveat'])
    check('the other two carry no caveat',
          not providers.get('claude').get('caveat')
          and not providers.get('gemini').get('caveat'))

    check('an unknown provider falls back to the default',
          providers.normalise('not-a-thing') == providers.DEFAULT_PROVIDER)
    check('Codex is kept, now that it is selectable',
          providers.normalise('codex') == 'codex')
    check('a good one is kept', providers.normalise('gemini') == 'gemini')
    check('the default is Claude Code', providers.DEFAULT_PROVIDER == 'claude')

    for provider_id in ('claude', 'gemini', 'codex'):
        check(f'{provider_id} has an install command',
              bool(providers.install_command(provider_id)), provider_id)


def test_choosing():
    section('choosing one')

    check('Claude Code by default', agent.current_provider() == 'claude')
    check('Gemini can be chosen', agent.set_provider('gemini') == 'gemini')
    check('and is remembered', agent.current_provider() == 'gemini')

    # A conversation belongs to the CLI that held it.
    agent.save_session_id('sess-from-gemini')
    agent.set_provider('claude')
    check('switching clears the conversation', agent.load_session_id() == '',
          agent.load_session_id())

    check('Codex can be chosen too', agent.set_provider('codex') == 'codex')
    agent.set_provider('claude')

    # Re-picking the one already in use is a confirmation, not a switch. It used
    # to be unclickable for exactly that reason, which read as a stuck button;
    # allowing the click is only safe because it no longer ends the conversation.
    agent.save_session_id('sess-keep-me')
    check('re-picking the current agent is accepted',
          agent.set_provider('claude') == 'claude')
    check('and the conversation survives it',
          agent.load_session_id() == 'sess-keep-me', agent.load_session_id())

    check('switching away still ends it', agent.set_provider('gemini') == 'gemini')
    check('as it must', agent.load_session_id() == '', agent.load_session_id())
    agent.set_provider('claude')


def test_gemini_is_confined():
    """The mechanism differs from Claude Code's; the guarantee must not."""
    section('Gemini is confined like Claude Code')

    work = tempfile.mkdtemp(prefix='xpm-gemini-')
    fake = os.path.join(TEST_HOME, 'gemini-fake')
    try:
        argv = agent.build_command('hello', provider='gemini', cli_path=fake,
                                   work_dir=work)
        check('it runs non-interactively', '--prompt' in argv, argv)
        check('and streams structured events',
              argv[argv.index('--output-format') + 1] == 'stream-json', argv)
        check('only this app\'s MCP server is loaded',
              argv[argv.index('--allowed-mcp-server-names') + 1] == 'xpost', argv)

        allowed = argv[argv.index('--allowed-tools') + 1].split(',')
        check('this app\'s tools are approved', 'create_post' in allowed, allowed)
        check('publishing is not, in manual mode', 'publish_now' not in allowed, allowed)
        for forbidden in ('run_shell_command', 'ShellTool', 'read_file', 'write_file'):
            check(f'{forbidden} is not approved', forbidden not in allowed, allowed)

        settings = json.load(open(os.path.join(work, '.gemini', 'settings.json'),
                                  encoding='utf-8'))
        check('built-in tools are disabled', settings['tools']['core'] == [], settings)
        check('the older spelling is written too', settings['coreTools'] == [], settings)
        check('the shell is excluded by name',
              'run_shell_command' in settings['tools']['exclude'], settings)
        servers = settings['mcpServers']
        check('exactly one MCP server is configured', list(servers) == ['xpost'], servers)
        check('and it is trusted, so its tools need no prompt',
              servers['xpost']['trust'] is True, servers)
        check('the server is told where the app is',
              'XPM_API_BASE' in servers['xpost']['env'], servers)
        check('manual mode does not let the child publish',
              'XPM_AGENT_ALLOW_PUBLISH' not in servers['xpost']['env'], servers)

        # Auto mode adds publishing and nothing else.
        argv = agent.build_command('hi', provider='gemini', cli_path=fake,
                                   work_dir=work, auto=True)
        allowed = argv[argv.index('--allowed-tools') + 1].split(',')
        check('auto mode approves publishing', 'publish_now' in allowed, allowed)
        check('auto mode still grants no shell',
              'run_shell_command' not in allowed, allowed)
        settings = json.load(open(os.path.join(work, '.gemini', 'settings.json'),
                                  encoding='utf-8'))
        check('auto mode still disables built-ins', settings['tools']['core'] == [])

        # Web access adds exactly the two read-only tools.
        argv = agent.build_command('hi', provider='gemini', cli_path=fake,
                                   work_dir=work, web_access=True)
        allowed = argv[argv.index('--allowed-tools') + 1].split(',')
        for tool in providers.__dict__.get('GEMINI_WEB_TOOLS', ()) or agent.GEMINI_WEB_TOOLS:
            check(f'{tool} is approved with web access on', tool in allowed, allowed)
        check('and the shell still is not', 'run_shell_command' not in allowed, allowed)

        # The user's own Gemini configuration must not be touched.
        check('the settings file is written inside the app workspace',
              os.path.abspath(os.path.join(work, '.gemini', 'settings.json'))
              .startswith(os.path.abspath(work)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        agent.set_provider('claude')


def test_codex_is_confined_as_far_as_it_goes():
    """Codex cannot be confined like the others; it must still be confined."""
    section('Codex, within its limits')

    work = tempfile.mkdtemp(prefix='xpm-codex-')
    fake = os.path.join(TEST_HOME, 'codex-fake')
    try:
        argv = agent.build_command('hello', provider='codex', cli_path=fake,
                                   work_dir=work)
        check('it runs non-interactively', 'exec' in argv, argv)
        check('and streams structured events', '--json' in argv, argv)
        check('the sandbox is read-only',
              argv[argv.index('--sandbox') + 1] == 'read-only', argv)
        check('nothing dangerous is bypassed',
              not any('dangerous' in a for a in argv), argv)
        check('it runs in the app workspace',
              argv[argv.index('--cd') + 1] == work, argv)

        env = agent.codex_env('http://127.0.0.1:5000', False, work)
        home = env['CODEX_HOME']
        check('it is given its own CODEX_HOME',
              os.path.abspath(home).startswith(os.path.abspath(work)), home)
        check("so the user's own ~/.codex is untouched",
              '.codex-home' in home, home)

        config = open(os.path.join(home, 'config.toml'), encoding='utf-8').read()
        # Without this the MCP calls are cancelled the moment stdin closes.
        check('approvals are never asked for', 'approval_policy = "never"' in config, config)
        check('and that is paired with a read-only sandbox',
              'sandbox_mode = "read-only"' in config, config)
        check('web search is off unless asked for',
              'web_search = false' in config, config)
        check("this app's MCP server is configured",
              '[mcp_servers.xpost]' in config, config)
        check('and required, so a silent failure is not possible',
              'required = true' in config, config)
        check('the server knows where the app is', 'XPM_API_BASE' in config, config)
        check('manual mode does not let it publish',
              'XPM_AGENT_ALLOW_PUBLISH' not in config, config)

        # A Windows path in TOML must survive its own backslashes.
        check('paths are escaped for TOML',
              '\\' in config or '/' in config, config[:200])

        env_auto = agent.codex_env('http://127.0.0.1:5000', True, work)
        config = open(os.path.join(env_auto['CODEX_HOME'], 'config.toml'),
                      encoding='utf-8').read()
        check('auto mode lets the child publish',
              'XPM_AGENT_ALLOW_PUBLISH' in config, config)
        check('and the sandbox is still read-only',
              'sandbox_mode = "read-only"' in config, config)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        agent.set_provider('claude')


def test_codex_translation():
    section('translating Codex output')

    translate = providers.translate_event
    started = translate('codex', {'type': 'thread.started', 'thread_id': 't1'})
    check('a started thread becomes an init', started['subtype'] == 'init', started)

    message = translate('codex', {'type': 'item.completed',
                                  'item': {'type': 'assistant_message', 'text': 'hi'}})
    check('an assistant message comes through',
          message['message']['content'][0]['text'] == 'hi', message)

    call = translate('codex', {'type': 'item.completed',
                               'item': {'type': 'mcp_tool_call', 'id': 'c1',
                                        'tool': 'create_post',
                                        'arguments': '{"text": "x"}'}})
    block = call['message']['content'][0]
    check('a tool call becomes a tool_use block', block['type'] == 'tool_use', block)
    check('with parsed arguments', block['input'] == {'text': 'x'}, block)

    done = translate('codex', {'type': 'item.completed',
                               'item': {'type': 'mcp_tool_call', 'id': 'c1',
                                        'tool': 'create_post', 'output': '{"id": 3}'}})
    check('a completed call becomes a tool_result',
          done['message']['content'][0]['type'] == 'tool_result', done)

    check('a finished turn becomes a result',
          translate('codex', {'type': 'turn.completed'})['type'] == 'result')
    check('a failed turn becomes an error',
          translate('codex', {'type': 'turn.failed', 'message': 'no'})['subtype'] == 'error')
    check('an unknown event is dropped',
          translate('codex', {'type': 'something.else'}) is None)


def test_event_translation():
    """Gemini's events reach the interface in the shape it already reads."""
    section('translating Gemini output')

    translate = providers.translate_event

    check('a Claude event passes through unchanged',
          translate('claude', {'type': 'assistant'}) == {'type': 'assistant'})

    init = translate('gemini', {'type': 'init', 'tools': ['create_post'],
                                'session_id': 'abc'})
    check('init becomes a system init', init['subtype'] == 'init', init)
    check('carrying the tool list', init['tools'] == ['create_post'], init)

    message = translate('gemini', {'type': 'message', 'text': 'hello there'})
    check('a message becomes assistant text',
          message['message']['content'][0]['text'] == 'hello there', message)
    check('an empty message is dropped',
          translate('gemini', {'type': 'message', 'text': '   '}) is None)

    call = translate('gemini', {'type': 'tool_use', 'id': 'c1', 'name': 'create_post',
                                'args': {'text': 'hi'}})
    block = call['message']['content'][0]
    check('a tool call becomes a tool_use block', block['type'] == 'tool_use', block)
    check('with its name', block['name'] == 'create_post', block)
    check('and its arguments', block['input'] == {'text': 'hi'}, block)
    check('and an id the result can be paired with', block['id'] == 'c1', block)

    # Arguments sometimes arrive as a JSON string.
    call = translate('gemini', {'type': 'tool_use', 'name': 'x',
                                'input': '{"a": 1}'})
    check('string arguments are parsed',
          call['message']['content'][0]['input'] == {'a': 1}, call)

    result = translate('gemini', {'type': 'tool_result', 'id': 'c1',
                                  'result': '{"id": 4}'})
    block = result['message']['content'][0]
    check('a tool result becomes a tool_result block',
          block['type'] == 'tool_result', block)
    check('pairing with the call', block['tool_use_id'] == 'c1', block)
    check('and is not an error by default', block['is_error'] is False, block)

    failed_call = translate('gemini', {'type': 'tool_result', 'id': 'c2',
                                       'result': 'nope', 'is_error': True})
    check('a failed tool result is marked',
          failed_call['message']['content'][0]['is_error'] is True, failed_call)

    final = translate('gemini', {'type': 'result', 'response': 'all done'})
    check('the final result is a result event', final['type'] == 'result', final)
    check('with the text', final['result'] == 'all done', final)

    error = translate('gemini', {'type': 'error', 'message': 'it broke'})
    check('an error becomes a failed result', error['subtype'] == 'error', error)

    check('an unknown event is dropped, not crashed on',
          translate('gemini', {'type': 'something-new'}) is None)
    check('a non-dict is dropped', translate('gemini', 'nonsense') is None)


def test_api(client):
    section('the API')

    r = client.get('/api/agent/status', headers=LOCAL)
    body = r.get_json()
    check('status names the provider', body.get('provider') == 'claude', body.get('provider'))
    check('and lists them all', len(body.get('providers', [])) == 3, body.get('providers'))
    codex = next(p for p in body['providers'] if p['id'] == 'codex')
    check('Codex is listed', codex['available'] is True, codex)
    check('with its caveat for the interface to show', bool(codex['caveat']), codex)
    # Detection runs for every agent, not only the chosen one - that is the
    # whole point of showing the list before the user picks.
    for entry in body['providers']:
        check(f"{entry['id']} reports whether it is installed",
              isinstance(entry['installed'], bool), entry)
        check(f"{entry['id']} reports a version when installed",
              (not entry['installed']) or bool(entry['version']), entry)

    r = client.post('/api/agent/provider', json={'provider': 'gemini'}, headers=LOCAL)
    check('a provider can be chosen', r.get_json().get('provider') == 'gemini', r.get_json())
    check('and status agrees',
          client.get('/api/agent/status', headers=LOCAL).get_json()['provider'] == 'gemini')

    r = client.post('/api/agent/provider', json={'provider': 'codex'}, headers=LOCAL)
    check('Codex can be selected over HTTP',
          r.get_json().get('provider') == 'codex', r.get_json())
    r = client.post('/api/agent/provider', json={'provider': 'not-real'}, headers=LOCAL)
    check('an unknown provider falls back rather than erroring',
          r.get_json().get('provider') == 'claude', r.get_json())
    r = client.post('/api/agent/provider', json={}, headers=LOCAL)
    check('an empty payload is refused', r.status_code == 400, r.status_code)

    r = client.post('/api/agent/provider', json={'provider': 'gemini'},
                    headers={'Host': '127.0.0.1:5000', 'Origin': 'https://evil.com'})
    check('cross-origin cannot switch the agent', r.status_code == 403, r.status_code)

    client.post('/api/agent/provider', json={'provider': 'claude'}, headers=LOCAL)



# The events below are not invented for the test. They are the shapes the
# installed CLIs emit, copied from Gemini's own bundle (the emitEvent call
# sites in StreamJsonFormatter) and from the serde tag table inside codex.exe.
# The first version of these translators was written from documentation and got
# five of them wrong, so the real shapes are pinned here.

GEMINI_INIT = {'type': 'init', 'timestamp': '2026-09-28T20:00:00.000Z',
               'session_id': 'sess-1', 'model': 'gemini-2.5-pro'}
GEMINI_USER_ECHO = {'type': 'message', 'timestamp': '...', 'role': 'user',
                    'content': 'You are the assistant inside X Post Management'}
GEMINI_SAY = {'type': 'message', 'timestamp': '...', 'role': 'assistant',
              'content': 'Scheduling that now.', 'delta': True}
GEMINI_CALL = {'type': 'tool_use', 'timestamp': '...', 'tool_name': 'create_post',
               'tool_id': 'call-7', 'parameters': {'content': 'hello'}}
GEMINI_OK = {'type': 'tool_result', 'timestamp': '...', 'tool_id': 'call-7',
             'status': 'success', 'output': 'Saved as post 12.'}
GEMINI_TOOL_FAIL = {'type': 'tool_result', 'timestamp': '...', 'tool_id': 'call-7',
                    'status': 'error', 'output': '',
                    'error': {'type': 'TOOL_EXECUTION_ERROR',
                              'message': 'The text is too long.'}}
GEMINI_DONE = {'type': 'result', 'timestamp': '...', 'status': 'success',
               'stats': {'total_tokens': 900}}
GEMINI_RUN_FAIL = {'type': 'result', 'timestamp': '...', 'status': 'error',
                   'error': {'type': 'FatalToolExecutionError',
                             'message': 'Ran out of turns.'},
                   'stats': {'total_tokens': 900}}
GEMINI_WARNING = {'type': 'error', 'timestamp': '...', 'severity': 'warning',
                  'message': 'An MCP server was blocked by policy.'}
GEMINI_ERROR = {'type': 'error', 'timestamp': '...', 'severity': 'error',
                'message': 'The model is unavailable.'}


def test_gemini_real_events():
    section('Gemini events, in the shape it really emits them')
    t = providers.translate_gemini_event

    check('init carries the session id',
          t(GEMINI_INIT)['session_id'] == 'sess-1')

    # Gemini echoes the prompt back. Showing it would replay the whole system
    # prompt into the conversation as though the assistant had said it.
    check('the echoed prompt is dropped', t(GEMINI_USER_ECHO) is None,
          t(GEMINI_USER_ECHO))

    said = t(GEMINI_SAY)
    check('the assistant text comes through',
          said['message']['content'][0]['text'] == 'Scheduling that now.', said)

    call = t(GEMINI_CALL)['message']['content'][0]
    check('a call keeps its name', call['name'] == 'create_post', call)
    check('and its arguments', call['input'] == {'content': 'hello'}, call)
    # Without tool_id the call and its result get different ids and the
    # interface can never pair them.
    check('and the id Gemini actually sends', call['id'] == 'call-7', call)

    ok = t(GEMINI_OK)['message']['content'][0]
    check('a result is matched to its call', ok['tool_use_id'] == 'call-7', ok)
    check('a good result is not an error', ok['is_error'] is False, ok)
    check('and carries the output', 'post 12' in ok['content'], ok)

    # status is the only thing that says a tool failed; there is no is_error.
    bad = t(GEMINI_TOOL_FAIL)['message']['content'][0]
    check('a failed tool is reported as failed', bad['is_error'] is True, bad)
    check('with the reason, not an empty box',
          'too long' in bad['content'], bad)

    done = t(GEMINI_DONE)
    check('a finished run is a success', done['subtype'] == 'success', done)

    broken = t(GEMINI_RUN_FAIL)
    check('a failed run is not reported as a success',
          broken['subtype'] == 'error', broken)
    check('and says why', 'Ran out of turns' in broken['error'], broken)

    # A warning must not end the turn: Gemini keeps going after one, and the
    # answer would be thrown away with it.
    warned = t(GEMINI_WARNING)
    check('a warning does not end the conversation',
          warned['type'] == 'xpm' and warned['subtype'] == 'notice', warned)
    check('but is still passed on', 'blocked' in warned['notice'], warned)

    fatal = t(GEMINI_ERROR)
    check('a real error does end it',
          fatal['type'] == 'result' and fatal['subtype'] == 'error', fatal)


CODEX_START = {'type': 'thread.started', 'thread_id': 'th-9'}
CODEX_SAY_PARTIAL = {'type': 'item.started',
                     'item': {'id': 'i1', 'type': 'agent_message',
                              'text': 'Scheduling that now.'}}
CODEX_SAY = {'type': 'item.completed',
             'item': {'id': 'i1', 'type': 'agent_message',
                      'text': 'Scheduling that now.'}}
CODEX_CALL = {'type': 'item.started',
              'item': {'id': 'i2', 'type': 'mcp_tool_call', 'server': 'xpost',
                       'tool': 'create_post', 'status': 'in_progress',
                       'arguments': {'content': 'hello'}}}
CODEX_OK = {'type': 'item.completed',
            'item': {'id': 'i2', 'type': 'mcp_tool_call', 'server': 'xpost',
                     'tool': 'create_post', 'status': 'completed',
                     'arguments': {'content': 'hello'},
                     'result': 'Saved as post 12.'}}
CODEX_TOOL_FAIL = {'type': 'item.completed',
                   'item': {'id': 'i2', 'type': 'mcp_tool_call',
                            'server': 'xpost', 'tool': 'create_post',
                            'status': 'failed', 'arguments': {},
                            'error': {'message': 'The text is too long.'}}}
CODEX_DONE = {'type': 'turn.completed', 'usage': {'input_tokens': 10}}
CODEX_FAIL = {'type': 'turn.failed',
              'error': {'message': 'The model refused the request.'}}


def test_codex_real_events():
    section('Codex events, in the shape it really emits them')
    t = providers.translate_codex_event

    check('the thread id becomes the session id',
          t(CODEX_START)['session_id'] == 'th-9', t(CODEX_START))

    # The same message arrives twice, once in progress and once finished.
    check('a partial message is not shown twice',
          t(CODEX_SAY_PARTIAL) is None, t(CODEX_SAY_PARTIAL))
    said = t(CODEX_SAY)
    check('the finished message is shown',
          said['message']['content'][0]['text'] == 'Scheduling that now.', said)

    call = t(CODEX_CALL)['message']['content'][0]
    check('a call in progress is a call', call['type'] == 'tool_use', call)
    check('with the tool name Codex uses', call['name'] == 'create_post', call)
    check('and its id', call['id'] == 'i2', call)

    ok = t(CODEX_OK)['message']['content'][0]
    check('a finished call becomes its result',
          ok['type'] == 'tool_result', ok)
    check('paired with the call', ok['tool_use_id'] == 'i2', ok)
    check('a completed status is not an error', ok['is_error'] is False, ok)

    bad = t(CODEX_TOOL_FAIL)['message']['content'][0]
    check('a failed status is an error', bad['is_error'] is True, bad)
    check('with the reason', 'too long' in bad['content'], bad)

    check('a finished turn is a success',
          t(CODEX_DONE)['subtype'] == 'success', t(CODEX_DONE))

    failed_turn = t(CODEX_FAIL)
    check('a failed turn is an error', failed_turn['subtype'] == 'error',
          failed_turn)
    # The reason is nested in an object; reading the key straight out would
    # print a Python dict at the user.
    check('and reads as a sentence, not a dict',
          failed_turn['error'] == 'The model refused the request.',
          failed_turn['error'])


def test_no_provider_leaks_a_dict_at_the_user():
    section('nothing reaches the interface as a raw object')

    # Whatever a CLI nests, what the interface renders has to be text.
    samples = [
        ('gemini', GEMINI_RUN_FAIL), ('gemini', GEMINI_TOOL_FAIL),
        ('gemini', GEMINI_ERROR), ('codex', CODEX_FAIL),
        ('codex', CODEX_TOOL_FAIL),
    ]
    for provider_id, event in samples:
        out = providers.translate_event(provider_id, event) or {}
        text = out.get('error', '')
        if not text:
            content = out.get('message', {}).get('content', [{}])[0]
            text = content.get('content', '')
        check(f'{provider_id} {event["type"]} reads as text',
              isinstance(text, str) and '{' not in text and text.strip() != '',
              repr(text))


# Copied from a real `codex exec --json` run against the config this app writes.
# Codex reported the config problem as an item in the stream, not on stderr, and
# then carried on - so an item like this must be shown without ending the turn.
CODEX_CONFIG_COMPLAINT = {
    'type': 'item.completed',
    'item': {'id': 'item_0', 'type': 'error',
             'message': 'Codex is ignoring 1 unrecognized configuration '
                        'setting. Check for typos or deprecated settings.'},
}


def test_codex_config_is_one_it_accepts():
    section('the Codex config holds only keys Codex knows')

    work = tempfile.mkdtemp(prefix='xpm-codex-')
    try:
        home = providers.write_codex_config(
            work,
            {'mcpServers': {'xpost': {
                'command': 'python', 'args': ['-m', 'mcp_server'],
                'env': {'XPM_API_BASE': 'http://127.0.0.1:5000'}}}},
            False)
        path = os.path.join(home, 'config.toml')
        raw = io.open(path, encoding='utf-8').read()

        with io.open(path, 'rb') as handle:
            parsed = tomllib.load(handle)
        check('the config is valid TOML', isinstance(parsed, dict))

        # Codex's [tools] table takes exactly these three. Anything else is
        # reported to the user as an error on every single turn.
        allowed = {'web_search', 'experimental_request_user_input', 'update_plan'}
        extra = set(parsed.get('tools', {})) - allowed
        check('no [tools] key Codex would reject', extra == set(), extra)
        check('view_image is gone, it was never a real setting',
              'view_image' not in raw, raw)

        check('it still may not ask for approval nobody can give',
              parsed['approval_policy'] == 'never', parsed)
        check('and it still runs read-only', parsed['sandbox_mode'] == 'read-only',
              parsed)
        check('this app is the only MCP server',
              list(parsed['mcp_servers']) == ['xpost'], parsed)
        check('and it is required, so a broken one is not silently skipped',
              parsed['mcp_servers']['xpost']['required'] is True, parsed)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_codex_error_item_is_shown():
    section('a Codex error item reaches the user')

    out = providers.translate_codex_event(CODEX_CONFIG_COMPLAINT)
    check('it is not dropped', out is not None, out)
    # It must not end the turn: Codex keeps working after one, and the answer
    # would be discarded along with it.
    check('and does not end the turn',
          out['type'] == 'xpm' and out['subtype'] == 'notice', out)
    check('and carries what Codex said',
          'unrecognized configuration' in out['notice'], out)


# The 14 events of a real `codex exec --json` run, in order, with no account
# signed in. Codex retries a connection several times and only then gives up, so
# the sequence is the point: the first "error" is not the end of anything.
CODEX_REAL_SEQUENCE = (
    [{'type': 'thread.started', 'thread_id': '01a0e962'},
     {'type': 'turn.started'}]
    + [{'type': 'error', 'message': f'Reconnecting... {n}/5 (unexpected status '
                                    '401 Unauthorized)'} for n in (2, 3, 4, 5)]
    + [{'type': 'item.completed',
        'item': {'id': 'item_0', 'type': 'error',
                 'message': 'Codex is ignoring 1 unrecognized setting.'}}]
    + [{'type': 'error', 'message': f'Reconnecting... {n}/5 (unexpected status '
                                    '401 Unauthorized)'} for n in (1, 2, 3, 4, 5)]
    + [{'type': 'error', 'message': 'unexpected status 401 Unauthorized'},
       {'type': 'turn.failed', 'error': {'message': 'Missing authentication.'}}]
)


def test_codex_sequence_ends_once_and_at_the_end():
    section('a real Codex run, event by event')

    translated = [providers.translate_codex_event(e) for e in CODEX_REAL_SEQUENCE]
    kept = [e for e in translated if e is not None]

    terminal = [e for e in kept if e.get('type') == 'result']
    check('the turn ends exactly once', len(terminal) == 1, terminal)
    check('and it ends as a failure', terminal[0]['subtype'] == 'error', terminal)
    check('with the reason read out of its object',
          terminal[0]['error'] == 'Missing authentication.', terminal[0])

    # The first eleven errors are retries. Ending on the first one declared the
    # turn dead while Codex was still working, and a retry usually succeeds.
    check('the terminal event is the last one',
          kept.index(terminal[0]) == len(kept) - 1, len(kept))

    notices = [e for e in kept if e.get('subtype') == 'notice']
    check('every retry is passed on as a notice', len(notices) == 11, len(notices))
    check('including the config complaint',
          any('unrecognized' in e['notice'] for e in notices), notices[:2])
    check('and a retry reads as itself',
          any('Reconnecting' in e['notice'] for e in notices), notices[:2])

    check('the thread id is picked up',
          kept[0].get('session_id') == '01a0e962', kept[0])


def test_packaged_build_points_every_agent_at_itself():
    """In a frozen build there is no interpreter to run -m mcp_server.

    The .exe re-runs itself with --mcp instead. Each provider writes its own
    config file from the same payload, so all three have to inherit that - and
    if one did not, the MCP server would fail to start for everyone using that
    agent from the packaged app, which is exactly how this app broke once before.
    """
    section('the packaged build starts its own MCP server')

    frozen_before = getattr(sys, 'frozen', None)
    executable_before = sys.executable
    sys.frozen = True
    sys.executable = r'C:\Program Files\X Post Management\X Post Management.exe'
    work = tempfile.mkdtemp(prefix='xpm-frozen-')
    try:
        config = agent.mcp_config('http://127.0.0.1:5000', False)
        server = config['mcpServers']['xpost']
        check('the exe re-runs itself', server['command'] == sys.executable, server)
        check('with --mcp', server['args'] == ['--mcp'], server)
        # PYTHONPATH would point at a source folder that is not in the bundle.
        check('and no PYTHONPATH pointing at sources that are not there',
              'PYTHONPATH' not in server['env'], server['env'])

        providers.write_gemini_settings(work, config, False)
        written = json.load(io.open(os.path.join(work, '.gemini', 'settings.json'),
                                    encoding='utf-8'))
        gemini = written['mcpServers']['xpost']
        check('Gemini is told to run the exe', gemini['args'] == ['--mcp'], gemini)

        home = providers.write_codex_config(work, config, False)
        with io.open(os.path.join(home, 'config.toml'), 'rb') as handle:
            codex = tomllib.load(handle)['mcp_servers']['xpost']
        check('and so is Codex', codex['args'] == ['--mcp'], codex)
        check('both name the same executable',
              gemini['command'] == codex['command'] == sys.executable,
              (gemini['command'], codex['command']))
    finally:
        sys.executable = executable_before
        if frozen_before is None:
            del sys.frozen
        else:
            sys.frozen = frozen_before
        shutil.rmtree(work, ignore_errors=True)

    # and development is unchanged: an interpreter plus the module
    server = agent.mcp_config('http://127.0.0.1:5000', False)['mcpServers']['xpost']
    check('development still runs the module',
          server['args'] == ['-m', 'mcp_server'], server)
    check('with the server folder on the path',
          server['env'].get('PYTHONPATH', '').endswith('server'), server['env'])

def main():
    print('=' * 62)
    print('  Agent provider tests')
    print('=' * 62)

    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    test_catalogue()
    test_choosing()
    test_gemini_is_confined()
    test_codex_is_confined_as_far_as_it_goes()
    test_codex_translation()
    test_event_translation()
    test_gemini_real_events()
    test_codex_real_events()
    test_codex_config_is_one_it_accepts()
    test_codex_error_item_is_shown()
    test_codex_sequence_ends_once_and_at_the_end()
    test_packaged_build_points_every_agent_at_itself()
    test_no_provider_leaks_a_dict_at_the_user()
    test_api(client)

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
        config.delete_password()
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
