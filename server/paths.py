"""Centralized path management for dev and PyInstaller bundle modes."""

import os
import sys


def get_base_dir():
    """Persistent data directory (data, logs, .env).

    In a PyInstaller bundle on Windows, this is the folder containing the .exe.
    In a macOS .app bundle, this is the folder containing the .app.
    In development, this is the project root (one level up from server/).
    """
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

# Create directories on import
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
