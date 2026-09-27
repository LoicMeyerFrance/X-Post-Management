"""Security checks for the local API.

The app serves its interface from a web server on 127.0.0.1 with no
authentication, so any page the user visits can also reach it. These tests pin
down the guards that make that safe, plus the handling of the X password. They
run against Flask's test client in a throwaway directory - no browser, no
network, no X account.

    python tests/test_security.py
"""

import io
import os
import re
import shutil
import sys
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix='xpm-sec-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import app as appmod        # noqa: E402
import config               # noqa: E402
import database             # noqa: E402

# The credential store is shared with the real installation, and the service name
# is the only thing that separates them. Point the tests somewhere else before
# anything can write: a test must never overwrite the user's actual password.
config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'


LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}
PNG = b'\x89PNG\r\n\x1a\n' + b'C' * 128

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    print(('  ok  ' if condition else 'FAIL  ') + name + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def read_source():
    files = {}
    for name in os.listdir(os.path.join(ROOT, 'server')):
        if name.endswith('.py'):
            path = os.path.join(ROOT, 'server', name)
            files[name] = io.open(path, encoding='utf-8').read()
    return files


def strip_comments(text):
    """Drop # comments so a comment about a flag is not mistaken for the flag."""
    return '\n'.join(re.sub(r'#.*$', '', line) for line in text.splitlines())


def test_cross_origin(client):
    section('a web page must not be able to drive the app')

    probes = [
        ('a foreign Host header (DNS rebinding)', {'Host': 'evil.com'}),
        ('a cross-origin Origin (CSRF)', {**LOCAL, 'Origin': 'https://evil.com'}),
        ('Sec-Fetch-Site: cross-site', {**LOCAL, 'Sec-Fetch-Site': 'cross-site'}),
        ('Sec-Fetch-Site: same-site (other port)', {**LOCAL, 'Sec-Fetch-Site': 'same-site'}),
        ('a cross-origin Referer', {**LOCAL, 'Referer': 'https://evil.com/page'}),
    ]
    for label, headers in probes:
        r = client.post('/api/posts', headers=headers, data={'text': 'x'})
        check(f'blocked: {label}', r.status_code == 403, r.status_code)

    # Reading is guarded too: the logs and the settings are worth stealing.
    for path in ('/api/logs', '/api/settings/env', '/api/posts'):
        r = client.get(path, headers={'Host': 'evil.com'})
        check(f'blocked: reading {path} from a foreign Host', r.status_code == 403, r.status_code)

    r = client.get('/api/health', headers=LOCAL)
    check('the app\'s own frontend is allowed', r.status_code == 200, r.status_code)


def test_headers(client):
    section('response headers')

    r = client.get('/api/health', headers=LOCAL)
    check('no wildcard CORS header', 'Access-Control-Allow-Origin' not in r.headers,
          r.headers.get('Access-Control-Allow-Origin'))
    for header, expected in (('X-Content-Type-Options', 'nosniff'),
                             ('X-Frame-Options', 'DENY'),
                             ('Referrer-Policy', 'no-referrer')):
        check(f'{header}: {expected}', r.headers.get(header) == expected, r.headers.get(header))

    csp = r.headers.get('Content-Security-Policy', '')
    for directive in ("default-src 'self'", "script-src 'self'", "object-src 'none'",
                      "frame-ancestors 'none'", "base-uri 'none'", "connect-src 'self'"):
        check(f'CSP contains {directive}', directive in csp, csp[:90])

    check('API replies are not cached', r.headers.get('Cache-Control') == 'no-store',
          r.headers.get('Cache-Control'))


def test_secrets(client):
    section('the X password stays on the machine')

    settings = {'X_USERNAME': 'someone', 'X_PASSWORD': 'sup3r s3cret "pw"', 'HEADLESS': 'true',
                'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
                'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''}
    client.post('/api/settings/env', headers=LOCAL, json=settings)
    secret = settings['X_PASSWORD']

    body = client.get('/api/settings/env', headers=LOCAL).get_json()
    check('the settings endpoint masks it', body['X_PASSWORD'] == config.MASK, body['X_PASSWORD'])
    check('it is still stored correctly', config.get_password() == secret)

    for path in ('/api/settings/connection-status', '/api/profile', '/api/logs'):
        raw = client.get(path, headers=LOCAL).data.decode('utf-8', 'replace')
        check(f'{path} does not leak it', secret not in raw)

    # What matters is that no file content escapes, whatever the status code.
    for path in ('/.env', '/../.env', '/%2e%2e/.env', '/ui/../.env',
                 '/assets/../../../.env', '/data/posts.db'):
        raw = client.get(path, headers=LOCAL).data
        check(f'{path} serves no file content',
              b'X_PASSWORD' not in raw and b'X_USERNAME' not in raw and secret.encode() not in raw)

    check('a missing file is a real 404, not the SPA shell',
          client.get('/.env', headers=LOCAL).status_code == 404,
          client.get('/.env', headers=LOCAL).status_code)
    # Only meaningful once the frontend has been built; CI runs the tests first.
    if os.path.isfile(os.path.join(appmod.FRONTEND_DIR, 'index.html')):
        check('a client-side route still gets the SPA shell',
              client.get('/schedule', headers=LOCAL).status_code == 200,
              client.get('/schedule', headers=LOCAL).status_code)
    else:
        route = client.get('/schedule', headers=LOCAL)
        check('without a build, a client-side route says so rather than leaking',
              route.status_code == 404 and route.is_json, route.status_code)


def test_uploads(client):
    section('uploads')

    r = client.get('/uploads/../posts.db', headers=LOCAL)
    check('path traversal is refused', r.status_code == 404, r.status_code)

    r = client.post('/api/posts', headers=LOCAL, data={
        'text': 'x', 'status': 'draft',
        'image': (io.BytesIO(b'<?php echo 1; ?>' + b'A' * 40), 'shell.png'),
    }, content_type='multipart/form-data')
    check('content is checked, not just the extension', r.status_code == 400, r.status_code)

    r = client.post('/api/posts', headers=LOCAL, data={
        'status': 'draft', 'image': (io.BytesIO(PNG), 'x.svg'),
    }, content_type='multipart/form-data')
    check('svg is not an accepted upload type', r.status_code == 400, r.status_code)

    huge = b'\x89PNG\r\n\x1a\n' + b'A' * (9 * 1024 * 1024)
    r = client.post('/api/posts', headers=LOCAL, data={
        'status': 'draft', 'image': (io.BytesIO(huge), 'big.png'),
    }, content_type='multipart/form-data')
    check('an oversized body is refused', r.status_code in (400, 413), r.status_code)


def test_input_validation(client):
    section('input validation')

    r = client.get("/api/posts?status=' OR 1=1--", headers=LOCAL)
    check('a SQL-shaped status is refused', r.status_code == 400, r.status_code)

    r = client.post('/api/settings/env', headers=LOCAL,
                    json={'X_USERNAME': 'a\nX_PASSWORD=stolen', 'X_PASSWORD': 'p',
                          'HEADLESS': 'true', 'CHECK_INTERVAL_SECONDS': '15',
                          'MAX_RETRIES': '1', 'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''})
    check('a newline cannot be injected into .env', r.status_code == 400, r.status_code)

    r = client.post('/api/settings/preferences', headers=LOCAL, json={'../../evil': 'x'})
    check('unknown preference keys are refused', r.status_code == 400, r.status_code)

    r = client.put('/api/posts/1', headers=LOCAL, json={'status': 'posted'})
    check('a client cannot force the posted status',
          r.status_code in (400, 404), r.status_code)


def test_account_identity():
    """The app must never act under an account other than the configured one.

    The browser profile is persistent, so a session for a previous account
    survives a credential change. Treating "somebody is signed in" as "the right
    person is signed in" means publishing under the wrong identity - and the app
    reporting the configured handle while doing it.
    """
    section('which account is signed in')

    import bot

    check('a handle is normalised for comparison',
          bot._normalise_handle('  @Alpha_User ') == 'alpha_user',
          bot._normalise_handle('  @Alpha_User '))
    check('an empty handle normalises to empty', bot._normalise_handle(None) == '')

    class FakePage:
        def __init__(self, handle_result):
            self.handle_result = handle_result

        def evaluate(self, _script):
            if isinstance(self.handle_result, Exception):
                raise self.handle_result
            return self.handle_result

    check('the handle is read from the page',
          bot._current_handle(FakePage('alpha_user')) == 'alpha_user')
    check('a leading @ and casing are ignored',
          bot._current_handle(FakePage('@Beta_User')) == 'beta_user')
    check('an unreadable page gives no handle, not a crash',
          bot._current_handle(FakePage(RuntimeError('detached'))) == '')
    check('a blank result gives no handle', bot._current_handle(FakePage('')) == '')

    # Signing out must never clear cookies wholesale: CHROME_PROFILE_DIR can
    # point at the user's real Chrome profile.
    source = open(os.path.join(ROOT, 'server', 'bot.py'), encoding='utf-8').read()
    signout = source[source.index('def _sign_out'):source.index('def _login')]
    check('sign-out clears cookies per domain', 'clear_cookies(domain=' in signout, signout[:80])
    check('sign-out never clears every cookie',
          'clear_cookies()' not in signout, signout[:80])
    check('sign-out refuses rather than clearing everything on old Playwright',
          'except TypeError' in signout and 'return False' in signout)

    # And the login path has to consult the handle before trusting the session.
    login = source[source.index('def _login'):source.index('def _do_post')]
    check('login compares the signed-in handle with the configured one',
          '_current_handle(page)' in login and '!= wanted' in login, login[:200])
    check('login signs out when they differ', '_sign_out(page)' in login)
    check('login reports the account it actually found',
          'signed in as @' in login or 'not @' in login)


def test_source():
    section('settings that must not come back')

    files = read_source()
    code = strip_comments('\n'.join(files.values()))

    check('the browser sandbox is not disabled', '--no-sandbox' not in code)
    check('TLS errors are not ignored',
          'ignore_https_errors=True' not in code and "'ignore_https_errors': True" not in code)
    check('Flask debug mode is off', 'debug=True' not in code)
    # Assert the value passed to app.run(), not a particular spelling: the host
    # lives in a variable now, and '0.0.0.0' also shows up in a version check.
    run_call = re.search(r'app\.run\((.*?)\)', files['app.py'], re.S)
    host_assign = re.search(r"^\s*host = '([\d.]+)'", files['app.py'], re.M)
    check('the server binds loopback only',
          bool(host_assign) and host_assign.group(1) == '127.0.0.1'
          and bool(run_call) and 'host=host' in run_call.group(1),
          host_assign.group(1) if host_assign else 'no host assignment')
    check('nothing binds to all interfaces',
          not re.search(r"(host\s*=\s*|run\()['\"]0\.0\.0\.0", code))
    check('a request size limit is set', 'MAX_CONTENT_LENGTH' in files['app.py'])
    check('no wildcard CORS header is emitted',
          "Access-Control-Allow-Origin'] = '*'" not in code)
    check('the user agent is not spoofed', '_USER_AGENT' not in code)

    # Every SQL string that interpolates must only interpolate our own names.
    interpolated = re.findall(r"execute(?:script)?\(f['\"](.*?)['\"]", files['database.py'])
    placeholders = {p for sql in interpolated for p in re.findall(r'\{(\w+)\}', sql)}
    check('SQL interpolates only internal identifiers',
          placeholders <= {'set_clause', 'placeholders', 'table', '_POSTS_SCHEMA'},
          placeholders)
    check('the table name is allow-listed', '_OUR_TABLES' in files['database.py'])



def test_password_storage():
    """The X password should not sit in a readable file.

    It still has to be recoverable at runtime, so this is about keeping it out of
    a file that can be copied or screenshotted - not about surviving an attacker
    who already has the user's session.
    """
    section('where the X password is kept')

    # Isolate from the real entry: the service name is what identifies it.
    real_service = config.KEYRING_SERVICE
    config.KEYRING_SERVICE = 'X Post Management TEST'
    env_path = os.path.join(TEST_HOME, 'storage.env')
    secret = 'p@ss "quoted" \\ and a\nnewline'

    try:
        store_works = config.set_password(secret, env_path)
        if not store_works:
            check('no credential store here, .env fallback is used', True,
                  'skipped: no backend')
            config.write_env_file(config.validate({
                'X_USERNAME': 'someone', 'X_PASSWORD': secret, 'HEADLESS': 'true',
                'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
                'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''}), env_path)
            check('the password is still readable back', config.get_password(env_path) == secret)
            check('and it is masked for the frontend',
                  config.public_values(env_path)['X_PASSWORD'] == config.MASK)
            return

        check('the credential store round-trips it exactly',
              config.get_password(env_path) == secret, repr(config.get_password(env_path)))

        # Saving settings must not put it back on disk.
        config.write_env_file(config.validate({
            'X_USERNAME': 'someone', 'X_PASSWORD': secret, 'HEADLESS': 'true',
            'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
            'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''}), env_path)

        on_disk = io.open(env_path, encoding='utf-8').read()
        check('.env does not contain the password', secret not in on_disk)
        check('.env does not contain a fragment of it', 'p@ss' not in on_disk)
        check('but it is still readable through the app',
              config.get_password(env_path) == secret)
        check('and still masked for the frontend',
              config.public_values(env_path)['X_PASSWORD'] == config.MASK)

        # The browser is a child process and would inherit the environment.
        config.reload_env(env_path)
        check('the password is not exported to child processes',
              'X_PASSWORD' not in os.environ, os.environ.get('X_PASSWORD'))

        # An existing install still has it in .env: it must be moved out.
        legacy = os.path.join(TEST_HOME, 'legacy.env')
        io.open(legacy, 'w', encoding='utf-8', newline='\n').write(
            'X_USERNAME="someone"\nX_PASSWORD="old plaintext"\nHEADLESS="true"\n')
        check('a legacy .env is detected as holding one', config.migrate_password(legacy))
        moved = io.open(legacy, encoding='utf-8').read()
        check('the plaintext is gone from the file', 'old plaintext' not in moved, moved[:120])
        check('the password survived the move',
              config.get_password(legacy) == 'old plaintext', config.get_password(legacy))
        check('migrating again is a no-op', not config.migrate_password(legacy))
        check('the other settings are untouched',
              config.read_env_file(legacy).get('X_USERNAME') == 'someone')
    finally:
        try:
            config.delete_password()
        except Exception:
            pass
        config.KEYRING_SERVICE = real_service
        config._keyring_usable = None


def main():
    database.init_db()
    client = appmod.app.test_client()

    test_cross_origin(client)
    test_headers(client)
    test_secrets(client)
    test_uploads(client)
    test_input_validation(client)
    test_password_storage()
    test_account_identity()
    test_source()

    print(f'\n{len(passed)} passed, {len(failed)} failed')
    if failed:
        print('failing: ' + ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
