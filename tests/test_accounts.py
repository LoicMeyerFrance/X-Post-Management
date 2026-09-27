"""Tests for keeping two X accounts apart.

One installation can be pointed at a different account at any time, and the
posts, the timeline mirror and the profile cache all outlive that change. Without
scoping, switching accounts shows one account's posts while signed in as another -
and publishing a draft would send it from the wrong place.

    python tests/test_accounts.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix='xpm-accounts-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import app as appmod        # noqa: E402
import config               # noqa: E402
import database            # noqa: E402
import paths                # noqa: E402

config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'

LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def use_account(handle):
    """Point the app at an account, the way saving settings does."""
    if handle:
        os.environ['X_USERNAME'] = handle
    else:
        os.environ.pop('X_USERNAME', None)


def test_helpers():
    section('reading the account off a row')

    check('a handle is normalised', database.normalise_account(' @Alpha_User ') == 'alpha_user')
    check('None normalises to empty', database.normalise_account(None) == '')

    url = 'https://x.com/alpha_user/status/1234567890123456789'
    check('the poster is read from a tweet url',
          database.account_from_tweet_url(url) == 'alpha_user',
          database.account_from_tweet_url(url))
    check('casing is ignored',
          database.account_from_tweet_url('https://x.com/Beta_User/status/1') == 'beta_user')
    check('a url with no status has no account',
          database.account_from_tweet_url('https://x.com/alpha_user') == '')
    check('an empty url has no account', database.account_from_tweet_url('') == '')

    use_account('alpha_user')
    check('the configured account is reported', database.current_account() == 'alpha_user')
    use_account('@Beta_User')
    check('and normalised on the way out', database.current_account() == 'beta_user')


def test_posts_are_separated():
    section('posts belong to one account')

    use_account('alpha_user')
    a1 = database.create_post(text='idroh one', status='draft')
    a2 = database.create_post(text='idroh two', status='scheduled',
                              scheduled_at='2030-01-01T09:00')
    check('posts are visible to their own account',
          {p['id'] for p in database.get_all_posts()} == {a1, a2},
          [p['id'] for p in database.get_all_posts()])

    use_account('beta_user')
    check('and invisible to another account', database.get_all_posts() == [],
          database.get_all_posts())
    check('the other account sees no drafts either',
          database.get_posts_by_status('draft') == [])
    check('nor anything pending for the scheduler',
          database.get_pending_scheduled() == [])

    b1 = database.create_post(text='haaper one', status='draft')
    check('its own post is visible',
          [p['id'] for p in database.get_all_posts()] == [b1])

    # An id from the other account must not resolve, or the UI would act on
    # something it never showed.
    check('another account\'s post cannot be read by id',
          database.get_post(a1) is None, database.get_post(a1))
    check('nor updated', database.update_post(a1, text='hijacked') is False)
    check('nor deleted', database.delete_post(a1) is False)

    use_account('alpha_user')
    check('and it is still intact', database.get_post(a1)['text'] == 'idroh one',
          database.get_post(a1))
    check('the other account\'s post is hidden here too',
          database.get_post(b1) is None)

    # Each side keeps its own view after switching back and forth.
    check('alpha_user still has two posts', len(database.get_all_posts()) == 2)
    use_account('beta_user')
    check('beta_user still has one', len(database.get_all_posts()) == 1)


def test_mirror_is_separated():
    section('the X mirror belongs to one account')

    use_account('alpha_user')
    database.save_x_posts([
        {'tweet_id': '111', 'url': 'https://x.com/alpha_user/status/111',
         'text': 'from idroh', 'posted_at': '2026-09-01T10:00:00.000Z',
         'username': 'alpha_user'},
    ])
    check('the mirror holds its own tweet', database.count_x_posts() == 1)

    use_account('beta_user')
    check('the other account sees an empty mirror', database.count_x_posts() == 0)
    check('and lists nothing', database.get_x_posts() == [])

    # The same tweet id under another account is a separate row, not a clash.
    database.save_x_posts([
        {'tweet_id': '222', 'url': 'https://x.com/beta_user/status/222',
         'text': 'from haaper', 'posted_at': '2026-09-02T10:00:00.000Z',
         'username': 'beta_user'},
    ])
    check('its own tweet is stored', database.count_x_posts() == 1)
    check('and it is the right one',
          database.get_x_posts()[0]['tweet_id'] == '222')

    use_account('alpha_user')
    check('alpha_user still sees only its own', [t['tweet_id'] for t in database.get_x_posts()] == ['111'])

    # A second sync for the same account refreshes rather than duplicating.
    added, updated = database.save_x_posts([
        {'tweet_id': '111', 'url': 'https://x.com/alpha_user/status/111',
         'text': 'from idroh', 'posted_at': '2026-09-01T10:00:00.000Z',
         'username': 'alpha_user', 'likes': '5'},
    ])
    check('re-syncing refreshes', (added, updated) == (0, 1), (added, updated))
    check('the refreshed metric is stored',
          database.get_x_posts()[0]['likes'] == '5')


def test_same_tweet_on_two_accounts():
    """A repost puts the same tweet on two timelines.

    The mirror was keyed on tweet_id alone, so the second account's sync died on
    an IntegrityError and abandoned the whole read.
    """
    section('the same tweet seen by two accounts')

    shared = {'tweet_id': '555', 'url': 'https://x.com/someone/status/555',
              'text': 'a tweet both accounts can see',
              'posted_at': '2026-09-01T10:00:00.000Z', 'username': 'someone'}

    use_account('accounta')
    check('the first account stores it', database.save_x_posts([shared]) == (1, 0))

    use_account('accountb')
    try:
        result = database.save_x_posts([shared])
        check('the second account stores it too, without crashing',
              result == (1, 0), result)
    except Exception as exc:
        check('the second account stores it too, without crashing', False,
              f'{type(exc).__name__}: {exc}')
        return

    check('each account sees exactly one copy', database.count_x_posts() == 1,
          database.count_x_posts())
    use_account('accounta')
    check('and so does the first', database.count_x_posts() == 1)

    # Refreshing one must not touch the other's copy.
    database.save_x_posts([{**shared, 'likes': '99'}])
    check('refreshing one account leaves its own row updated',
          database.get_x_posts()[0]['likes'] == '99')
    use_account('accountb')
    check("and the other account's copy is untouched",
          database.get_x_posts()[0]['likes'] == '', database.get_x_posts()[0]['likes'])


def test_orphan_adoption():
    section('rows created before a username was set')

    use_account('')
    orphan = database.create_post(text='before any account', status='draft')
    check('an unconfigured app still stores posts', orphan is not None)
    check('and can read them back', len(database.get_all_posts()) >= 1)

    use_account('newcomer')
    check('they are not visible under a named account yet',
          database.get_post(orphan) is None)

    posts, _ = database.adopt_orphan_rows('newcomer')
    check('adoption claims the orphans', posts >= 1, posts)
    check('and they are visible now', database.get_post(orphan) is not None)

    # Adoption must never take rows that already belong to someone.
    use_account('alpha_user')
    before = len(database.get_all_posts())
    database.adopt_orphan_rows('beta_user')
    check('an account already stamped is never stolen',
          len(database.get_all_posts()) == before, (before, len(database.get_all_posts())))
    check('adopting for nobody does nothing', database.adopt_orphan_rows('') == (0, 0))


def test_profile_cache_is_separated():
    section('the profile cache belongs to one account')

    a = paths.profile_info_path('alpha_user')
    b = paths.profile_info_path('beta_user')
    check('each account has its own profile file', a != b, (a, b))
    check('and its own picture',
          paths.profile_picture_name('alpha_user') != paths.profile_picture_name('beta_user'))
    check('an unset account falls back to the plain name',
          os.path.basename(paths.profile_info_path('')) == 'profile_info.json')
    # A handle must never escape the data directory.
    nasty = paths.profile_info_path('../../etc/passwd')
    check('a path cannot be escaped through the handle',
          os.path.dirname(os.path.abspath(nasty)) == os.path.abspath(paths.DATA_DIR), nasty)

    # The verification badge drives the character limit, so a stale cache would
    # silently cap a Premium account at 280.
    use_account('alpha_user')
    with open(appmod.profile_info_path(), 'w', encoding='utf-8') as handle:
        json.dump({'username': 'alpha_user', 'is_verified': True}, handle)
    check('the verified account gets the long limit', appmod.char_limit() == 25000,
          appmod.char_limit())

    use_account('beta_user')
    check('the other account is not treated as verified',
          appmod.char_limit() == 280, appmod.char_limit())


def test_legacy_migration():
    section('migrating a database written before scoping')

    legacy_home = tempfile.mkdtemp(prefix='xpm-legacy-')
    db_path = os.path.join(legacy_home, 'data', 'posts.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # A pre-scoping posts table: no account column.
    conn = sqlite3.connect(db_path)
    conn.execute('''CREATE TABLE posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT DEFAULT '',
        image_path TEXT DEFAULT '', scheduled_at TEXT,
        status TEXT DEFAULT 'draft', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, posted_at TEXT, error_message TEXT DEFAULT '',
        retries_count INTEGER DEFAULT 0, tweet_url TEXT DEFAULT '')''')
    conn.execute("INSERT INTO posts (text, created_at, updated_at, status, tweet_url) "
                 "VALUES ('published by idroh', '2026-01-01', '2026-01-01', 'posted', "
                 "'https://x.com/alpha_user/status/999')")
    conn.execute("INSERT INTO posts (text, created_at, updated_at, status, tweet_url) "
                 "VALUES ('a draft, no url', '2026-01-02', '2026-01-02', 'draft', '')")
    conn.commit()
    conn.close()

    # Re-import the modules against the legacy home so init_db migrates it.
    saved_home = os.environ['XPM_HOME']
    os.environ['XPM_HOME'] = legacy_home
    for name in ('paths', 'database', 'config', 'bot', 'agent', 'scheduler',
                 'mcp_server', 'app'):
        sys.modules.pop(name, None)
    try:
        import database as legacy_db
        use_account('beta_user')          # configured for the *new* account
        legacy_db.init_db()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = {r['text']: r['account'] for r in
                conn.execute('SELECT text, account FROM posts')}
        conn.close()
        check('a published post is credited to the handle in its tweet url',
              rows.get('published by idroh') == 'alpha_user', rows)
        check('a post with no url falls back to the configured account',
              rows.get('a draft, no url') == 'beta_user', rows)
        check('so the old account\'s published post is not claimed by the new one',
              rows.get('published by idroh') != 'beta_user', rows)
    finally:
        os.environ['XPM_HOME'] = saved_home
        for name in ('paths', 'database', 'config', 'bot', 'agent', 'scheduler',
                     'mcp_server', 'app'):
            sys.modules.pop(name, None)
        shutil.rmtree(legacy_home, ignore_errors=True)


def test_api_reports_the_switch(client):
    section('the settings API on an account switch')

    use_account('alpha_user')
    payload = {
        'X_USERNAME': 'beta_user', 'X_PASSWORD': 'irrelevant',
        'CHROME_PROFILE_DIR': '', 'CHROME_PATH': '', 'HEADLESS': 'true',
        'CHECK_INTERVAL_SECONDS': '15', 'MAX_RETRIES': '1',
    }
    r = client.post('/api/settings/env', json=payload, headers=LOCAL)
    check('saving settings succeeds', r.status_code == 200, r.status_code)
    body = r.get_json()
    check('the new account is reported', body.get('account') == 'beta_user', body)
    check('and the one it replaced', body.get('switched_from') == 'alpha_user', body)

    r = client.post('/api/settings/env', json=payload, headers=LOCAL)
    check('saving the same account again is not a switch',
          r.get_json().get('switched_from') == '', r.get_json())


def main():
    print('=' * 62)
    print('  Account separation tests')
    print('=' * 62)
    print(f'  test home: {TEST_HOME}')

    database.init_db()
    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    test_helpers()
    test_posts_are_separated()
    test_mirror_is_separated()
    test_same_tweet_on_two_accounts()
    test_orphan_adoption()
    test_profile_cache_is_separated()
    test_legacy_migration()
    test_api_reports_the_switch(client)

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
        config.delete_password()
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    sys.exit(code)
