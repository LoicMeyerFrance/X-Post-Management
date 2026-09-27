"""Tests for reading the account's X timeline back into the app.

No browser and no network: the scroll loop is driven by a fake page that mimics
the real thing's virtualisation - articles leave the DOM as you scroll, so a
batch only ever shows a window of the timeline. That is the behaviour the loop
exists to cope with, and the only way to test it deterministically.

    python tests/test_timeline.py
"""

import os
import shutil
import sys
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix='xpm-timeline-test-')
os.environ['XPM_HOME'] = TEST_HOME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'server'))

import app as appmod        # noqa: E402
import bot                  # noqa: E402
import config               # noqa: E402
import database             # noqa: E402

config.KEYRING_SERVICE = f'X Post Management TEST {os.getpid()}'

LOCAL = {'Host': '127.0.0.1:5000', 'Origin': 'http://127.0.0.1:5000'}

passed, failed = [], []


def check(name, condition, detail=''):
    (passed if condition else failed).append(name)
    mark = '  ok  ' if condition else 'FAIL  '
    print(f'{mark}{name}' + (f'   [{detail}]' if not condition else ''))


def section(title):
    print(f'\n--- {title} ---')


def article(tweet_id, author='me', text='hello', when='2026-09-01T10:00:00.000Z',
            social='', group='', photo=False, video=False, counts=None):
    """One scraped article, in the shape the page script returns."""
    return {
        'href': f'/{author}/status/{tweet_id}',
        'datetime': when,
        'text': text,
        'socialContext': social,
        'groupLabel': group,
        'counts': counts or {},
        'hasPhoto': photo,
        'hasVideo': video,
    }


class FakePage:
    """A timeline that only ever exposes a window of itself, like the real one."""

    def __init__(self, windows):
        self.windows = list(windows)
        self.scrolls = 0
        self.waits = 0

    def evaluate(self, script):
        if 'scrollBy' in script:
            self.scrolls += 1
            return None
        # Each read returns the next window; the last one repeats, which is what
        # X does once there is nothing more to serve.
        if len(self.windows) > 1:
            return self.windows.pop(0)
        return self.windows[0] if self.windows else []

    def wait_for_timeout(self, _ms):
        self.waits += 1


def test_views_parsing():
    section('view count out of the metrics label')

    check('simple French label', bot._views_from_label('12 vues') == '12')
    check('simple English label', bot._views_from_label('1,234 views') == '1234')
    check('singular', bot._views_from_label('1 vue') == '1')

    # The label lists every metric in one sentence. Collecting all the digits
    # would turn this into "215100".
    busy = "2 réponses, 1 repost, 5 j'aime, 100 vues"
    check('multi-metric French label picks only the views',
          bot._views_from_label(busy) == '100', bot._views_from_label(busy))
    busy_en = '2 replies, 1 repost, 5 likes, 3,402 views'
    check('multi-metric English label picks only the views',
          bot._views_from_label(busy_en) == '3402', bot._views_from_label(busy_en))
    check('thin spaces in a big number are stripped',
          bot._views_from_label('1 234 567 vues') == '1234567',
          bot._views_from_label('1 234 567 vues'))
    check('no views in the label', bot._views_from_label("5 j'aime") == '',
          bot._views_from_label("5 j'aime"))
    check('empty label', bot._views_from_label('') == '')
    check('None label', bot._views_from_label(None) == '')


def test_row_mapping():
    section('mapping an article to a row')

    row = bot._timeline_row(article('123456', author='me', text='Bonjour',
                                    counts={'like': '12', 'retweet': '3', 'reply': '1'},
                                    group='1 repost, 12 j\'aime, 900 vues'), 'me')
    check('id read from the permalink', row['tweet_id'] == '123456', row)
    check('url rebuilt absolute', row['url'] == 'https://x.com/me/status/123456', row['url'])
    check('text kept', row['text'] == 'Bonjour')
    check('timestamp kept', row['posted_at'] == '2026-09-01T10:00:00.000Z')
    check('likes read from the button', row['likes'] == '12', row)
    check('views read from the label', row['views'] == '900', row)
    check('own tweet is not a repost', row['is_repost'] == 0)

    # A retweet's permalink points at the original author.
    row = bot._timeline_row(article('999', author='someoneelse', social='Vous avez reposté'), 'me')
    check('someone else\'s tweet with a social context is a repost',
          row['is_repost'] == 1, row)
    check('the real author is recorded', row['username'] == 'someoneelse', row)

    # Own tweet with a social context (pinned) must not count as a repost.
    row = bot._timeline_row(article('1000', author='me', social='Épinglé'), 'me')
    check('a pinned own tweet is not a repost', row['is_repost'] == 0, row)

    row = bot._timeline_row(article('1001', text='En réponse à @someone quelque chose'), 'me')
    check('a reply is flagged', row['is_reply'] == 1, row)

    row = bot._timeline_row(article('1002', photo=True, video=True), 'me')
    check('photo flagged', row['has_photo'] == 1)
    check('video flagged', row['has_video'] == 1)

    check('an article with no permalink is skipped',
          bot._timeline_row({'href': '/me', 'datetime': None}, 'me') is None)
    check('a non-numeric id is skipped',
          bot._timeline_row({'href': '/me/status/abc'}, 'me') is None)
    check('an empty article is skipped', bot._timeline_row({}, 'me') is None)
    check('None is skipped', bot._timeline_row(None, 'me') is None)

    # Query strings and sub-paths must not leak into the id or the url.
    row = bot._timeline_row({'href': '/me/status/777/photo/1?s=20', 'datetime': '',
                             'text': '', 'counts': {}}, 'me')
    check('sub-path and query stripped from the id', row['tweet_id'] == '777', row)
    check('url keeps the path but drops the query',
          row['url'] == 'https://x.com/me/status/777/photo/1', row['url'])


