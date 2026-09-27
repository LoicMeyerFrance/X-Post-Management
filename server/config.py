"""Configuration management: safe .env reading/writing and typed env access.

The .env file holds the X credentials, so it is written with restrictive
permissions and every value is quoted/escaped to make injection impossible.
"""

import logging
import os
import re
import stat

import paths

logger = logging.getLogger(__name__)

ENV_PATH = os.path.join(paths.BASE_DIR, '.env')

# Keys exposed through the settings API, in the order they are written.
ENV_KEYS = (
    'X_USERNAME',
    'X_PASSWORD',
    'CHROME_PROFILE_DIR',
    'CHROME_PATH',
    'HEADLESS',
    'CHECK_INTERVAL_SECONDS',
    'MAX_RETRIES',
)

# Values never returned to the frontend in clear text.
SECRET_KEYS = frozenset({'X_PASSWORD'})

MASK = '********'

DEFAULTS = {
    'HEADLESS': 'true',
    'CHECK_INTERVAL_SECONDS': '15',
    'MAX_RETRIES': '1',
}

# X usernames: 1-15 chars, letters/digits/underscore only.
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,15}$')

CHECK_INTERVAL_BOUNDS = (5, 3600)
MAX_RETRIES_BOUNDS = (0, 10)


class ConfigError(ValueError):
    """Raised when a submitted configuration value is invalid."""


# --- Typed access to the current process environment ---

def env_str(key, default=''):
    value = os.getenv(key)
    if value is None:
        return DEFAULTS.get(key, default)
    return value


def env_int(key, default, minimum=None, maximum=None):
    """Read an int from the environment, falling back to `default` when the
    value is missing or malformed so a bad .env never crashes a worker."""
    raw = os.getenv(key, '')
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        if raw:
            logger.warning("Invalid integer for %s (%r), using %s", key, raw, default)
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_bool(key, default=False):
    raw = os.getenv(key)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# --- .env file I/O ---
#
# Parsing is done here rather than with python-dotenv: its parser drops a
# double-quoted value that ends with an escaped backslash, which silently loses
# a password ending in "\". Owning both sides of the round-trip keeps any
# password intact.

_LINE_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')

_ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"', "'": "'"}


def _parse_value(raw):
    """Decode one .env value: double-quoted (escapes honoured), single-quoted
    (literal) or bare (literal, with a trailing ` #comment` stripped)."""
    raw = raw.strip()
    if not raw:
        return ''

    if raw[0] == '"':
        out = []
        index = 1
        while index < len(raw):
            char = raw[index]
            if char == '\\' and index + 1 < len(raw):
                nxt = raw[index + 1]
                out.append(_ESCAPES.get(nxt, '\\' + nxt))
                index += 2
                continue
            if char == '"':
                break
            out.append(char)
            index += 1
        return ''.join(out)

    if raw[0] == "'":
        end = raw.find("'", 1)
        return raw[1:end] if end != -1 else raw[1:]

    # Bare value: keep backslashes as typed (Windows paths) but drop a comment.
    return raw.split(' #', 1)[0].strip()


def parse_env_text(text):
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        match = _LINE_RE.match(line)
        if match:
            values[match.group(1)] = _parse_value(match.group(2))
    return values


def read_env_file(path=ENV_PATH):
    """Parse the .env file. Returns {} when it does not exist."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_env_text(handle.read())
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def _quote(value):
    """Escape a value so parse_env_text reads it back verbatim."""
    escaped = (
        str(value)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '')
    )
    return '"' + escaped + '"'


# --- the X password lives in the OS credential store, not in .env ----------
#
# It still has to be readable in clear at runtime - the automation types it into
# X - so anything running as this user can get it back. What this buys is that it
# is no longer sitting in a text file that can be copied, synced to a backup or
# caught in a screenshot. It is not protection against someone who already has
# the user's session.

KEYRING_SERVICE = 'X Post Management'
KEYRING_ACCOUNT = 'X_PASSWORD'

_keyring_usable = None          # None = not probed yet


def _keyring():
    """The keyring module, or None when it cannot be used here."""
    global _keyring_usable
    if _keyring_usable is False:
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception as exc:
        if _keyring_usable is None:
            logger.warning("No OS credential store available (%s); the password "
                           "stays in .env", exc)
        _keyring_usable = False
        return None
    if isinstance(keyring.get_keyring(), FailKeyring):
        if _keyring_usable is None:
            logger.warning("No usable credential store backend; the password stays in .env")
        _keyring_usable = False
        return None
    _keyring_usable = True
    return keyring


def get_password(path=ENV_PATH):
    """The stored X password, wherever it lives."""
    ring = _keyring()
    if ring is not None:
        try:
            value = ring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if value:
                return value
        except Exception as exc:
            logger.warning("Could not read the password from the credential store: %s", exc)
    # Either no credential store, or nothing migrated yet.
    return read_env_file(path).get('X_PASSWORD', '')


def set_password(value, path=ENV_PATH):
    """Store the password. Returns True when the credential store took it."""
    ring = _keyring()
    if ring is None:
        return False
    try:
        if value:
            ring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, value)
        else:
            delete_password()
        return True
    except Exception as exc:
        logger.warning("Could not write to the credential store: %s", exc)
        return False


def delete_password():
    ring = _keyring()
    if ring is None:
        return
    try:
        ring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception:
        pass        # nothing stored, which is the state we wanted anyway


def has_password(path=ENV_PATH):
    return bool(get_password(path))


def migrate_password(path=ENV_PATH):
    """Move a password still sitting in .env into the credential store.

    Runs at startup so existing installs are upgraded without the user doing
    anything. If there is no credential store, .env is left exactly as it was.
    """
    on_disk = read_env_file(path)
    plaintext = on_disk.get('X_PASSWORD', '')
    if not plaintext:
        return False
    if not set_password(plaintext, path):
        return False
    # Rewrite .env without the secret.
    on_disk['X_PASSWORD'] = ''
    _write_env_lines(on_disk, path)
    logger.info("Moved the X password out of .env into the OS credential store")
    return True


def _harden_permissions(path):
    """Restrict the file to the current user (best effort, POSIX only)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # Windows ACLs / exotic filesystems
        logger.debug("Could not restrict permissions on %s: %s", path, exc)


