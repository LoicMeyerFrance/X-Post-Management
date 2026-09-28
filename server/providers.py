"""The agent CLIs this app can drive.

The assistant does not embed a model. It runs a command-line agent the user has
already installed and signed in, and gives it this app's tools over MCP. Which
agent is a user choice; what the agent is allowed to do is not.

Every provider here has to meet the same bar, or it does not ship:

  * only this app's tools - no shell, no filesystem, no other MCP server;
  * its own workspace, so nothing in the user's projects leaks into a run;
  * a way to approve this app's tools without approving everything else.

Claude Code and Gemini meet all three. Codex meets two: its built-in shell and
file tools cannot be switched off - the config reference is explicit that they
are core and have no disable switch - so a Codex run can read the user's files
and execute read-only commands, which a Claude Code or Gemini run cannot. It is
confined as far as it goes (read-only sandbox, no writes, this app's MCP server
and no other) and it says so in the interface rather than quietly offering a
weaker promise under the same wording.
"""

import json
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

CLAUDE = 'claude'
GEMINI = 'gemini'
CODEX = 'codex'

DEFAULT_PROVIDER = CLAUDE


# --- finding the binaries ---------------------------------------------------

def _no_window():
    if sys.platform != 'win32':
        return {}
    return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}


def _extra_locations(names):
    """Places a GUI app may have to look, when it did not inherit a shell PATH."""
    out = []
    home = os.path.expanduser('~')
    appdata = os.environ.get('APPDATA', '')
    localappdata = os.environ.get('LOCALAPPDATA', '')
    for name in names:
        if appdata:
            out += [os.path.join(appdata, 'npm', name + ext)
                    for ext in ('.cmd', '.CMD', '')]
        out += [
            os.path.join(home, '.local', 'bin', name + '.exe'),
            os.path.join(home, '.local', 'bin', name),
            os.path.join(home, '.npm-global', 'bin', name),
            '/usr/local/bin/' + name,
            '/opt/homebrew/bin/' + name,
        ]
        if localappdata:
            out += [os.path.join(localappdata, 'Microsoft', 'WinGet', 'Links', name + '.exe'),
                    os.path.join(localappdata, 'Microsoft', 'WindowsApps', name + '.exe')]
    return out


