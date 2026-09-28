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
import providers

logger = logging.getLogger(__name__)

SESSION_PATH = os.path.join(paths.DATA_DIR, 'agent_session.json')
PROVIDER_PATH = os.path.join(paths.DATA_DIR, 'agent_provider.json')

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
    # Reading what the user handed over is as safe as reading their posts:
    # the server serves one folder they chose, and nothing outside it.
    'list_documents', 'read_document',
    # The account's own published history, read-only. Allowed in both modes:
    # knowing what is already on X is how it avoids repeating it.
    'list_published', 'get_stats',
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


def find_cli(provider=None):
    """Absolute path to the chosen provider's executable, or ''."""
    spec = providers.get(provider or current_provider())
    return providers.find_binary(spec['binaries'])


_version_cache = {}


def cli_version(path=None, provider=None):
    """(major, minor, patch) of the installed CLI, or None when unknown."""
    path = path or find_cli(provider)
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


def provider_list():
    """Every agent CLI: whether it is on this machine, and which version.

    Probed for all of them, not only the one in use - the point of the chooser is
    to show what the user already has before they pick, so a list that only knew
    about the current choice would be answering the wrong question.
    """
    out = []
    for provider_id in providers.ORDER:
        spec = providers.get(provider_id)
        path = providers.find_binary(spec['binaries'])
        version = cli_version(path, provider_id) if path else None
        out.append({
            'id': provider_id,
            'label': spec['label'],
            'vendor': spec['vendor'],
            'available': spec['available'],
            'unavailable_reason': spec.get('unavailable_reason', ''),
            'caveat': spec.get('caveat', ''),
            'installed': bool(path),
            'path': path,
            'version': '.'.join(str(part) for part in version) if version else '',
            'install_command': providers.install_command(provider_id),
            'docs': spec['docs'],
            'plan_note': spec['plan_note'],
        })
    return out


def status():
    """What the settings page and the chat need to know before a run."""
    provider = current_provider()
    path = find_cli(provider)
    version = cli_version(path, provider) if path else None
    mode = auth_mode()
    # Only Claude Code reports its own sign-in state; for the others an
    # installed binary is as much as can be said without spending a turn.
    auth = (auth_status() if (path and provider == providers.CLAUDE)
            else {'known': False, 'logged_in': bool(path)})
    _, install_shown = install_command()

    # Ready means a turn can actually run: the binary is there, and either the
    # CLI is signed in or a key was supplied. Reporting "ready" on the binary
    # alone sent the user to a chat that failed on its first message.
    ready = bool(path) and (auth.get('logged_in') or has_api_key())

    return {
        'provider': provider,
        'provider_label': providers.get(provider)['label'],
        'provider_chosen': provider_chosen(),
        'providers': provider_list(),
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
        'install_command': providers.install_command(provider) or install_shown,
        'install_hint': install_shown,
        'can_install': True,
    }


# --- session continuity ----------------------------------------------------

def current_provider():
    """The agent CLI the user chose. Claude Code unless they said otherwise."""
    try:
        with open(PROVIDER_PATH, 'r', encoding='utf-8') as handle:
            return providers.normalise(json.load(handle).get('provider'))
    except (OSError, ValueError, AttributeError):
        return providers.DEFAULT_PROVIDER


def provider_chosen():
    """Has the user actually picked one, or are we on the default?

    The assistant tab opens on the chooser until they have, so the first thing
    they do is decide which agent runs - rather than discovering afterwards that
    something was picked for them.
    """
    return os.path.isfile(PROVIDER_PATH)


def set_provider(provider_id):
    """Choose the agent CLI. Refuses one this app will not run."""
    chosen = providers.normalise(provider_id)
    spec = providers.get(provider_id)
    if not spec['available']:
        raise AgentError(spec.get('unavailable_reason', 'That agent is not available.'))
    changed = chosen != current_provider() or not provider_chosen()
    os.makedirs(os.path.dirname(PROVIDER_PATH), exist_ok=True)
    tmp = PROVIDER_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump({'provider': chosen}, handle)
    os.replace(tmp, PROVIDER_PATH)

    # A conversation belongs to the CLI that held it - but only a real change
    # ends it. Choosing the agent already in use is a confirmation, not a
    # switch, and throwing the thread away for that would be a nasty surprise.
    if changed:
        clear_session()
    logger.info("Assistant provider set to %s%s", chosen,
                '' if changed else ' (unchanged)')
    return chosen


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

