"""Smoke tests for the local API.

Runs against Flask's test client - no browser, no network, no X account needed.
Everything lands in a throwaway directory, so your real data/ and .env are never
touched.

    python tests/test_api.py
"""

import io
import os
import re
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

# The credential store is shared with the real installation, and the service name
# is the only thing that separates them. Point the tests somewhere else before
# anything can write: a test must never overwrite the user's actual password.
config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'


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


def test_preference_keys(client):
    """Every preference the interface saves must be accepted.

    A missing key is rejected with a 400 that the frontend swallows, so the
    toggle looks like it saved and silently forgets itself on the next visit.
    """
    section('preferences the interface actually writes')

    ui_dir = os.path.join(ROOT, 'ui', 'src')
    written = set()
    for folder, _dirs, files in os.walk(ui_dir):
        for name in files:
            if not name.endswith(('.ts', '.tsx')):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8') as handle:
                text = handle.read()
            for match in re.finditer(r'savePreferences\(\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*:',
                                     text):
                written.add(match.group(1))

    check('the frontend was scanned for preference writes', bool(written), written)
    missing = sorted(written - appmod.PREFERENCE_KEYS)
    check('every key the frontend saves is allow-listed', not missing, missing)

    for key in sorted(written):
        r = client.post('/api/settings/preferences', json={key: 'true'}, headers=LOCAL)
        check(f'{key} is accepted', r.status_code == 200, r.status_code)

    r = client.post('/api/settings/preferences', json={'notAThing': 'true'}, headers=LOCAL)
    check('an unknown preference is still refused', r.status_code == 400, r.status_code)


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

    # Saving again with the mask must keep the stored password, wherever it lives.
    client.post('/api/settings/env', headers=LOCAL, json={**base, 'X_PASSWORD': config.MASK})
    check('masked password preserved on save',
          config.get_password() == 'secret', config.get_password())

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

    # The password may be in the credential store now, so ask for it properly.
    check('password with quotes and newline survives',
          config.get_password(path) == tricky['X_PASSWORD'],
          repr(config.get_password(path)))
    check('injected line did not overwrite another key',
          back.get('X_USERNAME') == 'loic', back.get('X_USERNAME'))
    # The quoting itself still has to hold for the values that do stay on disk.
    check('a value containing quotes round-trips through .env',
          back.get('CHROME_PATH') == tricky['CHROME_PATH'], repr(back.get('CHROME_PATH')))
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

        # --- and it cannot be published again once it is out ---
        # Republishing would put a second, identical tweet on the timeline. The
        # in-flight guard above does not cover this: by then the post is 'posted'.
        calls.clear()
        pid = new_draft('already out')
        client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        wait_for(pid, 'posting')
        again = client.post(f'/api/posts/{pid}/post-now', headers=LOCAL)
        check('publishing an already-published post is refused',
              again.status_code == 409, again.status_code)
        check('the refusal says to duplicate it instead',
              'uplicate' in (again.get_json() or {}).get('error', ''), again.get_json())
        check('so no second tweet went out', len(calls) == 1, calls)

        again = client.post(f'/api/posts/{pid}/schedule-now', headers=LOCAL)
        check('scheduling an already-published post is refused too',
              again.status_code == 409, again.status_code)
        check('and that sent nothing either', len(calls) == 1, calls)
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



def test_browser_mode_hint(client):
    """The dashboard hint about running the browser invisibly."""
    section('browser mode hint')

    r = client.post('/api/settings/preferences', headers=LOCAL, json={'browserHintSeen': 'true'})
    check('its dismissal can be stored', r.status_code == 200, r.status_code)
    check('and comes back on reload',
          client.get('/api/settings/preferences', headers=LOCAL)
          .get_json().get('browserHintSeen') == 'true')

    # The hint's one-click action just saves HEADLESS=true.
    base = {'X_USERNAME': 'someone', 'X_PASSWORD': config.MASK, 'HEADLESS': 'false',
            'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
            'CHROME_PATH': '', 'CHROME_PROFILE_DIR': ''}
    client.post('/api/settings/env', headers=LOCAL, json=base)
    check('the visible mode is readable', 
          client.get('/api/settings/env', headers=LOCAL).get_json()['HEADLESS'] == 'false')

    client.post('/api/settings/env', headers=LOCAL, json={**base, 'HEADLESS': 'true'})
    check('switching to invisible sticks',
          client.get('/api/settings/env', headers=LOCAL).get_json()['HEADLESS'] == 'true')
    check('the password was not lost in the process', config.get_password() != '')