def test_scroll_loop():
    section('the scroll loop against a virtualised timeline')

    # Three windows that overlap, as a real timeline does while scrolling.
    page = FakePage([
        [article('1'), article('2'), article('3')],
        [article('3'), article('4'), article('5')],
        [article('5'), article('6')],
    ])
    rows = bot._scrape_timeline(page, 'me', max_tweets=100, max_scrolls=20)
    ids = sorted(row['tweet_id'] for row in rows)
    check('every tweet across the windows is collected',
          ids == ['1', '2', '3', '4', '5', '6'], ids)
    check('overlapping windows do not duplicate',
          len(rows) == len(set(r['tweet_id'] for r in rows)), len(rows))
    check('it stopped instead of scrolling to the cap',
          page.scrolls < 20, page.scrolls)
    check('it waited between scrolls', page.waits == page.scrolls, (page.waits, page.scrolls))

    # A timeline that never grows must stop after the idle limit, not spin.
    page = FakePage([[article('1')]])
    rows = bot._scrape_timeline(page, 'me', max_tweets=100, max_scrolls=50)
    check('a static timeline yields its one tweet', len(rows) == 1, len(rows))
    check('and stops at the idle limit rather than the scroll cap',
          page.scrolls <= bot.TIMELINE_IDLE_LIMIT + 1, page.scrolls)

    check('an empty timeline is not an error',
          bot._scrape_timeline(FakePage([[]]), 'me', 100, 10) == [])

    # The ceiling means "the most recent N", even when a pinned old tweet leads.
    # A whole screenful is absorbed per round, so more than the ceiling can be
    # collected before the loop notices - and the cut has to drop the oldest.
    windows = [[
        article('900', when='2018-01-01T00:00:00.000Z'),      # pinned, comes first
        article('1', when='2026-09-01T10:00:00.000Z'),
        article('2', when='2026-09-02T10:00:00.000Z'),
        article('3', when='2026-09-03T10:00:00.000Z'),
    ]]
    rows = bot._scrape_timeline(FakePage(windows), 'me', max_tweets=3, max_scrolls=30)
    check('the ceiling is respected exactly', len(rows) == 3, len(rows))
    check('and it keeps the newest, not the first seen',
          '900' not in [r['tweet_id'] for r in rows], [r['tweet_id'] for r in rows])
    check('rows come back newest first',
          [r['posted_at'] for r in rows] == sorted((r['posted_at'] for r in rows),
                                                   reverse=True),
          [r['posted_at'] for r in rows])

    # Progress is reported so a long read can be followed in the log.
    seen = []
    bot._scrape_timeline(FakePage([[article('1')], [article('2')]]), 'me', 100, 10,
                         lambda total, step: seen.append((step, total)))
    check('progress is reported per round', len(seen) >= 2, seen)

    # A page that throws mid-read must return what it already has.
    class Exploding(FakePage):
        def evaluate(self, script):
            if 'scrollBy' in script:
                return None
            if self.scrolls >= 1:
                raise RuntimeError('page went away')
            return [article('1'), article('2')]

    rows = bot._scrape_timeline(Exploding([[]]), 'me', 100, 10)
    check('a read that throws keeps what was already collected',
          len(rows) == 2, len(rows))