def validate(values):
    """Validate and normalise a settings payload. Raises ConfigError."""
    cleaned = {}

    username = str(values.get('X_USERNAME', '')).strip().lstrip('@')
    if username and not USERNAME_RE.match(username):
        raise ConfigError('X username must be 1-15 characters (letters, digits, underscore)')
    cleaned['X_USERNAME'] = username

    password = str(values.get('X_PASSWORD', ''))
    if '\x00' in password:
        raise ConfigError('X password contains an invalid character')
    cleaned['X_PASSWORD'] = password

    for key in ('CHROME_PROFILE_DIR', 'CHROME_PATH'):
        value = str(values.get(key, '')).strip().strip('"')
        if '\x00' in value:
            raise ConfigError(f'{key} contains an invalid character')
        cleaned[key] = value

    headless = str(values.get('HEADLESS', DEFAULTS['HEADLESS'])).strip().lower()
    if headless not in ('true', 'false', ''):
        raise ConfigError('HEADLESS must be "true" or "false"')
    cleaned['HEADLESS'] = headless or DEFAULTS['HEADLESS']

    for key, (low, high) in (
        ('CHECK_INTERVAL_SECONDS', CHECK_INTERVAL_BOUNDS),
        ('MAX_RETRIES', MAX_RETRIES_BOUNDS),
    ):
        raw = str(values.get(key, '')).strip()
        if not raw:
            cleaned[key] = DEFAULTS[key]
            continue
        try:
            number = int(raw)
        except ValueError:
            raise ConfigError(f'{key} must be a whole number')
        if not low <= number <= high:
            raise ConfigError(f'{key} must be between {low} and {high}')
        cleaned[key] = str(number)

    return cleaned


def _write_env_lines(values, path=ENV_PATH):
    """Write .env atomically, with owner-only permissions."""
    stored_in_keyring = _keyring_usable is True
    header = [
        '# X Post Management configuration.',
        '# The X password is kept in the OS credential store, not here.'
        if stored_in_keyring else
        '# Contains your X password in clear text - keep this file private.',
    ]
    lines = header + [f'{key}={_quote(values.get(key, ""))}' for key in ENV_KEYS]

    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write('\n'.join(lines) + '\n')
    _harden_permissions(tmp_path)
    os.replace(tmp_path, path)
    _harden_permissions(path)


def write_env_file(values, path=ENV_PATH):
    """Save the settings: the password to the credential store, the rest to .env."""
    values = dict(values)
    if set_password(values.get('X_PASSWORD', ''), path):
        values['X_PASSWORD'] = ''       # never written to disk
    _write_env_lines(values, path)
    reload_env(path)


def reload_env(path=ENV_PATH):
    """Reload .env into os.environ, clearing keys that were removed.

    Secrets are deliberately kept out of os.environ: the browser is launched as a
    child process and would inherit them. Read them with get_password() instead.
    """
    on_disk = read_env_file(path)
    for key in ENV_KEYS:
        if key in SECRET_KEYS:
            os.environ.pop(key, None)
        elif key in on_disk:
            os.environ[key] = on_disk[key]
        else:
            os.environ.pop(key, None)
    # Any extra keys the user added by hand are honoured too.
    for key, value in on_disk.items():
        if key not in ENV_KEYS and key not in SECRET_KEYS:
            os.environ[key] = value


def public_values(path=ENV_PATH):
    """Settings for the frontend: every key present, secrets masked."""
    on_disk = read_env_file(path)
    values = {}
    for key in ENV_KEYS:
        if key in SECRET_KEYS:
            values[key] = MASK if has_password(path) else ''
        else:
            values[key] = on_disk.get(key, '') or DEFAULTS.get(key, '')
    return values


def merge_secrets(submitted, path=ENV_PATH):
    """Replace masked secrets with the value already stored."""
    merged = dict(submitted)
    if merged.get('X_PASSWORD') in (MASK, None):
        merged['X_PASSWORD'] = get_password(path)
    return merged
