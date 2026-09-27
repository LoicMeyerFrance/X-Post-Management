"""Smoke tests for the local API.

Runs against Flask's test client - no browser, no network, no X account needed.
Everything lands in a throwaway directory, so your real data/ and .env are never
touched.

    python tests/test_api.py
"""

import io
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

# Point the app at a temporary home *before* importing it: paths.py reads
# XPM_HOME at import time.
TEST_HOME = tempfile.mkdtemp(prefix='xpm-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import app as appmod        # noqa: E402
import config               # noqa: E402
import database             # noqa: E402

# Requests the app's own frontend would send.
LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

PNG = b'\x89PNG\r\n\x1a\n' + b'C' * 128

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def test_security(client):
    section('security guards')

    r = client.get('/api/posts', headers={'Host': 'evil.com'})
    check('foreign Host blocked (DNS rebinding)', r.status_code == 403, r.status_code)

    r = client.post('/api/posts', headers={'Host': '127.0.0.1:5000', 'Origin': 'https://evil.com'},
                    data={'text': 'csrf'})
    check('cross-origin POST blocked (CSRF)', r.status_code == 403, r.status_code)

    r = client.post('/api/posts', headers={**LOCAL, 'Sec-Fetch-Site': 'cross-site'},
                    data={'text': 'x'})
    check('cross-site fetch blocked', r.status_code == 403, r.status_code)

    r = client.get('/api/health', headers=LOCAL)
    check('same-origin request allowed', r.status_code == 200, r.status_code)
    check('no wildcard CORS header', 'Access-Control-Allow-Origin' not in r.headers)
    check('CSP header set', 'Content-Security-Policy' in r.headers)
    check('nosniff header set', r.headers.get('X-Content-Type-Options') == 'nosniff')


def test_validation(client):
    section('input validation')

    r = client.post('/api/posts', headers=LOCAL, data={'text': 'hi', 'status': 'posted'})
    check('bogus status rejected', r.status_code == 400, r.status_code)

    r = client.post('/api/posts', headers=LOCAL,
                    data={'text': 'hi', 'status': 'scheduled', 'scheduled_at': 'not-a-date'})
    check('malformed date rejected', r.status_code == 400, r.status_code)

    r = client.post('/api/posts', headers=LOCAL, data={'status': 'draft'})
    check('post with neither text nor image rejected', r.status_code == 400, r.status_code)

    r = client.post('/api/posts', headers=LOCAL, data={'text': 'x' * 400, 'status': 'draft'})
    check('text over the character limit rejected', r.status_code == 400, r.status_code)

    before = set(os.listdir(appmod.UPLOAD_DIR))
    r = client.post('/api/posts', headers=LOCAL, data={
        'text': 'x', 'status': 'draft',
        'image': (io.BytesIO(b'<?php echo 1; ?>' + b'A' * 64), 'payload.png'),
    }, content_type='multipart/form-data')
    check('file renamed to .png but not an image rejected', r.status_code == 400, r.status_code)

    r = client.post('/api/posts', headers=LOCAL, data={
        'status': 'draft', 'image': (io.BytesIO(PNG), 'evil.exe'),
    }, content_type='multipart/form-data')
    check('disallowed extension rejected', r.status_code == 400, r.status_code)
    check('rejected uploads leave nothing on disk',
          set(os.listdir(appmod.UPLOAD_DIR)) == before)


def test_post_lifecycle(client):
    section('post lifecycle')

    r = client.post('/api/posts', headers=LOCAL, data={
        'text': 'hello world', 'status': 'draft',
        'image': (io.BytesIO(PNG), 'shot.png'),
    }, content_type='multipart/form-data')
    check('create a draft with an image', r.status_code == 201, r.data[:120])
    post_id = r.get_json()['id']

    post = client.get(f'/api/posts/{post_id}', headers=LOCAL).get_json()
    check('draft is readable', post['text'] == 'hello world')

    stored = os.path.basename(post['image_path'])
    check('upload stored under a generated name', 'shot' not in stored, stored)

    r = client.get('/uploads/' + stored, headers=LOCAL)
    check('upload is served back intact', r.status_code == 200 and r.data == PNG, r.status_code)

    r = client.get('/uploads/../posts.db', headers=LOCAL)
    check('path traversal on /uploads blocked', r.status_code == 404, r.status_code)

    r = client.put(f'/api/posts/{post_id}', headers=LOCAL,
                   json={'text': 'edited', 'status': 'draft'})
    check('update a post', r.status_code == 200, r.status_code)

    r = client.put(f'/api/posts/{post_id}', headers=LOCAL, json={'status': 'bogus'})
    check('update with an unknown status rejected', r.status_code == 400, r.status_code)

    r = client.post(f'/api/posts/{post_id}/duplicate', headers=LOCAL)
    check('duplicate a post', r.status_code == 201, r.status_code)
    dup_id = r.get_json()['id']
    dup = client.get(f'/api/posts/{dup_id}', headers=LOCAL).get_json()
    check('duplicate owns its own copy of the image',
          dup['image_path'] and dup['image_path'] != post['image_path'])

    r = client.get('/api/posts?status=draft,scheduled', headers=LOCAL)
    check('several statuses in one query', r.status_code == 200 and len(r.get_json()) >= 2,
          r.status_code)

    r = client.get('/api/posts?status=nope', headers=LOCAL)
    check('unknown status in a query rejected', r.status_code == 400, r.status_code)

    image_file = os.path.join(appmod.UPLOAD_DIR, stored)
    r = client.delete(f'/api/posts/{post_id}', headers=LOCAL)
    check('delete a post', r.status_code == 200, r.status_code)
    check('deleting a post removes its image', not os.path.isfile(image_file))
    client.delete(f'/api/posts/{dup_id}', headers=LOCAL)

    r = client.get('/api/posts/999999', headers=LOCAL)
    check('missing post returns JSON 404', r.status_code == 404 and r.is_json, r.content_type)

    r = client.get('/api/nope', headers=LOCAL)
    check('unknown API route returns JSON, not the SPA shell', r.is_json, r.content_type)


def test_settings(client):
    section('settings')

    base = {'X_USERNAME': 'loic', 'X_PASSWORD': 'secret', 'HEADLESS': 'true',
            'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
            'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''}

    r = client.post('/api/settings/env', headers=LOCAL, json={**base, 'X_USERNAME': 'my user!'})
    check('invalid username rejected', r.status_code == 400, r.data[:100])

    r = client.post('/api/settings/env', headers=LOCAL,
                    json={**base, 'CHECK_INTERVAL_SECONDS': '99999'})
    check('out-of-range interval rejected', r.status_code == 400, r.status_code)

    r = client.post('/api/settings/env', headers=LOCAL, json=base)
    check('valid settings accepted', r.status_code == 200, r.data[:100])

    values = client.get('/api/settings/env', headers=LOCAL).get_json()
    check('password never sent to the frontend in clear', values['X_PASSWORD'] == config.MASK)

    # Saving again with the mask must keep the stored password.
    client.post('/api/settings/env', headers=LOCAL, json={**base, 'X_PASSWORD': config.MASK})
    check('masked password preserved on save',
          config.read_env_file()['X_PASSWORD'] == 'secret',
          config.read_env_file().get('X_PASSWORD'))

    status = client.get('/api/settings/connection-status', headers=LOCAL)
    check('connection status is readable without opening a browser',
          status.status_code == 200 and 'connected' in status.get_json(), status.status_code)
    check('reports not connected before any sign-in',
          status.get_json()['connected'] is False, status.get_json())

    # 404 for the GET, 405 for the POST: Flask's catch-all static route claims
    # the path for GET only. Either way the endpoint is gone.
    gone = (client.get('/api/settings/check-google', headers=LOCAL).status_code,
            client.post('/api/settings/connect-google', headers=LOCAL).status_code)
    check('the old Google endpoints are gone',
          all(code in (404, 405) for code in gone), gone)

    for key in ('locale', 'theme', 'setupComplete'):
        r = client.post('/api/settings/preferences', headers=LOCAL, json={key: 'x'})
        check(f'preference {key!r} accepted', r.status_code == 200, r.status_code)

    r = client.post('/api/settings/preferences', headers=LOCAL, json={'arbitrary': 'x'})
    check('unknown preference rejected', r.status_code == 400, r.status_code)


def test_env_round_trip():
    section('.env round-trip')

    path = os.path.join(TEST_HOME, 'round-trip.env')
    tricky = {
        'X_USERNAME': 'loic',
        # Quotes, a newline that tries to inject a second key, a trailing
        # backslash: all legal in a password.
        'X_PASSWORD': 'p@ss "with" #hash\nX_USERNAME=hacked\\',
        'CHROME_PATH': r'C:\Program Files\Google\Chrome\chrome.exe',
        'CHROME_PROFILE_DIR': '', 'HEADLESS': 'false',
        'CHECK_INTERVAL_SECONDS': '30', 'MAX_RETRIES': '2',
    }
    config.write_env_file(config.validate(tricky), path)
    back = config.read_env_file(path)

    check('password with quotes and newline survives',
          back.get('X_PASSWORD') == tricky['X_PASSWORD'], repr(back.get('X_PASSWORD')))
    check('injected line did not overwrite another key',
          back.get('X_USERNAME') == 'loic', back.get('X_USERNAME'))
    check('windows path survives', back.get('CHROME_PATH') == tricky['CHROME_PATH'],
          repr(back.get('CHROME_PATH')))

    # The shipped example file must stay readable by our parser.
    example = config.read_env_file(os.path.join(ROOT, '.env.example'))
    check('.env.example parses', example.get('HEADLESS') == 'true', example)



def test_publishing_is_queued(client):
    """The browser work must not hold the HTTP request open.

    A fake stands in for the browser so nothing is sent to X.
    """
    section('publishing is queued, not blocking')

    import time
    import bot

    calls = []

    def fake_do_post(text, image_path, scheduled_at=None):
        calls.append({'text': text, 'scheduled_at': scheduled_at})
        time.sleep(0.6)                      # stand in for the browser
        if 'boom' in text:
            return {'success': False, 'error': 'X refused the post'}
        return {'success': True, 'tweet_url': 'https://x.com/u/status/1'}

    original = bot._do_post
    bot._do_post = fake_do_post
    try:
        def new_draft(text):
            r = client.post('/api/posts', headers=LOCAL, data={'text': text, 'status': 'draft'})
            return r.get_json()['id']

        def wait_for(post_id, transient, tries=80):
            for _ in range(tries):
                post = client.get(f'/api/posts/{post_id}', headers=LOCAL).get_json()
                if post['status'] != transient:
                    return post
                time.sleep(0.1)
            return post

        # --- happy path ---
        pid = new_draft('queued post')
        started = time.monotonic()
        r = client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        elapsed = time.monotonic() - started
        check('post-now answers 202 straight away',
              r.status_code == 202 and elapsed < 0.4, (r.status_code, round(elapsed, 2)))
        check('the reply says it is queued', r.get_json().get('queued') is True, r.get_json())

        post = wait_for(pid, 'posting')
        check('the worker records success', post['status'] == 'posted', post['status'])
        check('the tweet URL is stored',
              post['tweet_url'] == 'https://x.com/u/status/1', post['tweet_url'])
        check('the browser ran exactly once', len(calls) == 1, calls)
        client.delete(f'/api/posts/{pid}', headers=LOCAL)

        # --- failure path ---
        calls.clear()
        pid = new_draft('boom please')
        r = client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        check('a failing post is queued too', r.status_code == 202, r.status_code)
        post = wait_for(pid, 'posting')
        check('failure lands in the error status', post['status'] == 'error', post['status'])
        check('the error message is kept',
              post['error_message'] == 'X refused the post', post['error_message'])
        client.delete(f'/api/posts/{pid}', headers=LOCAL)

        # --- scheduling goes through the same queue ---
        calls.clear()
        future = (datetime.now() + timedelta(days=2)).replace(microsecond=0).isoformat()
        r = client.post('/api/posts', headers=LOCAL,
                        data={'text': 'later', 'status': 'scheduled', 'scheduled_at': future})
        pid = r.get_json()['id']
        r = client.post(f'/api/posts/{pid}/schedule-now', headers=LOCAL)
        check('schedule-now answers 202', r.status_code == 202, r.status_code)
        post = wait_for(pid, 'scheduling')
        check('scheduling reaches scheduled_on_x',
              post['status'] == 'scheduled_on_x', post['status'])
        check('the date was handed to the browser',
              calls and calls[0]['scheduled_at'] == future, calls)
        client.delete(f'/api/posts/{pid}', headers=LOCAL)

        # --- a post already in flight cannot be queued twice ---
        calls.clear()
        pid = new_draft('double click')
        client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        second = client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        check('a second click is refused while it is in flight',
              second.status_code == 409, second.status_code)
        wait_for(pid, 'posting')
        check('so the browser still ran only once', len(calls) == 1, calls)
        client.delete(f'/api/posts/{pid}', headers=LOCAL)
    finally:
        bot._do_post = original


def test_interrupted_posts_are_recovered(client):
    section('posts stranded by a shutdown')

    import database

    pid = client.post('/api/posts', headers=LOCAL,
                      data={'text': 'stranded', 'status': 'draft'}).get_json()['id']
    database.update_post(pid, status='posting')      # as if the app died mid-publish

    recovered = database.recover_interrupted()
    check('the stranded post is picked up', recovered >= 1, recovered)

    post = client.get(f'/api/posts/{pid}', headers=LOCAL).get_json()
    check('it is flagged instead of left spinning', post['status'] == 'error', post['status'])
    check('the message tells the user to check X',
          'Check X' in (post['error_message'] or ''), post['error_message'])
    check('it is NOT silently requeued (that would double-post)',
          post['status'] != 'scheduled')
    client.delete(f'/api/posts/{pid}', headers=LOCAL)



def test_native_window_fallback():
    """A blank white window is worse than no window.

    pywebview silently falls back to MSHTML (Internet Explorer's engine) when
    EdgeChromium is unavailable, and this interface does not run there - the
    window just comes up blank. The app must notice and use the browser.
    """
    section('native window vs. browser fallback')

    import platform
    if platform.system() != 'Windows':
        check('non-Windows always gets a native window', appmod.native_window_usable()[0])
        return

    usable, reason = appmod.native_window_usable()
    check('this machine can open a native window', usable, reason)

    original = appmod._webview2_installed
    appmod._webview2_installed = lambda: False
    try:
        usable, reason = appmod.native_window_usable()
        check('a missing WebView2 runtime is detected', not usable, reason)
        check('the reason points at the runtime', 'WebView2' in reason, reason)
        check('the reason carries the download link',
              appmod.WEBVIEW2_DOWNLOAD in reason, reason)
    finally:
        appmod._webview2_installed = original

    check('detection recovers afterwards', appmod.native_window_usable()[0])


def main():
    database.init_db()
    client = appmod.app.test_client()

    test_security(client)
    test_validation(client)
    test_post_lifecycle(client)
    test_settings(client)
    test_publishing_is_queued(client)
    test_interrupted_posts_are_recovered(client)
    test_native_window_fallback()
    test_env_round_trip()

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
