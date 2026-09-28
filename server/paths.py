"""Centralized path management for dev and PyInstaller bundle modes."""

import os
import sys


APP_FOLDER_NAME = 'X Post Management'

# Drop a file with this name next to the executable to keep everything beside it -
# for a USB stick, or for anyone who would rather the app left no trace elsewhere.
PORTABLE_MARKER = 'portable.txt'


def app_data_dir():
    """The out-of-the-way folder this platform keeps application data in."""
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
        return os.path.join(root, APP_FOLDER_NAME)
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/' + APP_FOLDER_NAME)
    root = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(root, 'x-post-management')


def documents_dir():
    """The user's Documents folder, as Windows actually resolves it.

    Read from the registry rather than assumed to be ~/Documents: the folder is
    routinely redirected, and guessing would put the data somewhere the user
    never sees.
    """
    if sys.platform == 'win32':
        try:
            import winreg
            key = r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value = winreg.QueryValueEx(handle, 'Personal')[0]
            if value:
                return os.path.expandvars(value)
        except Exception:
            pass
    return os.path.join(os.path.expanduser('~'), 'Documents')


def _is_cloud_synced(path):
    """Is this folder inside OneDrive, or another sync client's tree?

    A synced folder is the wrong home for a live SQLite database and a browser
    profile: the sync client copies files out from under the app, which means
    conflict copies, gigabytes of upload, and a database that can be corrupted
    mid-write. Worth avoiding even though it will not happen to everyone.
    """
    # Lowercased explicitly: os.path.normcase only does that on Windows, so
    # relying on it left every marker below unmatched on macOS - where iCloud's
    # "Library/Mobile Documents" is exactly the case this has to catch.
    normalised = os.path.abspath(path).lower()

    onedrive = os.environ.get('OneDrive') or os.environ.get('OneDriveConsumer')
    if onedrive and normalised.startswith(os.path.abspath(onedrive).lower()):
        return True

    markers = ('onedrive', 'dropbox', 'google drive', 'icloud', 'mobile documents',
               'creative cloud files')
    # Both separators, whatever this platform uses: a marker can be a whole path
    # component ("Mobile Documents" is two words).
    haystack = normalised.replace('\\', ' ').replace('/', ' ')
    return any(marker in haystack for marker in markers)


def standard_data_dir():
    """Where a fresh install should keep its data.

    Documents when it is a plain local folder: it is somewhere the user can find,
    back up and delete on purpose, which the application-data folder is not. When
    Documents is synced to the cloud, the application-data folder instead - being
    findable is not worth a corrupted database.
    """
    documents = documents_dir()
    if os.path.isdir(documents) and not _is_cloud_synced(documents):
        return os.path.join(documents, APP_FOLDER_NAME)
    return app_data_dir()


def executable_dir():
    """The folder holding the executable, or the .app bundle on macOS."""
    if sys.platform == 'darwin':
        # .app/Contents/MacOS/exe -> the folder containing the .app
        app_bundle = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
        return os.path.dirname(app_bundle)
    return os.path.dirname(sys.executable)


def _looks_like_an_install(folder):
    """Has this folder been used as a data directory before?"""
    return (os.path.isdir(os.path.join(folder, 'data'))
            or os.path.isfile(os.path.join(folder, '.env')))


def get_base_dir():
    """Persistent data directory (data, logs, .env).

    A packaged app used to write all of that beside its executable, which meant a
    few hundred megabytes of browser profile landing in whatever folder the user
    happened to run it from - usually Downloads - and an app that could not write
    at all once the executable sat in Program Files.

    So a frozen build now keeps its data where the platform expects, with two
    exceptions that matter more than tidiness:

      * an install that already has data beside the executable keeps using it,
        because silently moving to a new folder looks exactly like losing every
        post;
      * a `portable.txt` next to the executable forces the old behaviour.

    XPM_HOME still overrides everything, and is what the test suite uses.
    """
    override = os.environ.get('XPM_HOME', '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))

    if not getattr(sys, 'frozen', False):
        # Development: the project directory, as before.
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    exe_dir = executable_dir()
    if os.environ.get('XPM_PORTABLE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return exe_dir
    if os.path.isfile(os.path.join(exe_dir, PORTABLE_MARKER)):
        return exe_dir
    if _looks_like_an_install(exe_dir):
        return exe_dir
    return standard_data_dir()


def describe_location():
    """Where the data is and why, for the interface to show."""
    base = BASE_DIR
    if os.environ.get('XPM_HOME', '').strip():
        return {'path': base, 'kind': 'override'}
    if not getattr(sys, 'frozen', False):
        return {'path': base, 'kind': 'development'}
    exe_dir = executable_dir()
    if os.path.normcase(base) != os.path.normcase(exe_dir):
        return {'path': base, 'kind': 'standard'}
    if (os.environ.get('XPM_PORTABLE', '').strip()
            or os.path.isfile(os.path.join(exe_dir, PORTABLE_MARKER))):
        return {'path': base, 'kind': 'portable'}
    # Beside the executable because it was already there.
    return {'path': base, 'kind': 'legacy', 'standard_path': standard_data_dir()}


def get_resource_dir():
    """Read-only resources bundled with PyInstaller (frontend assets).

    In a PyInstaller bundle, this is sys._MEIPASS (temp extraction folder).
    In development, this is the project root (one level up from server/).
    """
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()

FRONTEND_DIR = os.path.join(RESOURCE_DIR, 'ui', 'dist')

DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'posts.db')

LOG_DIR = os.path.join(BASE_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'app.log')


# --- per-account caches ----------------------------------------------------
#
# The profile name, avatar and verification badge belong to one X account, and
# the app can be pointed at another at any time. One shared file would show the
# previous account's identity - and the badge decides the character limit, so a
# stale one silently caps a Premium account at 280.

def _file_safe(account):
    """X handles are letters, digits and underscores; anything else is dropped
    rather than trusted to be safe in a path."""
    return ''.join(c for c in str(account or '') if c.isalnum() or c == '_').lower()


def profile_info_path(account=''):
    handle = _file_safe(account)
    return os.path.join(DATA_DIR, f'profile_info_{handle}.json' if handle
                        else 'profile_info.json')


def profile_picture_name(account=''):
    handle = _file_safe(account)
    return f'profile_picture_{handle}.jpg' if handle else 'profile_picture.jpg'

# Create directories on import
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