# The same two capabilities, as Gemini names them.
GEMINI_WEB_TOOLS = ('google_web_search', 'web_fetch')


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
    'The user can hand you documents to work from: call list_documents to see '
    'what they gave you before saying you have nothing, and read_document to '
    'read one. You cannot see anything else on their computer. '
    'You can also read what is already on X for this account, going back years '
    'and including posts made elsewhere: list_published for the posts '
    'themselves, get_stats for how they did. Check there before claiming '
    'something is new, and use it when asked what worked. Those figures come '
    'from the profile page, which shows a count for some posts and not others, '
    'so say what you are averaging over rather than implying it covers '
    'everything. '
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
    'straight away. The app sends it: create_post is all you need, and calling '
    'publish_now or schedule_on_x for a post you just created only races with it. '
    'Use those two only for a post that already existed before this request. '
    'Treat create_post as publishing: say what you published, and do not describe '
    'anything as a draft awaiting approval. Because it is immediate, get the text '
    'right before you call it.'
)


def codex_env(api_base, auto, work_dir):
    """Environment for a Codex run: its config, not the user's."""
    env = clean_env()
    env['CODEX_HOME'] = providers.write_codex_config(
        work_dir, mcp_config(api_base, auto), auto)
    key = get_api_key()
    if key:
        # Codex reads its own variable; the app's stored key is an Anthropic one,
        # so it is deliberately not passed here.
        env.pop('OPENAI_API_KEY', None)
    return env


def _codex_command(prompt, cli_path, auto, api_base, web_access, work_dir):
    """Codex, confined as far as Codex can be.

    Its shell and file tools are core to it and have no disable switch, so this
    cannot promise what the other two do. What it can do: a read-only sandbox, so
    nothing is written anywhere; this app's MCP server and no other; and
    approval_policy = "never" in a config of the app's own, which is what lets
    the MCP tools run at all - without it Codex cancels them the moment stdin
    closes, with nobody at a terminal to approve.
    """
    tail = (AUTO_TAIL if auto else MANUAL_TAIL) + (WEB_TAIL if web_access else NO_WEB_TAIL)
    return [
        cli_path, 'exec',
        '--json',
        '--sandbox', 'read-only',
        '--skip-git-repo-check',
        '--cd', work_dir,
        SYSTEM_PROMPT + tail + '\n\n' + prompt,
    ]


def _gemini_command(prompt, cli_path, auto, api_base, web_access, work_dir):
    """Gemini CLI, confined the same way Claude Code is.

    The mechanism differs, the guarantee does not. Gemini reads
    .gemini/settings.json from the directory it runs in, so the app writes one
    into its own workspace: an empty built-in tool allowlist, and this app's MCP
    server marked trusted so its tools run without a prompt nobody is there to
    answer. The user's own ~/.gemini settings are never touched.
    """
    providers.write_gemini_settings(work_dir, mcp_config(api_base, auto), auto)

    tail = (AUTO_TAIL if auto else MANUAL_TAIL) + (WEB_TAIL if web_access else NO_WEB_TAIL)
    argv = [
        cli_path,
        '--prompt', SYSTEM_PROMPT + tail + '\n\n' + prompt,
        '--output-format', 'stream-json',
    ]
    # Name the tools rather than trusting the mode: --allowed-tools skips the
    # confirmation for these and nothing else.
    allowed = list(DRAFT_TOOLS) + (list(PUBLISH_TOOLS) if auto else [])
    if web_access:
        allowed += list(GEMINI_WEB_TOOLS)
    argv += ['--allowed-tools', ','.join(allowed)]
    # Only this app's server, whatever else the user has configured.
    argv += ['--allowed-mcp-server-names', MCP_SERVER_NAME]
    return argv


