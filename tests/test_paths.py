"""Tests for where a packaged app keeps its data.

A packaged build used to write everything beside its executable, so running it
from Downloads left a few hundred megabytes of browser profile there, and an
executable in Program Files could not write at all. Getting this wrong in the
other direction is worse: an install that quietly changes folder looks exactly
like losing every post.

    python tests/test_paths.py
"""

import importlib
import os
import shutil
import sys
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix='xpm-paths-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import paths  # noqa: E402

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


class FakeFrozen:
    """Pretend to be a PyInstaller build whose executable lives in `folder`."""

    def __init__(self, folder, **env):
        self.folder = folder
        self.env = env
        self.saved_env = {}
        self.saved_argv0 = None

    def __enter__(self):
        sys.frozen = True
        # A real bundle also has _MEIPASS, and paths.py reads it at import time
        # for the frontend assets.
        sys._MEIPASS = self.folder
        self.saved_argv0 = sys.executable
        sys.executable = os.path.join(self.folder, 'X Post Management.exe')
        for key, value in self.env.items():
            self.saved_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *_):
        del sys.frozen
        del sys._MEIPASS
        sys.executable = self.saved_argv0
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_cloud_detection():
    section('recognising a synced folder')

    check('a plain Documents folder is not synced',
          not paths._is_cloud_synced(r'C:\Users\someone\Documents'))
    check('a OneDrive path is synced',
          paths._is_cloud_synced(r'C:\Users\someone\OneDrive\Documents'))
    check('a Dropbox path is synced',
          paths._is_cloud_synced(r'C:\Users\someone\Dropbox\Documents'))
    check("iCloud's mobile documents are synced",
          paths._is_cloud_synced(os.path.expanduser('~/Library/Mobile Documents/x')))

    # The markers are lower case, and os.path.normcase only lowers a path on
    # Windows - so relying on it silently disabled every check on macOS and
    # Linux, which is where iCloud lives.
    for variant in ('OneDrive', 'onedrive', 'ONEDRIVE', 'OneDRIVE'):
        check(f'{variant} is recognised whatever its case',
              paths._is_cloud_synced('/home/someone/' + variant + '/Documents'),
              variant)
    check('Mobile Documents is recognised in its real casing',
          paths._is_cloud_synced('/Users/someone/Library/Mobile Documents/Docs'))

    saved = os.environ.get('OneDrive')
    os.environ['OneDrive'] = r'D:\Sync\MyCloud'
    try:
        check('the OneDrive variable is honoured wherever it points',
              paths._is_cloud_synced(r'D:\Sync\MyCloud\Documents'))
        check('and a sibling folder is not caught by it',
              not paths._is_cloud_synced(r'D:\Sync\Other\Documents'))
    finally:
        if saved is None:
            os.environ.pop('OneDrive', None)
        else:
            os.environ['OneDrive'] = saved