def test_check_posts_on_x(client):
    """Finding posts the app still lists but X no longer has.

    A fake stands in for the browser: nothing is opened and nothing is deleted
    on X.
    """
    section('reconciling with X')

    import bot
    import database

    def make_posted(text, url):
        pid = client.post('/api/posts', headers=LOCAL,
                          data={'text': text, 'status': 'draft'}).get_json()['id']
        database.update_post(pid, status='posted', tweet_url=url)
        return pid

    alive = make_posted('still up', 'https://x.com/u/status/111')
    gone = make_posted('deleted on X', 'https://x.com/u/status/222')
    murky = make_posted('could not tell', 'https://x.com/u/status/333')

    original = bot.check_tweets
    bot.check_tweets = lambda urls: {'success': True, 'states': {
        'https://x.com/u/status/111': 'present',
        'https://x.com/u/status/222': 'missing',
        'https://x.com/u/status/333': 'unknown',
    }}
    try:
        r = client.post('/api/posts/check-on-x', headers=LOCAL)
        check('the check answers 200', r.status_code == 200, r.status_code)
        body = r.get_json()

        check('all three were looked at', body['checked'] == 3, body['checked'])
        check('only the missing one is reported',
              [m['id'] for m in body['missing']] == [gone], body['missing'])
        check('the inconclusive one is counted, not proposed',
              body['unknown'] == 1, body['unknown'])

        # The endpoint reports; it must not delete on its own.
        for pid in (alive, gone, murky):
            check(f'post {pid} still exists after the check',
                  client.get(f'/api/posts/{pid}', headers=LOCAL).status_code == 200)

        # A browser failure must not be read as "everything is gone".
        bot.check_tweets = lambda urls: {'success': False, 'error': 'browser died'}
        body = client.post('/api/posts/check-on-x', headers=LOCAL).get_json()
        check('a failed check reports the error instead of a list',
              body.get('error') == 'browser died' and 'missing' not in body, body)
    finally:
        bot.check_tweets = original
        for pid in (alive, gone, murky):
            client.delete(f'/api/posts/{pid}', headers=LOCAL)

    r = client.post('/api/posts/check-on-x', headers=LOCAL)
    check('with nothing published, it says so without opening a browser',
          r.get_json()['checked'] == 0, r.get_json())