def find_binary(names):
    """Absolute path to the first of `names` on this machine, or ''.

    Resolved to an absolute path on purpose: Windows will not run a .cmd by bare
    name through CreateProcess, and a packaged app started from Explorer can have
    a narrower PATH than a terminal.
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    for candidate in _extra_locations(names):
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ''


# --- provider descriptions --------------------------------------------------

PROVIDERS = {
    CLAUDE: {
        'id': CLAUDE,
        'label': 'Claude Code',
        'vendor': 'Anthropic',
        'binaries': ('claude',),
        'available': True,
        'install': {
            'win32': 'irm https://claude.ai/install.ps1 | iex',
            'other': 'curl -fsSL https://claude.ai/install.sh | bash',
        },
        'docs': 'https://code.claude.com',
        'plan_note': 'Needs a Claude Pro, Max, Team or Enterprise plan.',
    },
    GEMINI: {
        'id': GEMINI,
        'label': 'Gemini CLI',
        'vendor': 'Google',
        'binaries': ('gemini',),
        'available': True,
        'install': {
            'win32': 'npm install -g @google/gemini-cli',
            'other': 'npm install -g @google/gemini-cli',
        },
        'docs': 'https://github.com/google-gemini/gemini-cli',
        # Observed with CLI 0.61.0 and a personal Google account: the OAuth
        # sign-in succeeds and Google then refuses the client with
        # IneligibleTierError, pointing individuals at its Antigravity
        # products. So the card leads with the route that works.
        'plan_note': ('Needs a Gemini API key in GEMINI_API_KEY. A personal '
                      'Google sign-in is currently refused by Google itself.'),
    },
    CODEX: {
        'id': CODEX,
        'label': 'Codex',
        'vendor': 'OpenAI',
        'binaries': ('codex',),
        'available': True,
        # Said plainly, because the guarantee really is weaker here. Codex's
        # shell and file tools are core to it and cannot be turned off, so
        # "only this app's tools" is not true for this one.
        'caveat': (
            'Codex keeps its own file and shell tools whatever this app asks: they '
            'cannot be switched off. It runs read-only and cannot write anywhere, '
            'but unlike the others it can read files on your computer.'
        ),
        'install': {
            'win32': 'npm install -g @openai/codex',
            'other': 'curl -fsSL https://chatgpt.com/codex/install.sh | sh',
        },
        'docs': 'https://learn.chatgpt.com/docs/non-interactive-mode',
        'plan_note': 'Sign in with ChatGPT, or set OPENAI_API_KEY.',
    },
}

ORDER = (CLAUDE, GEMINI, CODEX)


def get(provider_id):
    return PROVIDERS.get(provider_id or DEFAULT_PROVIDER) or PROVIDERS[DEFAULT_PROVIDER]


def normalise(provider_id):
    """A provider id we are willing to run, falling back to the default."""
    candidate = str(provider_id or '').strip().lower()
    if candidate in PROVIDERS and PROVIDERS[candidate]['available']:
        return candidate
    return DEFAULT_PROVIDER


def install_command(provider_id):
    spec = get(provider_id)
    key = 'win32' if sys.platform == 'win32' else 'other'
    return spec['install'].get(key, '')


# --- Gemini: its workspace configuration ------------------------------------
#
# Gemini reads .gemini/settings.json from the directory it runs in, so the app
# writes one into its own working folder. The user's own settings are never
# touched, and the run carries only what is written here.

def write_gemini_settings(work_dir, mcp_config, allow_publish):
    """Confine a Gemini run to this app's tools. Returns the file written."""
    server_name, server = next(iter(mcp_config['mcpServers'].items()))

    settings = {
        # An empty allowlist of built-in tools: no shell, no file access, no web.
        # MCP tools are configured separately and stay available.
        'tools': {'core': [], 'exclude': ['run_shell_command', 'ShellTool']},
        # The older flat spelling, for a CLI that predates the nested one. An
        # unknown key is ignored; a missing one would leave the shell enabled.
        'coreTools': [],
        'excludeTools': ['run_shell_command', 'ShellTool'],
        'mcpServers': {
            server_name: {
                'command': server['command'],
                'args': server.get('args', []),
                'env': server.get('env', {}),
                # Trust this one server, so its tools run without a prompt there
                # is nobody to answer. Emphatically not YOLO: nothing else is
                # trusted, and nothing else is loaded.
                'trust': True,
            }
        },
    }
    folder = os.path.join(work_dir, '.gemini')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, 'settings.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def write_codex_config(work_dir, mcp_config, allow_publish):
    """Confine a Codex run as far as Codex can be confined. Returns CODEX_HOME.

    Codex reads config.toml from CODEX_HOME, so the app points that at its own
    folder: the user's ~/.codex is never read or written.

    approval_policy = "never" is what lets MCP tools run with nobody at a
    terminal to approve them - without it they are cancelled the moment stdin
    closes. It is paired with the read-only sandbox, so "never ask" never means
    "may write".
    """
    server_name, server = next(iter(mcp_config['mcpServers'].items()))

    def toml_string(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

    lines = [
        '# Written by X Post Management for its own runs. Not the user\'s config.',
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        '',
        # Codex's [tools] table takes exactly three keys - web_search,
        # experimental_request_user_input and update_plan. An unknown one is
        # not ignored quietly: Codex reports it as an error item on every turn,
        # which the user reads as the app being broken. view_image was in here
        # and did nothing but that.
        '[tools]',
        'web_search = false',
        '',
        f'[mcp_servers.{server_name}]',
        'command = ' + toml_string(server['command']),
        'args = [' + ', '.join(toml_string(a) for a in server.get('args', [])) + ']',
        'required = true',
    ]
    env = server.get('env', {})
    if env:
        pairs = ', '.join(f'{key} = {toml_string(value)}' for key, value in env.items())
        lines.append('env = { ' + pairs + ' }')

    home = os.path.join(work_dir, '.codex-home')
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, 'config.toml')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    os.replace(tmp, path)
    return home


