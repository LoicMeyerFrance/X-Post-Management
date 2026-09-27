import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import config
from paths import DB_PATH

logger = logging.getLogger(__name__)


# --- account scoping -------------------------------------------------------
#
# One installation can be pointed at a different X account at any time, and the
# browser profile, the posts and the timeline mirror all outlive that change.
# Without a handle on every row, switching accounts shows one account's posts
# while signed in as another - and publishing a draft would send it from the
# wrong place. Every read is therefore scoped to the configured account, and
# every write stamps it.

def normalise_account(value):
    return str(value or '').strip().lstrip('@').lower()


def current_account():
    """The handle the app is configured for, lowercased. '' when unset."""
    return normalise_account(config.env_str('X_USERNAME', ''))


def account_from_tweet_url(url):
    """The handle in a tweet URL - the only record of who actually posted it."""
    if not url or '/status/' not in url:
        return ''
    path = url.split('://', 1)[-1]
    parts = [p for p in path.split('/') if p]
    # host / handle / status / id
    return normalise_account(parts[1]) if len(parts) > 2 else ''

VALID_STATUSES = (
    'draft', 'scheduled', 'scheduling', 'scheduled_on_x', 'posting', 'posted', 'error',
)

_STATUS_CHECK = "CHECK(status IN (" + ",".join(f"'{s}'" for s in VALID_STATUSES) + "))"

_POSTS_SCHEMA = f'''
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT DEFAULT '',
        image_path TEXT DEFAULT '',
        scheduled_at TEXT,
        status TEXT DEFAULT 'draft' {_STATUS_CHECK},
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        posted_at TEXT,
        error_message TEXT DEFAULT '',
        retries_count INTEGER DEFAULT 0,
        tweet_url TEXT DEFAULT '',
        account TEXT DEFAULT ''
    )
'''

UPDATABLE_FIELDS = frozenset({
    'text', 'image_path', 'scheduled_at', 'status',
    'error_message', 'retries_count', 'posted_at', 'tweet_url',
})

# Tweets read back from the X profile itself, including ones this app never sent.
# Deliberately a separate table from `posts`: these are a read-only mirror of what
# is on X, with no status and nothing to schedule, and mixing them into `posts`
# would put rows in front of the scheduler that it has no business touching.
#
# Keyed on (account, tweet_id), not on the tweet alone: two accounts can
# legitimately have the same tweet on their timeline - a repost - and a bare
# tweet_id key made the second sync raise IntegrityError and abandon the read.
_X_POSTS_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS x_posts (
        tweet_id TEXT NOT NULL,
        account TEXT NOT NULL DEFAULT '',
        url TEXT DEFAULT '',
        text TEXT DEFAULT '',
        posted_at TEXT,
        is_repost INTEGER DEFAULT 0,
        is_reply INTEGER DEFAULT 0,
        has_photo INTEGER DEFAULT 0,
        has_video INTEGER DEFAULT 0,
        replies TEXT DEFAULT '',
        reposts TEXT DEFAULT '',
        likes TEXT DEFAULT '',
        views TEXT DEFAULT '',
        username TEXT DEFAULT '',
        first_seen_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (account, tweet_id)
    )
