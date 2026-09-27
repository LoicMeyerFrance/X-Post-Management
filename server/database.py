import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from paths import DB_PATH

logger = logging.getLogger(__name__)

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
        tweet_url TEXT DEFAULT ''
    )
'''

UPDATABLE_FIELDS = frozenset({
    'text', 'image_path', 'scheduled_at', 'status',
    'error_message', 'retries_count', 'posted_at', 'tweet_url',
})


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
_OUR_TABLES = ('posts', 'followers_history')


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
                                   updated_at, posted_at, error_message, retries_count, tweet_url)
                    SELECT id, text, image_path, scheduled_at, status, created_at,
                           updated_at, posted_at, error_message, retries_count,
                           COALESCE(tweet_url, '')
                    FROM posts_old;
                DROP TABLE posts_old;
                PRAGMA foreign_keys=ON;
            ''')

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

        # Indexes for the queries the UI polls every few seconds.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_scheduled_at ON posts(scheduled_at)')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_followers_user_time '
            'ON followers_history(username, recorded_at)'
        )


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


def _row_to_dict(row):
    return dict(row) if row is not None else None


def create_post(text='', image_path='', scheduled_at=None, status='draft'):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            '''INSERT INTO posts (text, image_path, scheduled_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (text, image_path, scheduled_at, status, now, now)
        )
        return cur.lastrowid


def get_post(post_id):
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
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
            f'SELECT * FROM posts WHERE status IN ({placeholders}) ORDER BY created_at DESC',
            statuses,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pending_scheduled():
    """Posts waiting to be handed to X's native scheduler."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_posts():
    with get_connection() as conn:
        rows = conn.execute('SELECT * FROM posts ORDER BY created_at DESC').fetchall()
    return [_row_to_dict(r) for r in rows]


def update_post(post_id, **kwargs):
    fields = {k: v for k, v in kwargs.items() if k in UPDATABLE_FIELDS}
    if not fields:
        return False
    if 'status' in fields and fields['status'] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {fields['status']!r}")
    fields['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f'{k} = ?' for k in fields)
    values = list(fields.values()) + [post_id]
    with get_connection() as conn:
        cur = conn.execute(f'UPDATE posts SET {set_clause} WHERE id = ?', values)
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
        cur = conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))
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