def translate_codex_event(event):
    """One Codex JSONL event as a Claude-shaped one, or None to drop it.

    Tag names read from the serde table inside codex.exe: the thread events are
    thread.started / turn.started / turn.completed / turn.failed and
    item.started / item.updated / item.completed, items carry a status of
    in_progress / completed / failed, and an mcp_tool_call item carries
    server, tool, arguments and result.
    """
    if not isinstance(event, dict):
        return None
    kind = str(_first(event, 'type', 'event', default='')).lower()

    if kind in ('thread.started', 'session.created', 'turn.started'):
        return {'type': 'system', 'subtype': 'init',
                'session_id': str(_first(event, 'thread_id', 'session_id',
                                         default='')),
                'tools': _first(event, 'tools', default=[]),
                'mcp_servers': _first(event, 'mcp_servers', default=[])}

    item = _first(event, 'item', default=None)
    if isinstance(item, dict):
        item_type = str(_first(item, 'type', 'item_type', default='')).lower()
        if item_type in ('assistant_message', 'message', 'agent_message'):
            # The same message arrives on item.started and again on
            # item.completed. Taking only the finished one keeps it from
            # appearing twice in the conversation.
            if kind not in ('item.completed', 'item.done', ''):
                return None
            text = _as_text(_first(item, 'text', 'content', default=''))
            if not text.strip():
                return None
            return {'type': 'assistant',
                    'message': {'content': [{'type': 'text', 'text': text}]}}
        if item_type == 'error':
            # Codex reports trouble as an item and keeps going; only
            # turn.failed ends a turn. Ending it here would throw away the
            # answer that still arrives, and dropping it leaves the user
            # watching nothing happen.
            return {'type': 'xpm', 'subtype': 'notice',
                    'notice': _error_message(item, 'Codex reported a problem.')}
        if 'tool' in item_type or item_type == 'mcp_tool_call':
            name = str(_first(item, 'tool', 'name', 'tool_name', default=''))
            arguments = _first(item, 'arguments', 'input', 'args', default={})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {'value': arguments}
            call_id = str(_first(item, 'id', 'call_id', default=name))
            output = _first(item, 'output', 'result', default=None)
            broken = _failed(item)
            if output is None and not broken:
                return {'type': 'assistant', 'message': {'content': [{
                    'type': 'tool_use', 'id': call_id, 'name': name,
                    'input': arguments if isinstance(arguments, dict) else {}}]}}
            content = _as_text(output)
            if broken and not content.strip():
                content = _error_message(item, 'The tool failed.')
            return {'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': call_id,
                'content': content, 'is_error': broken}]}}

    if kind in ('turn.completed', 'thread.completed'):
        return {'type': 'result', 'subtype': 'success',
                'result': _as_text(_first(event, 'text', 'output', default=''))}

    if kind in ('turn.failed', 'thread.error'):
        # turn.failed nests the reason in an object; reading the key straight
        # out would print a Python dict at the user.
        return {'type': 'result', 'subtype': 'error',
                'error': _error_message(event, 'Codex failed')}

    if kind == 'error':
        # Not the end of anything. A real run emitted eleven of these -
        # "Reconnecting... 2/5" and so on - before turn.failed finally came.
        # Treating the first as terminal declared the turn dead while Codex was
        # still retrying, and a retry that succeeds is the normal case.
        return {'type': 'xpm', 'subtype': 'notice',
                'notice': _error_message(event, 'Codex reported a problem.')}

    return None

# --- Gemini: making its output look like the one the interface already reads --
#
# The frontend understands Claude Code's stream-json. Rather than teach it a
# second dialect, Gemini's events are translated here: one shape in the
# interface, one place to fix when a CLI changes its mind.

def _first(mapping, *names, default=None):
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) is not None:
            return mapping[name]
    return default


def _failed(event):
    """True when the provider marked this step as failed.

    Both CLIs report failure with a status word rather than a boolean, so
    looking only for is_error reads every failure as a success.
    """
    status = str(_first(event, 'status', default='')).lower()
    if status in ('error', 'failed', 'failure'):
        return True
    if status in ('success', 'completed', 'ok', 'in_progress'):
        return False
    flag = _first(event, 'is_error', 'isError', default=None)
    if flag is not None:
        return bool(flag)
    error = _first(event, 'error', default=None)
    return bool(error) if isinstance(error, (dict, str)) else False


def _error_message(event, default=''):
    """The readable part of an error, however deeply the CLI nested it."""
    error = _first(event, 'error', default=None)
    if isinstance(error, dict):
        return str(_first(error, 'message', 'detail', 'text', default=default))
    if isinstance(error, str) and error.strip():
        return error
    message = _first(event, 'message', default=None)
    if isinstance(message, str) and message.strip():
        return message
    return default


