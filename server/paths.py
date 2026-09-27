"""Centralized path management for dev and PyInstaller bundle modes."""

import os
import sys


def get_base_dir():
    """Persistent data directory (data, logs, .env).

    Set XPM_HOME to keep the data somewhere else (also used by the test suite).
    In a PyInstaller bundle on Windows, this is the folder containing the .exe.
    In a macOS .app bundle, this is the folder containing the .app.
    In development, this is the project root (one level up from server/).
    """
    override = os.environ.get('XPM_HOME', '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))

    if getattr(sys, 'frozen', False):
        if sys.platform == 'darwin':
            # .app/Contents/MacOS/exe → go up to the folder containing the .app
            app_bundle = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
            return os.path.dirname(app_bundle)
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
