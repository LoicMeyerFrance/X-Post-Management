"""Bridge to the Claude Code CLI, so the app can host a chat that drives itself.

The app never handles a Claude login. It runs the `claude` binary the user has
already installed and signed in, and reads its newline-delimited JSON stream.
That is what makes "use my own account's tokens" work with no code: the CLI
already holds the subscription session.

Two authentication paths, decided by whether a key is stored:

  * no key  -> the CLI's own login is used (the user's subscription). `--bare`
               must NOT be passed: in bare mode the CLI "never reads OAuth
               credentials or the system keychain" and requires an API key.
  * a key   -> `--bare` plus ANTHROPIC_API_KEY in the child environment only.
               Bare mode is what Anthropic recommends for scripted calls, and it
               also stops the run from loading whatever hooks, MCP servers or
               CLAUDE.md happen to sit in the working directory.

Distribution note: offering *other people* a claude.ai login inside a product is
not allowed without Anthropic's approval, which is why the shipped path is the
user's own API key and the subscription path only works for whoever is signed in
on this machine.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading

import config
import paths

logger = logging.getLogger(__name__)

SESSION_PATH = os.path.join(paths.DATA_DIR, 'agent_session.json')

# Where this package's modules live. Deliberately derived from __file__ and not
# from paths.BASE_DIR: BASE_DIR is the *data* directory and moves with XPM_HOME,
# so using it to find mcp_server.py breaks the moment the data lives elsewhere.
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

MCP_SERVER_NAME = 'xpost'

# The tools the agent may use without being asked, per mode. Read and draft
# tools are always safe: nothing here reaches X.
DRAFT_TOOLS = (
    'get_limits', 'list_posts', 'get_post',
    'create_post', 'update_post', 'delete_post',
)
PUBLISH_TOOLS = ('publish_now', 'schedule_on_x')

# `--permission-prompts none` landed in this version. Older CLIs reject unknown
# options outright, so it is only passed when the installed version is new
# enough; without it an unattended run can sit waiting for an answer.
PERMISSION_PROMPTS_SINCE = (2, 1, 259)

MAX_PROMPT_CHARS = 16000

# Windows will not execute a .cmd through CreateProcess by name, and PATHEXT is
# not applied to an argv[0] either - the resolved absolute path works, a bare
# "claude" raises WinError 2. Everything below therefore uses the resolved path.
CLI_NAMES = ('claude',)


class AgentError(RuntimeError):
    """Something the user needs to fix before a run can start."""


# --- locating the CLI ------------------------------------------------------

def _npm_global_candidates():
    """Where npm puts global binaries, for a PATH that a GUI app did not inherit.

    A packaged desktop app launched from Explorer can have a narrower PATH than
    the user's shell, so `which` alone is not enough.
    """
    out = []
    appdata = os.environ.get('APPDATA', '')
    if appdata:
        out += [os.path.join(appdata, 'npm', 'claude.cmd'),
                os.path.join(appdata, 'npm', 'claude.CMD'),
                os.path.join(appdata, 'npm', 'claude')]
    home = os.path.expanduser('~')
    out += [
        # Where the native installer puts it - the recommended install, and the
        # one that needs no Node. On Windows it is claude.exe, which an earlier
        # version of this list missed entirely.
        os.path.join(home, '.local', 'bin', 'claude.exe'),
        os.path.join(home, '.local', 'bin', 'claude'),
        os.path.join(home, '.npm-global', 'bin', 'claude'),
        os.path.join(home, 'AppData', 'Local', 'Programs', 'claude', 'claude.exe'),
        '/usr/local/bin/claude',
        '/opt/homebrew/bin/claude',
    ]
    # WinGet shims live under Microsoft's app-install links directory.
    localappdata = os.environ.get('LOCALAPPDATA', '')
    if localappdata:
        out += [os.path.join(localappdata, 'Microsoft', 'WinGet', 'Links', 'claude.exe'),
                os.path.join(localappdata, 'Microsoft', 'WindowsApps', 'claude.exe')]
    return out


def find_cli():
    """Absolute path to the claude executable, or '' when it is not installed."""
    for name in CLI_NAMES:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    for candidate in _npm_global_candidates():
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ''


_version_cache = {}


def cli_version(path=None):
    """(major, minor, patch) of the installed CLI, or None when unknown."""
    path = path or find_cli()
    if not path:
        return None
    if path in _version_cache:
        return _version_cache[path]
    try:
        result = subprocess.run([path, '--version'], capture_output=True, text=True,
                                timeout=30, **_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not run %s --version: %s", path, exc)
        return None
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', result.stdout or '')
    version = tuple(int(g) for g in match.groups()) if match else None
    _version_cache[path] = version
    return version


def _no_window():
    """Keep a console window from flashing up on Windows."""
    if sys.platform != 'win32':
        return {}
    return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}


# --- the stored API key ----------------------------------------------------

def get_api_key():
    return config.get_secret(config.KEYRING_ANTHROPIC_KEY)


def set_api_key(value):
    return config.set_secret(config.KEYRING_ANTHROPIC_KEY, value)


def has_api_key():
    return bool(get_api_key())


def auth_mode():
    """Which credential a run would use."""
    if has_api_key():
        return 'api_key'
    if find_cli():
        return 'subscription'
    return 'none'


# --- setting Claude Code up, without a terminal -----------------------------
#
# Someone who downloads this app should not have to learn a command line. Two
# things are needed - the binary, and a signed-in account - and the CLI can do
# both on its own: the official installer is one command, and `claude auth` both
# reports the state and starts the browser sign-in.

INSTALL_URL_WINDOWS = 'https://claude.ai/install.ps1'
INSTALL_URL_UNIX = 'https://claude.ai/install.sh'


def install_command():
    """(argv, what to show the user) for the official installer.

    The displayed string is the command from Anthropic's own install docs. It is
    shown before anything runs: this fetches and executes a remote script, so the
    user gets to see exactly what they are agreeing to.
    """
    if sys.platform == 'win32':
        shown = f'irm {INSTALL_URL_WINDOWS} | iex'
        argv = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-Command', f'irm {INSTALL_URL_WINDOWS} | iex']
    else:
        shown = f'curl -fsSL {INSTALL_URL_UNIX} | bash'
        argv = ['bash', '-c', f'curl -fsSL {INSTALL_URL_UNIX} | bash']
    return argv, shown


def auth_status():
    """Whether the CLI is signed in, straight from `claude auth status`.

    Costs nothing and calls no model, unlike inferring it from a failed run.
    """
    path = find_cli()
    if not path:
        return {'known': False, 'logged_in': False}
    try:
        result = subprocess.run([path, 'auth', 'status', '--json'],
                                capture_output=True, text=True, timeout=60,
                                encoding='utf-8', errors='replace',
                                env=clean_env(), **_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not read the Claude Code auth status: %s", exc)
        return {'known': False, 'logged_in': False}

    raw = (result.stdout or '').strip()
    try:
        data = json.loads(raw)
    except ValueError:
        logger.debug("auth status was not JSON: %r", raw[:200])
        return {'known': False, 'logged_in': False}

    # orgId is of no use to the interface, so it is left out rather than shipped.
    return {
        'known': True,
        'logged_in': bool(data.get('loggedIn')),
        'method': str(data.get('authMethod') or ''),
        'plan': str(data.get('subscriptionType') or ''),
        'email': str(data.get('email') or ''),
    }


def install():
    """Run the official installer. Returns (ok, output)."""
    argv, shown = install_command()
    logger.info("Installing Claude Code with: %s", shown)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=600,
                                encoding='utf-8', errors='replace', **_no_window())
    except FileNotFoundError:
        return False, (f'Could not run the installer ({argv[0]} was not found). '
                       f'Run this yourself in a terminal:\n\n{shown}')
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f'The installer could not be started: {exc}\n\nRun it yourself:\n\n{shown}'

    output = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
    # A fresh install is not on this process's PATH, and the cached miss would
    # otherwise outlive it.
    _version_cache.clear()
    found = find_cli()
    if result.returncode != 0 and not found:
        return False, output[-2000:] or f'The installer exited with code {result.returncode}.'
    if not found:
        return False, ('The installer finished but Claude Code was not found. '
                       'Close and reopen the app, or open a new terminal and run '
                       '"claude --version".')
    logger.info("Claude Code installed at %s", found)
    return True, output[-2000:]


def start_login():
    """Open the browser sign-in, in its own console window. Returns (ok, detail).

    Given its own console rather than a pipe: the sign-in prints a URL and waits,
    and a windowed app has no terminal to show that in. The user sees the prompt
    and can finish it, which is the whole point.
    """
    path = find_cli()
    if not path:
        return False, 'Claude Code is not installed yet.'

    argv = [path, 'auth', 'login', '--claudeai']
    kwargs = {}
    if sys.platform == 'win32':
        # Its own console window; CREATE_NO_WINDOW would hide the very prompt
        # the user has to answer.
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
    else:
        kwargs['start_new_session'] = True

    try:
        subprocess.Popen(argv, env=clean_env(), **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (f'Could not start the sign-in: {exc}\n\n'
                       'Open a terminal and run: claude auth login')
    logger.info("Started Claude Code sign-in")
    return True, 'A window opened to sign in to your Claude account.'


def status():
    """What the settings page and the chat need to know before a run."""
    path = find_cli()
    version = cli_version(path) if path else None
    mode = auth_mode()
    auth = auth_status() if path else {'known': False, 'logged_in': False}
    _, install_shown = install_command()

    # Ready means a turn can actually run: the binary is there, and either the
    # CLI is signed in or a key was supplied. Reporting "ready" on the binary
    # alone sent the user to a chat that failed on its first message.
    ready = bool(path) and (auth.get('logged_in') or has_api_key())

    return {
        'cli_installed': bool(path),
        'cli_path': path,
        'cli_version': '.'.join(str(n) for n in version) if version else '',
        'auth_mode': mode,
        'has_api_key': has_api_key(),
        'logged_in': bool(auth.get('logged_in')),
        'auth_known': bool(auth.get('known')),
        'plan': auth.get('plan', ''),
        'account_email': auth.get('email', ''),
        'ready': ready,
        'session_id': load_session_id(),
        'install_command': install_shown,
        'install_hint': install_shown,
        'can_install': True,
    }


# --- session continuity ----------------------------------------------------

def load_session_id():
    try:
        with open(SESSION_PATH, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        value = data.get('session_id')
        return value if isinstance(value, str) else ''
    except (OSError, ValueError):
        return ''


def save_session_id(session_id):
    if not session_id:
        return
    tmp = SESSION_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'session_id': session_id}, handle)
        os.replace(tmp, SESSION_PATH)
    except OSError as exc:
        logger.warning("Could not save the agent session id: %s", exc)


def clear_session():
    try:
        os.remove(SESSION_PATH)
    except OSError:
        pass


# --- building the command --------------------------------------------------

def mcp_config(api_base, allow_publish):
    """The --mcp-config payload pointing at this app's own MCP server.

    In a frozen build there is no python.exe to call, so the executable re-runs
    itself with --mcp; in development it is the interpreter plus the module.
    """
    env = {'XPM_API_BASE': api_base}
    if allow_publish:
        env['XPM_AGENT_ALLOW_PUBLISH'] = '1'
    # The MCP server resolves its own paths from XPM_HOME, so a test or a
    # relocated install stays consistent across the process boundary.
    if os.environ.get('XPM_HOME'):
        env['XPM_HOME'] = os.environ['XPM_HOME']

    if getattr(sys, 'frozen', False):
        command, args = sys.executable, ['--mcp']
    else:
        command, args = sys.executable, ['-m', 'mcp_server']
        env['PYTHONPATH'] = SERVER_DIR

    return {'mcpServers': {MCP_SERVER_NAME: {
        'command': command,
        'args': args,
        'env': env,
    }}}


# The only built-ins ever handed to the agent, and only when the user asks for
# them. Both are read-only, and WebFetch cannot reach a private address, so
# neither can be turned back on this app's own loopback API.
WEB_TOOLS = ('WebSearch', 'WebFetch')


def _tool_names(allow_publish, web_access=False):
    names = list(DRAFT_TOOLS) + (list(PUBLISH_TOOLS) if allow_publish else [])
    out = [f'mcp__{MCP_SERVER_NAME}__{name}' for name in names]
    # Both need permission by default, so being available is not enough - without
    # this they would be denied on use.
    if web_access:
        out += list(WEB_TOOLS)
    return out


SYSTEM_PROMPT = (
    'You are the assistant inside X Post Management, a desktop app that posts to '
    'X by driving a real browser. Use the xpost tools for anything about posts, '
    'drafts, media or the calendar - never guess at the state, read it. '
    'When the user asks for several tweets, call create_post once per tweet. '
    'Times are the machine\'s local time and carry no timezone suffix. '
    'Keep replies short: say what you did and name the posts by id. '
    'If a request needs something you have no tool for, say so plainly instead '
    'of improvising.'
)

# Whether the agent can look things up. Off, it has no way to reach anything but
# this app; on, it gains read-only web tools and nothing else.
NO_WEB_TAIL = (
    ' You have no shell, no file access and no web access. Never state a fact '
    'about the outside world as if you had checked it.'
)

WEB_TAIL = (
    ' You can search the web and fetch a page, and nothing else beyond that: no '
    'shell, no file access. Use them to check a claim before it goes into a post '
    'rather than guessing - a wrong fact published under the user\'s name is worse '
    'than a slower answer. Say where a figure came from, and say so plainly when a '
    'search settles nothing.'
)

# What happens to a post after create_post differs by mode, and the agent has to
# describe it correctly to the user.
MANUAL_TAIL = (
    ' Nothing you create is public. Each post appears in this conversation with a '
    'tick and a cross, and the user decides there - say it is waiting for them, '
    'and never tell them to go and publish it somewhere else.'
)

AUTO_TAIL = (
    ' The user has turned on automatic approval, so every post you create goes '
    'out to X immediately - a dated one through X\'s own scheduler, an undated one '
    'straight away. Treat create_post as publishing: say what you published, and '
    'do not describe anything as a draft awaiting approval. Because it is '
    'immediate, get the text right before you call it.'
)


def build_command(prompt, session_id='', auto=False, api_base='http://127.0.0.1:5000',
                  cli_path=None, version=None, web_access=False):
    """The argv for one turn. Raises AgentError when the CLI is missing."""
    cli_path = cli_path or find_cli()
    if not cli_path:
        raise AgentError('Claude Code is not installed. Install it with '
                         '"npm install -g @anthropic-ai/claude-code", then sign in '
                         'with "claude" once.')
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AgentError(f'That message is too long ({len(prompt)} characters, '
                         f'maximum {MAX_PROMPT_CHARS}).')

    use_key = has_api_key()
    argv = [cli_path, '-p', prompt,
            '--output-format', 'stream-json',
            '--verbose', '--include-partial-messages']

    # Bare mode is right for a scripted call, but it cannot read the
    # subscription login - so it is only used when we have a key to supply.
    if use_key:
        argv.append('--bare')

    argv += ['--mcp-config', json.dumps(mcp_config(api_base, auto))]

    # This is what confines the agent to posts.
    #
    # --allowedTools only pre-approves; it does not restrict. On its own the
    # session would still carry Claude Code's built-ins, and the read-only Bash
    # set runs without a prompt *in every permission mode* - so the agent could
    # read files and run commands even in manual mode. Two flags close that:
    #
    #   --tools ""           drops every built-in tool (Bash, Read, Write, Edit,
    #                        WebFetch, WebSearch, Task...). MCP tools come from
    #                        --mcp-config and are not affected by this flag.
    #   --strict-mcp-config  ignores MCP servers configured globally or per
    #                        project, so no other tools can slip into the session.
    #
    # What is left is exactly the xpost tools, plus the two read-only web tools
    # when the user has asked for them. Naming them in --tools is a whitelist, not
    # a relaxation: Bash, Read, Write and the rest stay out either way.
    argv += ['--tools', ','.join(WEB_TOOLS) if web_access else '']
    argv.append('--strict-mcp-config')

    argv += ['--allowedTools', ','.join(_tool_names(auto, web_access))]
    tail = (AUTO_TAIL if auto else MANUAL_TAIL) + (WEB_TAIL if web_access else NO_WEB_TAIL)
    argv += ['--append-system-prompt', SYSTEM_PROMPT + tail]

    if auto:
        argv += ['--permission-mode', 'auto']
    else:
        # Second gate on publishing, this time at the client: a bare-name deny
        # rule removes the tool from the model's context entirely, in every mode.
        # The MCP server refuses these calls too - neither layer is load-bearing
        # on its own.
        argv += ['--disallowedTools',
                 ','.join(f'mcp__{MCP_SERVER_NAME}__{name}' for name in PUBLISH_TOOLS)]

    version = version if version is not None else cli_version(cli_path)
    if version and version >= PERMISSION_PROMPTS_SINCE:
        # Nobody is at a terminal to answer a prompt; deny instead of hanging.
        argv += ['--permission-prompts', 'none']

    if session_id:
        argv += ['--resume', session_id]

    return argv


def clean_env():
    """Environment for `claude auth ...`, with no API key in it.

    Two reasons the key must not leak into these calls. `auth status` answers
    "is the CLI itself signed in", which a key in the environment muddles; and
    `auth login` prompts to approve an ANTHROPIC_API_KEY instead of opening the
    browser when it finds one, which is the opposite of what the button promises.
    """
    env = dict(os.environ)
    env.pop('ANTHROPIC_API_KEY', None)
    env.pop('ANTHROPIC_AUTH_TOKEN', None)
    return env


def child_env(api_base):
    """Environment for the CLI child: the key goes here and nowhere else.

    config.reload_env deliberately keeps secrets out of os.environ because the
    browser is also a child process. The same reasoning applies in reverse: the
    key is injected for this one process instead of being exported globally.
    """
    env = dict(os.environ)
    env.pop('ANTHROPIC_API_KEY', None)
    key = get_api_key()
    if key:
        env['ANTHROPIC_API_KEY'] = key
    env['XPM_API_BASE'] = api_base
    # Stop the CLI from trying to render progress spinners into a pipe.
    env['CI'] = env.get('CI', '1')
    return env


# --- running a turn --------------------------------------------------------

_current = {'process': None}
_lock = threading.Lock()


def is_running():
    process = _current['process']
    return process is not None and process.poll() is None


def stop():
    """Ask a running turn to stop. Returns True when there was one."""
    process = _current['process']
    if process is None or process.poll() is not None:
        return False
    try:
        process.terminate()
    except OSError as exc:
        logger.warning("Could not stop the agent process: %s", exc)
        return False
    logger.info("Agent run stopped by the user")
    return True


def _work_dir():
    """Run the CLI somewhere the app owns.

    Without --bare the CLI reads the working directory's .claude/ and .mcp.json.
    Pointing it at our data directory means a project folder the user happens to
    be in cannot inject hooks or extra MCP servers into the run.
    """
    work = os.path.join(paths.DATA_DIR, 'agent')
    os.makedirs(work, exist_ok=True)
    return work


def stream(prompt, auto=False, api_base='http://127.0.0.1:5000', resume=True,
           web_access=False):
    """Run one turn, yielding the CLI's stream-json events as dicts.

    Also yields a few synthetic events of our own, tagged `xpm`, for errors the
    CLI never gets a chance to report.
    """
    prompt = (prompt or '').strip()
    if not prompt:
        raise AgentError('Nothing to send.')

    with _lock:
        if is_running():
            raise AgentError('The assistant is already working on something. '
                             'Wait for it to finish, or stop it.')
        session_id = load_session_id() if resume else ''
        argv = build_command(prompt, session_id=session_id, auto=auto,
                             api_base=api_base, web_access=web_access)

        logger.info("Agent turn starting (auto=%s, web=%s, resume=%s, auth=%s)",
                    auto, web_access, bool(session_id), auth_mode())
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=_work_dir(),
                env=child_env(api_base),
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                **_no_window(),
            )
        except OSError as exc:
            raise AgentError(f'Could not start Claude Code: {exc}')
        _current['process'] = process

    stderr_chunks = []

    def drain_stderr():
        try:
            for line in process.stderr:
                stderr_chunks.append(line)
                logger.debug("claude stderr: %s", line.rstrip())
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    saw_result = False
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                # Not every line is guaranteed to be JSON; surface it rather
                # than dropping it silently.
                logger.debug("Non-JSON line from claude: %s", line[:200])
                continue

            if isinstance(event, dict):
                new_id = event.get('session_id')
                if new_id and new_id != session_id:
                    save_session_id(new_id)
                    session_id = new_id
                if event.get('type') == 'result':
                    saw_result = True
            yield event
    finally:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
        code = process.wait()
        stderr_thread.join(timeout=2)
        _current['process'] = None

        if not saw_result:
            detail = ''.join(stderr_chunks).strip()
            yield {
                'type': 'xpm',
                'subtype': 'failed',
                'exit_code': code,
                'error': _explain_failure(code, detail),
            }
        logger.info("Agent turn finished (exit %s)", code)


def _explain_failure(code, stderr_text):
    """Turn an exit code plus stderr into something a user can act on."""
    text = (stderr_text or '').strip()
    lowered = text.lower()
    if code == 143:
        return 'Stopped.'
    if 'not logged in' in lowered or 'authentication' in lowered or 'unauthorized' in lowered:
        if has_api_key():
            return ('Claude Code rejected the API key. Check it in Settings, or '
                    'remove it to use the login of the Claude Code on this machine.')
        return ('Claude Code is not signed in. Run "claude" once in a terminal and '
                'sign in, or add an API key in Settings.')
    if 'unknown option' in lowered or 'unknown argument' in lowered:
        return ('This version of Claude Code does not accept one of the options the '
                'app uses. Update it with "npm install -g @anthropic-ai/claude-code".')
    if text:
        return text[-600:]
    return f'Claude Code exited with code {code} and said nothing.'