def _as_text(value):
    """Whatever the CLI put in a content field, as a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(_first(value, 'text', 'content', default=''))
    if isinstance(value, list):
        parts = []
        for piece in value:
            text = _as_text(piece)
            if text:
                parts.append(text)
        return ' '.join(parts)
    return '' if value is None else str(value)


def translate_gemini_event(event):
    """One Gemini JSONL event as a Claude-shaped one, or None to drop it.

    The field names are the ones the installed CLI actually emits, read from
    its own bundle rather than from the documentation:

        {"type":"init","session_id":...,"model":...}
        {"type":"message","role":"user"|"assistant","content":...,"delta":true}
        {"type":"tool_use","tool_name":...,"tool_id":...,"parameters":{...}}
        {"type":"tool_result","tool_id":...,"status":"success"|"error",
         "output":...,"error":{"type","message"}}
        {"type":"result","status":"success"|"error","error":{...},"stats":{...}}
        {"type":"error","severity":"error"|"warning","message":...}
    """
    if not isinstance(event, dict):
        return None
    kind = str(_first(event, 'type', 'event', default='')).lower()

    if kind == 'init':
        return {
            'type': 'system',
            'subtype': 'init',
            'session_id': _first(event, 'session_id', 'sessionId', default=''),
            'tools': _first(event, 'tools', default=[]),
            'mcp_servers': _first(event, 'mcp_servers', 'mcpServers', default=[]),
        }

    if kind in ('message', 'assistant', 'content'):
        # Gemini echoes the prompt back as role "user". Rendering that would
        # replay the entire system prompt in the conversation as if the
        # assistant had written it.
        role = str(_first(event, 'role', default='assistant')).lower()
        if role and role != 'assistant':
            return None
        text = _as_text(_first(event, 'text', 'content', 'message', default=''))
        if not text.strip():
            return None
        return {'type': 'assistant',
                'message': {'content': [{'type': 'text', 'text': text}]}}

    if kind in ('tool_use', 'tool_call', 'tooluse'):
        name = str(_first(event, 'tool_name', 'name', 'tool', default=''))
        arguments = _first(event, 'parameters', 'input', 'args', 'arguments',
                           default={})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {'value': arguments}
        return {'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use',
            # tool_id is what Gemini sends; without it a call and its result
            # carry different ids and the interface never pairs them up.
            'id': str(_first(event, 'tool_id', 'id', 'call_id', 'tool_use_id',
                             default=name)),
            'name': name,
            'input': arguments if isinstance(arguments, dict) else {},
        }]}}

    if kind in ('tool_result', 'tool_response', 'toolresult'):
        content = _as_text(_first(event, 'output', 'result', 'content',
                                  'response', default=''))
        broken = _failed(event)
        if broken and not content.strip():
            content = _error_message(event, 'The tool failed.')
        return {'type': 'user', 'message': {'content': [{
            'type': 'tool_result',
            'tool_use_id': str(_first(event, 'tool_id', 'id', 'call_id',
                                      'tool_use_id', default='')),
            'content': content,
            'is_error': broken,
        }]}}

    if kind == 'result':
        if _failed(event):
            return {'type': 'result', 'subtype': 'error',
                    'error': _error_message(
                        event, 'The assistant stopped with an error.')}
        return {'type': 'result', 'subtype': 'success',
                'result': _as_text(_first(event, 'response', 'result', 'text',
                                          default=''))}

    if kind == 'error':
        message = _error_message(event, 'Unknown error')
        severity = str(_first(event, 'severity', default='error')).lower()
        if severity in ('warning', 'warn', 'info', 'debug'):
            # A warning is not the end of the turn. Gemini emits these for
            # things like a blocked MCP server and then carries on, so ending
            # the conversation here would hide the answer that follows.
            return {'type': 'xpm', 'subtype': 'notice', 'notice': message}
        return {'type': 'result', 'subtype': 'error', 'error': message}

    return None


def translate_event(provider_id, event):
    """Whatever the provider said, in the one shape the interface reads."""
    if provider_id == GEMINI:
        return translate_gemini_event(event)
    if provider_id == CODEX:
        return translate_codex_event(event)
    return event                       # Claude Code already speaks it