'''


@contextmanager
def get_connection():
    """Yield a SQLite connection, committing on success and always closing.

    A generous busy timeout keeps the scheduler thread and HTTP requests from
    tripping over each other with "database is locked".
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=NORMAL')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return (row[0] or '') if row else ''


# PRAGMA cannot take a bound parameter, so the table name is interpolated. It
# only ever comes from this module, and the allow-list keeps it that way.
_OUR_TABLES = ('posts', 'followers_history', 'x_posts')


def _columns(conn, table):
    if table not in _OUR_TABLES:
        raise ValueError(f'Unknown table: {table!r}')
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def init_db():
    with get_connection() as conn:
        conn.execute(_POSTS_SCHEMA)

        # Migration 1: add tweet_url to databases created before it existed.
        if 'tweet_url' not in _columns(conn, 'posts'):
            conn.execute("ALTER TABLE posts ADD COLUMN tweet_url TEXT DEFAULT ''")

        if 'account' not in _columns(conn, 'posts'):
            conn.execute("ALTER TABLE posts ADD COLUMN account TEXT DEFAULT ''")

        # Migration 2: older schemas had a CHECK constraint without the
        # 'scheduling'/'scheduled_on_x' statuses. SQLite cannot alter a
        # constraint, so the table is rebuilt - carrying every column over,
        # tweet_url included (the previous version silently dropped it).
        if 'scheduling' not in _table_sql(conn, 'posts'):
            logger.info("Migrating posts table to the current status set")
            conn.executescript(f'''
                PRAGMA foreign_keys=OFF;
                ALTER TABLE posts RENAME TO posts_old;
                {_POSTS_SCHEMA};
                INSERT INTO posts (id, text, image_path, scheduled_at, status, created_at,
                                   updated_at, posted_at, error_message, retries_count,
                                   tweet_url, account)
                    SELECT id, text, image_path, scheduled_at, status, created_at,
                           updated_at, posted_at, error_message, retries_count,
                           COALESCE(tweet_url, ''), ''
                    FROM posts_old;
                DROP TABLE posts_old;
                PRAGMA foreign_keys=ON;
            ''')

        # Migration 3: give every post an owner.
        #
        # Deliberately *not* guarded on the column being new. Migration 2 rebuilds
        # the table from the current schema, so by the time this runs the column
        # can already exist and be empty - guarding on its absence skipped the
        # stamping entirely on exactly the databases that needed it. Filling in
        # the blanks is idempotent, so running it every start is harmless.
        blanks = conn.execute(
            "SELECT id, tweet_url FROM posts WHERE COALESCE(account, '') = ''"
        ).fetchall()
        if blanks:
            # A published post records who sent it in its tweet URL: the only
            # trustworthy evidence. Anything else gets whatever is configured now,
            # because there is nothing better to go on.
            stamped = 0
            for row in blanks:
                handle = account_from_tweet_url(row['tweet_url'])
                if handle:
                    conn.execute('UPDATE posts SET account = ? WHERE id = ?',
                                 (handle, row['id']))
                    stamped += 1
            fallback = current_account()
            assigned = 0
            if fallback:
                assigned = conn.execute(
                    "UPDATE posts SET account = ? WHERE COALESCE(account, '') = ''",
                    (fallback,)).rowcount
            if stamped or assigned:
                logger.info("Account scoping: %s post(s) credited from their tweet "
                            "URL, %s assigned to @%s", stamped, assigned, fallback or '?')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS followers_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                followers_count INTEGER DEFAULT 0,
                following_count INTEGER DEFAULT 0,
                recorded_at TEXT NOT NULL,
                username TEXT DEFAULT ''
            )
        ''')

        if 'username' not in _columns(conn, 'followers_history'):
            conn.execute("ALTER TABLE followers_history ADD COLUMN username TEXT DEFAULT ''")

        # Tweets read back from the X profile itself, including ones this app
        # never sent. Deliberately a separate table from `posts`: these are a
        # read-only mirror of what is on X, with no status and nothing to
        # schedule, and mixing them into `posts` would put rows in front of the
        # scheduler that it has no business touching.
        conn.execute(_X_POSTS_SCHEMA)

        # Indexes for the queries the UI polls every few seconds.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_scheduled_at ON posts(scheduled_at)')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_followers_user_time '
            'ON followers_history(username, recorded_at)'
        )
        # The mirror predates scoping too. `username` there is the tweet's author,
        # which for an own post is the account it was read from - the best
        # available answer, and correct for everything but a repost.
        if 'account' not in _columns(conn, 'x_posts'):
            conn.execute("ALTER TABLE x_posts ADD COLUMN account TEXT DEFAULT ''")
            conn.execute("UPDATE x_posts SET account = LOWER(username) "
                         "WHERE COALESCE(account, '') = '' AND COALESCE(username, '') != ''")

        # The first version keyed the mirror on tweet_id alone. Two accounts that
        # both have a tweet on their timeline then collided, and the sync died on
        # an IntegrityError partway through. SQLite cannot alter a primary key, so
        # the table is rebuilt; duplicates across accounts cannot exist yet, which
        # is what makes the copy safe.
        if 'PRIMARY KEY (account, tweet_id)' not in _table_sql(conn, 'x_posts'):
            logger.info("Rebuilding the X mirror with a per-account key")
            columns = ', '.join(('tweet_id', *X_POST_FIELDS, 'first_seen_at', 'fetched_at'))
            conn.executescript(f'''
                PRAGMA foreign_keys=OFF;
                ALTER TABLE x_posts RENAME TO x_posts_old;
                {_X_POSTS_SCHEMA};
                INSERT INTO x_posts ({columns}) SELECT {columns} FROM x_posts_old;
                DROP TABLE x_posts_old;
                PRAGMA foreign_keys=ON;
            ''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_x_posts_posted_at '
                     'ON x_posts(posted_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_x_posts_account ON x_posts(account)')


# --- the X timeline mirror -------------------------------------------------

# `account` is whose timeline the tweet was read from; `username` is the tweet's
# author, which differs on a repost. Both are needed: one scopes the mirror, the
# other says who wrote it.
X_POST_FIELDS = (
    'account', 'url', 'text', 'posted_at', 'is_repost', 'is_reply',
    'has_photo', 'has_video', 'replies', 'reposts', 'likes', 'views', 'username',
)

_X_FLAG_FIELDS = frozenset({'is_repost', 'is_reply', 'has_photo', 'has_video'})

# Built once from the constant above rather than formatted per row: the column
# list never varies, and keeping the SQL out of an f-string inside execute()
# leaves no room for a caller's string to reach a statement.
_X_INSERT_SQL = (
    'INSERT INTO x_posts (tweet_id, ' + ', '.join(X_POST_FIELDS) +
    ', first_seen_at, fetched_at) VALUES (' +
    ', '.join('?' for _ in range(len(X_POST_FIELDS) + 3)) + ')'
)

_X_UPDATE_SQL = (
    'UPDATE x_posts SET ' + ', '.join(f'{field} = ?' for field in X_POST_FIELDS) +
    ', fetched_at = ? WHERE tweet_id = ? AND account = ?'
)


def save_x_posts(rows, account=None):
    """Insert or refresh scraped tweets. Returns (added, updated).

    Keyed on the tweet id within one account, so re-running a sync is harmless:
    metrics are refreshed and first_seen_at is left as it was, which is what makes
    "new since last time" meaningful.
    """
    now = datetime.now().isoformat()
    owner = current_account() if account is None else normalise_account(account)
    added = updated = 0
    with get_connection() as conn:
        known = {r[0] for r in conn.execute(
            'SELECT tweet_id FROM x_posts WHERE account = ?', (owner,))}
        for row in rows:
            tweet_id = str(row.get('tweet_id') or '').strip()
            if not tweet_id:
                continue
            row = {**row, 'account': owner}
            values = [
                row.get(field) if row.get(field) is not None
                else (0 if field in _X_FLAG_FIELDS else '')
                for field in X_POST_FIELDS
            ]
            if tweet_id in known:
                conn.execute(_X_UPDATE_SQL, (*values, now, tweet_id, owner))
                updated += 1
            else:
                conn.execute(_X_INSERT_SQL, (tweet_id, *values, now, now))
                added += 1
    return added, updated


def tweet_id_from_url(url):
    """The numeric id in a tweet URL, or '' when there is none."""
    if not url or '/status/' not in url:
        return ''
    tail = url.split('/status/', 1)[1]
    digits = ''
    for char in tail:
        if not char.isdigit():
            break
        digits += char
    return digits


def get_x_posts(limit=500):
    """The mirrored timeline, newest first, each tagged with the app post that
    produced it when there is one.

    The tweet/post pairing is done here rather than in SQL: matching ids means
    parsing a URL, and SQLite string gymnastics on a suffix would be both
    unreadable and wrong the moment a stored URL carries a query string.
    """
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT * FROM x_posts WHERE account = ? '
            'ORDER BY posted_at DESC, tweet_id DESC LIMIT ?',
            (current_account(), limit),
        ).fetchall()
        owned = conn.execute(
            'SELECT id, tweet_url FROM posts WHERE account = ? '
            "AND COALESCE(tweet_url, '') != ''",
            (current_account(),),
        ).fetchall()

    by_tweet_id = {}
    for post in owned:
        tweet_id = tweet_id_from_url(post['tweet_url'])
        if tweet_id:
            by_tweet_id.setdefault(tweet_id, post['id'])

    out = []
    for row in rows:
        item = dict(row)
        item['app_post_id'] = by_tweet_id.get(item['tweet_id'])
        out.append(item)
    return out


def count_x_posts():
    with get_connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM x_posts WHERE account = ?',
                            (current_account(),)).fetchone()[0]


INTERRUPTED_MESSAGE = (
    'Interrupted while the app was closing. Check X before retrying - '
    'this post may already have gone out.'
)


def recover_interrupted():
    """Clear posts stranded in a transient state by a crash or a force-quit.

    'scheduling' and 'posting' are set just before the browser work begins, so a
    post still wearing one of them means the app stopped in between. Whether X
    received it is unknowable from here: putting it back in the queue risks
    publishing twice, so it is flagged for the user to check instead. Without
    this the post sits in the UI forever, and the Schedule page polls every
    three seconds for a job nobody is running.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE posts SET status = 'error', error_message = ?, updated_at = ? "
            "WHERE status IN ('scheduling', 'posting')",
            (INTERRUPTED_MESSAGE, datetime.now().isoformat()),
        )
    return cur.rowcount


def adopt_orphan_rows(account):
    """Hand rows that belong to nobody to `account`. Returns (posts, tweets).

    Covers the case where posts were created before a username was configured:
    without this they would be scoped to '' and disappear the moment the user
    filled the field in. Rows already stamped with another handle are left alone,
    so switching accounts never steals the other one's history.
    """
    owner = normalise_account(account)
    if not owner:
        return 0, 0
    with get_connection() as conn:
        posts = conn.execute(
            "UPDATE posts SET account = ? WHERE COALESCE(account, '') = ''", (owner,)
        ).rowcount
        tweets = conn.execute(
            "UPDATE x_posts SET account = ? WHERE COALESCE(account, '') = ''", (owner,)
        ).rowcount
    if posts or tweets:
        logger.info("Adopted %s orphan post(s) and %s tweet(s) into @%s",
                    posts, tweets, owner)
    return posts, tweets


def accounts_with_data():
    """Every handle that has posts or mirrored tweets, for telling the user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT account, COUNT(*) n FROM posts WHERE COALESCE(account, '') != '' "
            'GROUP BY account ORDER BY n DESC'
        ).fetchall()
    return [{'account': r['account'], 'posts': r['n']} for r in rows]


def _row_to_dict(row):
    return dict(row) if row is not None else None


def create_post(text='', image_path='', scheduled_at=None, status='draft', account=None):
    now = datetime.now().isoformat()
    owner = current_account() if account is None else normalise_account(account)
    with get_connection() as conn:
        cur = conn.execute(
            '''INSERT INTO posts (text, image_path, scheduled_at, status, created_at,
                                  updated_at, account)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (text, image_path, scheduled_at, status, now, now, owner)
        )
        return cur.lastrowid