def test_verified_detection(client):
    """Whether the account is verified decides the character limit.

    Get it wrong one way and a Premium user is stuck at 280 characters; the
    other way and X rejects the post.
    """
    section('verified account detection')

    import bot

    # The badge label is written in the interface language.
    for label, expected in (
        ('Verified account', 'blue'),
        ('Verified', 'blue'),
        ('Compte certifié', 'blue'),        # accents and lowercase: the old
        ('Compte vérifié', 'blue'),         # selectors matched neither
        ('compte certifie', 'blue'),
        ('Verified business account', 'business'),
        ('Compte entreprise vérifié', 'business'),
        ('Government account verified', 'government'),
        ('Compte gouvernemental certifié', 'government'),
    ):
        check(f'{label!r} -> {expected}', bot._badge_type(label) == expected,
              bot._badge_type(label))

    for label in ('Follow', 'Suivre', 'Profile photo', 'More', '', None):
        check(f'{label!r} is not a badge', bot._badge_type(label) == '', bot._badge_type(label))

    # X embeds data about suggested accounts on the same page.
    page = {
        'data': {'user': {'result': {'screen_name': 'me', 'is_blue_verified': False}}},
        'suggestions': [{'screen_name': 'someone_famous', 'is_blue_verified': True}],
    }
    check('a stranger\'s badge is not taken for ours',
          bot._verified_in_payload(page, 'me') is False,
          bot._verified_in_payload(page, 'me'))
    check('that stranger is still readable on their own name',
          bot._verified_in_payload(page, 'someone_famous') is True)
    check('an account absent from the payload is unknown, not false',
          bot._verified_in_payload(page, 'nobody') is None)
    check('the @ and the case do not matter',
          bot._verified_in_payload(page, '@ME') is False)
    check('a verified owner is found',
          bot._verified_in_payload({'u': {'screen_name': 'me', 'is_blue_verified': True}},
                                   'me') is True)
    check('an empty username never matches', bot._verified_in_payload(page, '') is None)

    # And the limit that hangs off it.
    import json
    import os
    # Ask the app where it keeps the cache: it is per-account now, so a
    # hardcoded name is read by nobody once a username is configured.
    info = appmod.profile_info_path()
    try:
        with open(info, 'w', encoding='utf-8') as fh:
            json.dump({'is_verified': False}, fh)
        check('an ordinary account is capped at 280', appmod.char_limit() == 280,
              appmod.char_limit())
        r = client.post('/api/posts', headers=LOCAL,
                        data={'text': 'x' * 281, 'status': 'draft'})
        check('and the server refuses a longer post', r.status_code == 400, r.status_code)

        with open(info, 'w', encoding='utf-8') as fh:
            json.dump({'is_verified': True}, fh)
        check('a verified account gets 25000', appmod.char_limit() == 25_000,
              appmod.char_limit())
        r = client.post('/api/posts', headers=LOCAL,
                        data={'text': 'x' * 281, 'status': 'draft'})
        check('and the same post is accepted', r.status_code == 201, r.status_code)
        if r.status_code == 201:
            client.delete(f"/api/posts/{r.get_json()['id']}", headers=LOCAL)
    finally:
        if os.path.isfile(info):
            os.remove(info)



# An MP4/MOV header: four bytes of box size, then "ftyp".
MP4 = bytes(4) + b'ftypisom' + b'\x00' * 64