def test_resolution_order():
    section('where a packaged build puts its data')

    exe_dir = tempfile.mkdtemp(prefix='xpm-exe-')
    try:
        # XPM_HOME wins over everything, which is what the test suite relies on.
        with FakeFrozen(exe_dir, XPM_HOME=TEST_HOME):
            importlib.reload(paths)
            check('an explicit XPM_HOME wins',
                  os.path.normcase(paths.get_base_dir()) == os.path.normcase(TEST_HOME),
                  paths.get_base_dir())

        # A fresh install goes to the standard place, not next to the executable.
        with FakeFrozen(exe_dir, XPM_HOME=None, XPM_PORTABLE=None):
            importlib.reload(paths)
            base = paths.get_base_dir()
            check('a fresh install does not write beside the executable',
                  os.path.normcase(base) != os.path.normcase(exe_dir), base)
            check('it uses the standard location',
                  os.path.normcase(base) == os.path.normcase(paths.standard_data_dir()), base)
            # The folder is named after the app, spelled the way each platform
            # spells such things: Linux uses the XDG lower-case-and-dashes form.
            expected_name = ('x-post-management' if sys.platform not in ('win32', 'darwin')
                             else paths.APP_FOLDER_NAME)
            check('which is named after the app', expected_name in base, (base, expected_name))
            check('and it reports itself as standard',
                  paths.describe_location()['kind'] == 'standard',
                  paths.describe_location())

        # An install that already has data beside the executable keeps it.
        os.makedirs(os.path.join(exe_dir, 'data'), exist_ok=True)
        with FakeFrozen(exe_dir, XPM_HOME=None, XPM_PORTABLE=None):
            importlib.reload(paths)
            check('an existing install is left where it is',
                  os.path.normcase(paths.get_base_dir()) == os.path.normcase(exe_dir),
                  paths.get_base_dir())
            described = paths.describe_location()
            check('and is reported as legacy', described['kind'] == 'legacy', described)
            check('with the location it could move to',
                  'standard_path' in described, described)
        shutil.rmtree(os.path.join(exe_dir, 'data'))

        # A half-configured install counts too: .env without data/.
        with open(os.path.join(exe_dir, '.env'), 'w', encoding='utf-8') as handle:
            handle.write('X_USERNAME="someone"\n')
        with FakeFrozen(exe_dir, XPM_HOME=None, XPM_PORTABLE=None):
            importlib.reload(paths)
            check('a settings file alone also keeps the old location',
                  os.path.normcase(paths.get_base_dir()) == os.path.normcase(exe_dir),
                  paths.get_base_dir())
        os.remove(os.path.join(exe_dir, '.env'))

        # Portable, by marker file and by environment variable.
        marker = os.path.join(exe_dir, paths.PORTABLE_MARKER)
        with open(marker, 'w', encoding='utf-8') as handle:
            handle.write('keep my data here\n')
        with FakeFrozen(exe_dir, XPM_HOME=None, XPM_PORTABLE=None):
            importlib.reload(paths)
            check('a portable marker forces the executable folder',
                  os.path.normcase(paths.get_base_dir()) == os.path.normcase(exe_dir),
                  paths.get_base_dir())
            check('and is reported as portable',
                  paths.describe_location()['kind'] == 'portable',
                  paths.describe_location())
        os.remove(marker)

        with FakeFrozen(exe_dir, XPM_HOME=None, XPM_PORTABLE='1'):
            importlib.reload(paths)
            check('XPM_PORTABLE does the same',
                  os.path.normcase(paths.get_base_dir()) == os.path.normcase(exe_dir),
                  paths.get_base_dir())
    finally:
        os.environ['XPM_HOME'] = TEST_HOME
        importlib.reload(paths)
        shutil.rmtree(exe_dir, ignore_errors=True)


def test_development_is_unchanged():
    section('development is untouched')

    saved = os.environ.pop('XPM_HOME', None)
    try:
        importlib.reload(paths)
        check('without XPM_HOME, development uses the project directory',
              os.path.isfile(os.path.join(paths.get_base_dir(), 'requirements.txt')),
              paths.get_base_dir())
        check('and reports itself as development',
              paths.describe_location()['kind'] == 'development')
    finally:
        if saved is not None:
            os.environ['XPM_HOME'] = saved
        importlib.reload(paths)


def test_everything_stays_inside_the_base():
    section('nothing escapes the data directory')

    importlib.reload(paths)
    base = os.path.abspath(paths.BASE_DIR)
    for name, value in (('DATA_DIR', paths.DATA_DIR), ('UPLOAD_DIR', paths.UPLOAD_DIR),
                        ('DB_PATH', paths.DB_PATH), ('LOG_DIR', paths.LOG_DIR),
                        ('LOG_FILE', paths.LOG_FILE)):
        check(f'{name} is inside the data directory',
              os.path.abspath(value).startswith(base), value)

    check('a profile file stays inside it too',
          os.path.abspath(paths.profile_info_path('someone')).startswith(base))
    # A handle is not allowed to climb out through the filename.
    nasty = paths.profile_info_path('../../../etc/passwd')
    check('and a hostile handle cannot climb out',
          os.path.dirname(os.path.abspath(nasty)) == os.path.abspath(paths.DATA_DIR),
          nasty)


def main():
    print('=' * 62)
    print('  Data location tests')
    print('=' * 62)

    test_cloud_detection()
    test_resolution_order()
    test_development_is_unchanged()
    test_everything_stays_inside_the_base()

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
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