def get_post(post_id):
    """One post, but only if it belongs to the configured account.

    Scoped like the lists: an id left over from another account must not resolve,
    or the UI would publish, edit or delete something it never showed."""
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM posts WHERE id = ? AND account = ?',
            (post_id, current_account()),
        ).fetchone()
    return _row_to_dict(row)


def get_posts_by_status(status):
    return get_posts_by_statuses([status])


def get_posts_by_statuses(statuses):
    """Fetch several statuses in one round-trip (the Schedule page needs four)."""
    statuses = [s for s in statuses if s in VALID_STATUSES]
    if not statuses:
        return []
    placeholders = ','.join('?' * len(statuses))
    with get_connection() as conn:
        rows = conn.execute(
            f'SELECT * FROM posts WHERE account = ? AND status IN ({placeholders}) '
            'ORDER BY created_at DESC',
            [current_account(), *statuses],
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pending_scheduled():
    """Posts waiting to be handed to X's native scheduler."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE account = ? AND status = 'scheduled' "
            'ORDER BY scheduled_at ASC',
            (current_account(),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_posts():
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT * FROM posts WHERE account = ? ORDER BY created_at DESC',
            (current_account(),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_post(post_id, **kwargs):
    fields = {k: v for k, v in kwargs.items() if k in UPDATABLE_FIELDS}
    if not fields:
        return False
    if 'status' in fields and fields['status'] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {fields['status']!r}")
    fields['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f'{k} = ?' for k in fields)
    values = list(fields.values()) + [post_id, current_account()]
    with get_connection() as conn:
        cur = conn.execute(
            f'UPDATE posts SET {set_clause} WHERE id = ? AND account = ?', values)
    return cur.rowcount > 0


def update_post_status(post_id, status, error_message=None):
    kwargs = {'status': status}
    if error_message is not None:
        kwargs['error_message'] = error_message
    if status == 'posted':
        kwargs['posted_at'] = datetime.now().isoformat()
    return update_post(post_id, **kwargs)


def delete_post(post_id):
    with get_connection() as conn:
        cur = conn.execute('DELETE FROM posts WHERE id = ? AND account = ?',
                           (post_id, current_account()))
    return cur.rowcount > 0


def add_follower_snapshot(followers_count, following_count, username=''):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            'INSERT INTO followers_history (followers_count, following_count, recorded_at, username)'
            ' VALUES (?, ?, ?, ?)',
            (followers_count, following_count, now, username)
        )


def get_follower_history(username=None, limit=1000):
    """Most recent snapshots first in SQL, returned oldest-first for charting."""
    with get_connection() as conn:
        if username:
            rows = conn.execute(
                'SELECT * FROM followers_history WHERE username = ?'
                ' ORDER BY recorded_at DESC LIMIT ?',
                (username, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM followers_history ORDER BY recorded_at DESC LIMIT ?',
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in reversed(rows)]