def test_video_upload(client):
    """Videos used to be refused outright. They are accepted now, within X's limits."""
    section('video attachments')

    import bot

    check('mp4 is recognised as video', appmod.media_kind('clip.mp4', MP4) == 'video')
    check('mov is recognised as video', appmod.media_kind('clip.mov', MP4) == 'video')
    check('png is still an image', appmod.media_kind('a.png', PNG) == 'image')
    check('a png renamed .mp4 is refused', appmod.media_kind('a.mp4', PNG) is None)
    check('an mp4 renamed .png is refused', appmod.media_kind('a.png', MP4) is None)
    check('an unknown extension is refused', appmod.media_kind('a.avi', MP4) is None)

    r = client.post('/api/posts', headers=LOCAL, data={
        'text': 'with a clip', 'status': 'draft',
        'image': (io.BytesIO(MP4), 'clip.mp4'),
    }, content_type='multipart/form-data')
    check('a post with a video is created', r.status_code == 201, r.data[:120])
    post_id = r.get_json()['id']

    post = client.get(f'/api/posts/{post_id}', headers=LOCAL).get_json()
    stored = os.path.basename(post['image_path'])
    check('the video keeps its extension on disk', stored.endswith('.mp4'), stored)
    served = client.get('/uploads/' + stored, headers=LOCAL)
    check('and is served back', served.status_code == 200 and served.data == MP4,
          served.status_code)
    client.delete(f'/api/posts/{post_id}', headers=LOCAL)

    # A video may be far bigger than an image; the limits are per kind.
    over_image_limit = MP4 + b'\x00' * (appmod.MAX_IMAGE_SIZE + 1024)
    r = client.post('/api/posts', headers=LOCAL, data={
        'status': 'draft', 'image': (io.BytesIO(over_image_limit), 'big.mp4'),
    }, content_type='multipart/form-data')
    check('a video larger than the image limit is still accepted',
          r.status_code == 201, r.status_code)
    if r.status_code == 201:
        client.delete(f"/api/posts/{r.get_json()['id']}", headers=LOCAL)

    r = client.post('/api/posts', headers=LOCAL, data={
        'status': 'draft', 'image': (io.BytesIO(PNG + b'\x00' * appmod.MAX_IMAGE_SIZE),
                                     'big.png'),
    }, content_type='multipart/form-data')
    check('an image over its own limit is refused', r.status_code == 400, r.status_code)

    # A video over its own limit must be refused too. The real ceiling is 512 MB,
    # so the constant is lowered here rather than pushing half a gigabyte through.
    real_limit = appmod.MAX_VIDEO_SIZE_STANDARD
    appmod.MAX_VIDEO_SIZE_STANDARD = 64 * 1024
    try:
        too_big = MP4 + b'\x00' * (64 * 1024)
        r = client.post('/api/posts', headers=LOCAL, data={
            'status': 'draft', 'image': (io.BytesIO(too_big), 'huge.mp4'),
        }, content_type='multipart/form-data')
        check('a video over the video limit is refused', r.status_code == 400, r.status_code)
        check('and the message names the video', b'Video too large' in r.data, r.data[:90])

        r = client.post('/api/posts', headers=LOCAL, data={
            'status': 'draft', 'image': (io.BytesIO(MP4 + b'\x00' * 1024), 'ok.mp4'),
        }, content_type='multipart/form-data')
        check('one just under is accepted', r.status_code == 201, r.status_code)
        if r.status_code == 201:
            client.delete(f"/api/posts/{r.get_json()['id']}", headers=LOCAL)
    finally:
        appmod.MAX_VIDEO_SIZE_STANDARD = real_limit

    check('the documented size limits are the ones in force',
          appmod.MAX_IMAGE_SIZE == 5 * 1024 * 1024
          and appmod.MAX_VIDEO_SIZE_STANDARD == 512 * 1024 * 1024,
          (appmod.MAX_IMAGE_SIZE, appmod.MAX_VIDEO_SIZE_STANDARD))
    check('the request cap leaves room for the largest video X allows',
          appmod.MAX_REQUEST_SIZE > appmod.MAX_VIDEO_SIZE_PREMIUM)

    # The ceiling follows the account: X gives Premium far more room.
    import json as _json
    # Ask the app where it keeps the cache: it is per-account now, so a
    # hardcoded name is read by nobody once a username is configured.
    info = appmod.profile_info_path()
    try:
        with open(info, 'w', encoding='utf-8') as fh:
            _json.dump({'is_verified': False}, fh)
        check('a standard account is capped at 512 MB',
              appmod.video_size_limit() == 512 * 1024 * 1024, appmod.video_size_limit())
        with open(info, 'w', encoding='utf-8') as fh:
            _json.dump({'is_verified': True}, fh)
        check('a Premium account gets the larger ceiling X allows',
              appmod.video_size_limit() == 16 * 1024 * 1024 * 1024,
              appmod.video_size_limit())
        check('and the image limit does not move',
              appmod.MAX_IMAGE_SIZE == 5 * 1024 * 1024)
    finally:
        if os.path.isfile(info):
            os.remove(info)

    # A body declared far too large is refused from its header alone.
    r = client.post('/api/posts', headers={**LOCAL, 'Content-Length': str(20 * 1024**3)},
                    data={'status': 'draft'})
    check('an absurd Content-Length is refused early',
          r.status_code in (413, 400), r.status_code)

    # The browser side has to wait for X to transcode.
    check('videos get a far longer processing budget than images',
          bot._VIDEO_READY_TIMEOUT >= 10 * 60 and bot._IMAGE_READY_TIMEOUT <= 120,
          (bot._IMAGE_READY_TIMEOUT, bot._VIDEO_READY_TIMEOUT))
    check('the bot knows the video extensions',
          set(bot.VIDEO_EXTENSIONS) == {'.mp4', '.mov', '.m4v'}, bot.VIDEO_EXTENSIONS)


def main():
    database.init_db()
    client = appmod.app.test_client()

    test_security(client)
    test_preference_keys(client)
    test_validation(client)
    test_post_lifecycle(client)
    test_settings(client)
    test_publishing_is_queued(client)
    test_interrupted_posts_are_recovered(client)
    test_native_window_fallback()
    test_browser_mode_hint(client)
    test_check_posts_on_x(client)
    test_verified_detection(client)
    test_video_upload(client)
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