def test_storage():
    section('storing the mirrored timeline')

    database.init_db()
    rows = [
        bot._timeline_row(article('111', text='premier',
                                  when='2026-09-01T10:00:00.000Z'), 'me'),
        bot._timeline_row(article('222', text='second',
                                  when='2026-09-02T10:00:00.000Z'), 'me'),
    ]
    added, updated = database.save_x_posts(rows)
    check('both rows inserted', (added, updated) == (2, 0), (added, updated))
    check('counted', database.count_x_posts() == 2, database.count_x_posts())

    # Re-syncing must refresh, not duplicate: that is what makes "new since last
    # time" mean anything.
    rows[0]['likes'] = '42'
    added, updated = database.save_x_posts(rows)
    check('a second sync adds nothing', added == 0, added)
    check('and refreshes what it already had', updated == 2, updated)
    check('still two rows', database.count_x_posts() == 2)

    stored = database.get_x_posts()
    check('newest first', [r['tweet_id'] for r in stored] == ['222', '111'],
          [r['tweet_id'] for r in stored])
    check('metrics were refreshed',
          next(r for r in stored if r['tweet_id'] == '111')['likes'] == '42', stored[0])
    check('first_seen_at is preserved across syncs',
          all(r['first_seen_at'] <= r['fetched_at'] for r in stored),
          [(r['first_seen_at'], r['fetched_at']) for r in stored])

    check('a row with no id is ignored',
          database.save_x_posts([{'text': 'no id'}]) == (0, 0))

    # A tweet the app itself published is linked back to its post.
    post_id = database.create_post(text='mine', status='draft')
    database.update_post(post_id, tweet_url='https://x.com/me/status/222')
    stored = database.get_x_posts()
    linked = next(r for r in stored if r['tweet_id'] == '222')
    other = next(r for r in stored if r['tweet_id'] == '111')
    check('a tweet sent by the app is linked to its post',
          linked['app_post_id'] == post_id, linked['app_post_id'])
    check('a tweet sent from elsewhere has no post',
          other['app_post_id'] is None, other['app_post_id'])

    # The link must survive a url with a query string or a sub-path.
    database.update_post(post_id, tweet_url='https://x.com/me/status/222?s=20')
    linked = next(r for r in database.get_x_posts() if r['tweet_id'] == '222')
    check('the link survives a query string on the stored url',
          linked['app_post_id'] == post_id, linked['app_post_id'])

    database.delete_post(post_id)


def test_endpoints(client):
    section('the history API')

    original = bot.fetch_timeline
    try:
        calls = []

        def fake_fetch(max_tweets=None, **kwargs):
            calls.append(max_tweets)
            return {'success': True, 'tweets': [
                bot._timeline_row(article('555', text='depuis le téléphone',
                                          when='2026-09-05T08:00:00.000Z'), 'me'),
            ]}

        bot.fetch_timeline = fake_fetch

        r = client.post('/api/history/x/sync', json={}, headers=LOCAL)
        check('sync answers 200', r.status_code == 200, r.status_code)
        body = r.get_json()
        check('it reports what it read', body.get('read') == 1, body)
        check('and what was new', body.get('added') == 1, body)
        check('the default ceiling is passed through',
              calls and calls[0] == bot.TIMELINE_MAX_TWEETS, calls)

        r = client.post('/api/history/x/sync', json={}, headers=LOCAL)
        check('a second sync adds nothing', r.get_json().get('added') == 0, r.get_json())
        check('and reports it as refreshed', r.get_json().get('updated') == 1, r.get_json())

        r = client.post('/api/history/x/sync', json={'max_tweets': 5}, headers=LOCAL)
        check('a caller may lower the ceiling', calls[-1] == 5, calls)
        r = client.post('/api/history/x/sync', json={'max_tweets': 99999}, headers=LOCAL)
        check('but never raise it past the cap',
              calls[-1] == bot.TIMELINE_MAX_TWEETS, calls)
        r = client.post('/api/history/x/sync', json={'max_tweets': 'lots'}, headers=LOCAL)
        check('a nonsense ceiling is refused', r.status_code == 400, r.status_code)

        r = client.get('/api/history/x', headers=LOCAL)
        check('the history reads back', r.status_code == 200, r.status_code)
        body = r.get_json()
        check('it contains the scraped tweet',
              any(t['tweet_id'] == '555' for t in body.get('tweets', [])), body)
        check('and reports when it was last synced', bool(body.get('last_sync')), body)

        # A failure from the browser is reported, not raised.
        bot.fetch_timeline = lambda **kwargs: {'success': False, 'error': 'not signed in'}
        r = client.post('/api/history/x/sync', json={}, headers=LOCAL)
        check('a browser failure comes back as an error message',
              r.status_code == 200 and 'not signed in' in r.get_json().get('error', ''),
              r.get_json())

        # The guards apply here too.
        r = client.post('/api/history/x/sync', headers={'Host': 'evil.com'}, json={})
        check('foreign Host blocked on sync', r.status_code == 403, r.status_code)
        r = client.get('/api/history/x', headers={**LOCAL, 'Sec-Fetch-Site': 'cross-site'})
        check('cross-site read blocked', r.status_code == 403, r.status_code)
    finally:
        bot.fetch_timeline = original


def main():
    print('=' * 62)
    print('  X timeline tests (scraping logic, storage, API)')
    print('=' * 62)
    print(f'  test home: {TEST_HOME}')

    database.init_db()
    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    test_views_parsing()
    test_row_mapping()
    test_scroll_loop()
    test_storage()
    test_endpoints(client)

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