def build_command(prompt, session_id='', auto=False, api_base='http://127.0.0.1:5000',
                  cli_path=None, version=None, web_access=False, provider=None,
                  work_dir=None):
    """The argv for one turn. Raises AgentError when the CLI is missing."""
    provider = providers.normalise(provider or current_provider())
    spec = providers.get(provider)
    cli_path = cli_path or find_cli(provider)
    if not cli_path:
        raise AgentError(f'{spec["label"]} is not installed. Install it with '
                         f'"{providers.install_command(provider)}", then sign in once.')
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AgentError(f'That message is too long ({len(prompt)} characters, '
                         f'maximum {MAX_PROMPT_CHARS}).')

    if provider == providers.GEMINI:
        return _gemini_command(prompt, cli_path, auto, api_base, web_access,
                               work_dir or _work_dir())
    if provider == providers.CODEX:
        return _codex_command(prompt, cli_path, auto, api_base, web_access,
                              work_dir or _work_dir())

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
           web_access=False, provider=None):
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
        provider = providers.normalise(provider or current_provider())
        # Only Claude Code resumes by id; a Gemini turn starts fresh.
        session_id = (load_session_id()
                      if (resume and provider == providers.CLAUDE) else '')
        argv = build_command(prompt, session_id=session_id, auto=auto,
                             api_base=api_base, web_access=web_access,
                             provider=provider)

        logger.info("Agent turn starting (%s, auto=%s, web=%s, resume=%s, auth=%s)",
                    provider, auto, web_access, bool(session_id), auth_mode())
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=_work_dir(),
                # Codex needs a CODEX_HOME of our own, holding the config that
                # lets its MCP calls run at all.
                env=(codex_env(api_base, auto, _work_dir())
                     if provider == providers.CODEX else child_env(api_base)),
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
    # Plain-text lines on stdout, kept rather than dropped: a CLI that fails
    # before it can stream anything explains itself in prose, and not always on
    # stderr. Gemini uses stderr and exit 41; that is one CLI and one version.
    noise_chunks = []

    def drain_stderr():
        try:
            for line in process.stderr:
                stderr_chunks.append(line)
                logger.debug("%s stderr: %s", provider, line.rstrip())
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
                # Not every line is JSON. Keep it: when the run produces no
                # result at all, this is what tells the user what went wrong.
                logger.debug("Non-JSON line from %s: %s", provider, line[:200])
                if len(noise_chunks) < 40:
                    noise_chunks.append(line)
                continue

            # Whatever the provider said, in the shape the interface reads.
            event = providers.translate_event(provider, event)
            if event is None:
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
                'error': _explain_failure(code, detail, provider,
                                          '\n'.join(noise_chunks).strip()),
            }
        logger.info("Agent turn finished (exit %s)", code)


def _explain_failure(code, stderr_text, provider_id=None, stdout_text=''):
    """Turn an exit code plus whatever was printed into something actionable.

    Named for the CLI that actually ran: telling a Gemini user to reinstall
    Claude Code sends them after the wrong thing.
    """
    if code == 143:
        return 'Stopped.'

    provider = providers.get(provider_id)
    label = provider['label']
    binary = (provider.get('binaries') or ('claude',))[0]
    install = providers.install_command(provider['id'])

    # Both streams count. The reason a CLI could not start is prose, and which
    # stream carries it is not something to rely on.
    text = '\n'.join(part for part in ((stderr_text or '').strip(),
                                       (stdout_text or '').strip()) if part)
    lowered = text.lower()

    signed_out = ('set an auth method' in lowered
                  or 'not logged in' in lowered
                  or 'please sign in' in lowered
                  or 'authentication' in lowered
                  or 'unauthorized' in lowered
                  or 'no credentials' in lowered)
    if signed_out:
        if provider['id'] == providers.CLAUDE and has_api_key():
            return (f'{label} rejected the API key. Check it in Settings, or '
                    f'remove it to use the login of the {label} on this machine.')
        return (f'{label} is not signed in. Run "{binary}" once in a terminal '
                f'and sign in, then try again.')

    if 'unknown option' in lowered or 'unknown argument' in lowered:
        return (f'This version of {label} does not accept one of the options '
                f'the app uses. Update it with "{install}".')

    if code != 0 and ('not found' in lowered or 'not recognized' in lowered):
        return f'{label} could not be started. Install it with "{install}".'

    if text:
        return text[-600:]
    return f'{label} exited with code {code} and said nothing.'
