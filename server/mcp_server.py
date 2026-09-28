"""MCP server exposing this app's posts to an agent, over stdio.

Claude Code launches this as a child process and speaks JSON-RPC 2.0 to it on
stdin/stdout. It is a thin façade over the local REST API rather than a second
copy of the app: the Flask process owns the single browser worker thread, the
scheduler and the SQLite connection, so every tool here is an HTTP call to
127.0.0.1. Importing database/bot directly would start a second scheduler and a
second worker.

Written against the MCP spec revision 2025-06-18, with the standard library
only - no `mcp` package to declare as a hidden import in the PyInstaller spec.

Two rules the transport imposes, both easy to break by accident:

  * stdout carries nothing but MCP messages. Every diagnostic goes to stderr.
  * one message per line, no embedded newlines.

The publish tools are hidden unless XPM_AGENT_ALLOW_PUBLISH is set. In manual
mode the agent can fill the calendar but cannot make anything public; the user
presses the button. A hidden tool is also refused if called anyway, because
"the model cannot see it" is not an access control.
"""

import io
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROTOCOL_VERSION = '2025-06-18'

# Versions we can speak. The client's is echoed back when we know it, otherwise
# we answer with ours and let the client decide whether to continue.
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')

SERVER_NAME = 'xpost'
SERVER_VERSION = '1.0.0'

API_BASE = os.environ.get('XPM_API_BASE', 'http://127.0.0.1:5000').rstrip('/')

# Timeout for a normal API call. Publishing returns 202 immediately - the
# browser work happens after the response - so nothing here waits on Chrome.
HTTP_TIMEOUT = 30

# JSON-RPC error codes we use (from the spec).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def allow_publish():
    return os.environ.get('XPM_AGENT_ALLOW_PUBLISH', '').strip().lower() in ('1', 'true', 'yes', 'on')


def log(message):
    """Diagnostics go to stderr: stdout belongs to the protocol.

    A windowed PyInstaller build can leave sys.stderr as None, and a broken pipe
    is not worth crashing over, so this never raises.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(f'[xpost-mcp] {message}', file=stream, flush=True)
    except (OSError, ValueError):
        pass


def _stdio_streams():
    """Text streams built on file descriptors 0 and 1, not on sys.std*.

    The frozen app is built with console=False. A windowed Windows build can
    leave sys.stdin/sys.stdout as None even when the parent process handed us
    real pipes, so the descriptors are wrapped directly. write_through keeps a
    response from sitting in a buffer while the client waits for it.
    """
    reader = io.TextIOWrapper(io.open(0, 'rb', closefd=False),
                              encoding='utf-8', errors='replace', newline='')
    writer = io.TextIOWrapper(io.open(1, 'wb', closefd=False),
                              encoding='utf-8', newline='', write_through=True)
    return reader, writer


# --- HTTP to the local API -------------------------------------------------
#
# No Origin header is sent, and the Host is loopback, so security.check_request
# lets these through exactly as it lets curl through. Nothing is relaxed here.

class ApiError(Exception):
    """An error the API reported, to be handed back to the agent verbatim."""


def _request(method, path, data=None, content_type=None):
    url = f'{API_BASE}{path}'
    headers = {'Accept': 'application/json'}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
            detail = payload.get('error') or payload.get('message') or str(payload)
        except ValueError:
            detail = raw.decode('utf-8', 'replace')[:300] or f'HTTP {exc.code}'
        raise ApiError(detail)
    except urllib.error.URLError as exc:
        raise ApiError(f'The app is not reachable at {API_BASE} ({exc.reason}). '
                       'Is X Post Management running?')
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        return {}


def api_get(path):
    return _request('GET', path)


def api_post_json(path, payload=None):
    data = json.dumps(payload or {}).encode('utf-8')
    return _request('POST', path, data=data, content_type='application/json')


def api_put_json(path, payload):
    data = json.dumps(payload).encode('utf-8')
    return _request('PUT', path, data=data, content_type='application/json')


def api_delete(path):
    return _request('DELETE', path)


def _multipart(fields, file_field=None, file_path=None):
    """Encode a multipart/form-data body. Returns (body, content_type)."""
    boundary = '----xpostmcp' + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += f'--{boundary}\r\n'.encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode('utf-8') + b'\r\n'

    if file_field and file_path:
        filename = os.path.basename(file_path)
        guessed = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        with open(file_path, 'rb') as handle:
            content = handle.read()
        out += f'--{boundary}\r\n'.encode()
        out += (f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n').encode('utf-8')
        out += f'Content-Type: {guessed}\r\n\r\n'.encode()
        out += content + b'\r\n'

    out += f'--{boundary}--\r\n'.encode()
    return bytes(out), f'multipart/form-data; boundary={boundary}'


# --- Tool implementations --------------------------------------------------

def _summarise(post):
    """The fields an agent needs, without the noise."""
    return {
        'id': post.get('id'),
        'text': post.get('text', ''),
        'status': post.get('status'),
        'scheduled_at': post.get('scheduled_at'),
        'media': os.path.basename(post.get('image_path') or '') or None,
        'error_message': post.get('error_message') or None,
        'created_at': post.get('created_at'),
    }


def tool_list_posts(args):
    status = (args.get('status') or '').strip()
    path = '/api/posts'
    if status:
        path += '?status=' + urllib.parse.quote(status, safe=',')
    posts = api_get(path)
    if not isinstance(posts, list):
        posts = []
    return {'count': len(posts), 'posts': [_summarise(p) for p in posts]}


def tool_get_post(args):
    post_id = _require_int(args, 'id')
    return _summarise(api_get(f'/api/posts/{post_id}'))


def tool_create_post(args):
    text = str(args.get('text') or '')
    media_path = (args.get('media_path') or '').strip()
    scheduled_at = (args.get('scheduled_at') or '').strip()
    status = (args.get('status') or '').strip() or ('scheduled' if scheduled_at else 'draft')

    if status not in ('draft', 'scheduled'):
        raise ApiError("status must be 'draft' or 'scheduled'")
    if not text and not media_path:
        raise ApiError('A post needs text, media, or both')

    if media_path:
        media_path = os.path.abspath(os.path.expanduser(media_path))
        if not os.path.isfile(media_path):
            raise ApiError(f'No such file: {media_path}')

    fields = {'text': text, 'status': status}
    if scheduled_at:
        fields['scheduled_at'] = scheduled_at

    body, content_type = _multipart(fields, 'image' if media_path else None, media_path or None)
    created = _request('POST', '/api/posts', data=body, content_type=content_type)

    # The note has to match the mode. It used to say "nothing is public until it
    # is published" in every case, which contradicted what automatic mode tells
    # the agent - so it believed the tool, called publish_now on a post the app
    # was already sending, and had to explain a 409 to the user.
    if allow_publish():
        note = ('Saved, and the app publishes it by itself in automatic mode. '
                'Do NOT call publish_now or schedule_on_x for this post.')
    else:
        note = ('Saved in the app. Nothing is public until the user approves it '
                'in the conversation.')
    return {
        'id': created.get('id'),
        'status': created.get('status'),
        'note': note,
    }


def tool_update_post(args):
    post_id = _require_int(args, 'id')
    payload = {}
    if 'text' in args and args['text'] is not None:
        payload['text'] = str(args['text'])
    if 'scheduled_at' in args and args['scheduled_at'] is not None:
        payload['scheduled_at'] = str(args['scheduled_at'])
    if 'status' in args and args['status']:
        status = str(args['status'])
        if status not in ('draft', 'scheduled'):
            raise ApiError("status must be 'draft' or 'scheduled'")
        payload['status'] = status
    if not payload:
        raise ApiError('Nothing to update: pass text, scheduled_at or status')
    api_put_json(f'/api/posts/{post_id}', payload)
    return _summarise(api_get(f'/api/posts/{post_id}'))


def tool_delete_post(args):
    post_id = _require_int(args, 'id')
    api_delete(f'/api/posts/{post_id}')
    return {'deleted': post_id}


def tool_publish_now(args):
    post_id = _require_int(args, 'id')
    api_post_json(f'/api/posts/{post_id}/post-now')
    return {
        'id': post_id,
        'queued': True,
        'note': 'Handed to the browser. Poll get_post until status leaves "posting".',
    }


def tool_schedule_on_x(args):
    post_id = _require_int(args, 'id')
    api_post_json(f'/api/posts/{post_id}/schedule-now')
    return {
        'id': post_id,
        'queued': True,
        'note': 'Handed to the browser to be scheduled inside X itself.',
    }


def tool_get_limits(args):
    health = api_get('/api/health')
    profile = api_get('/api/profile')
    premium = bool(profile.get('is_verified'))
    return {
        'app_version': health.get('version'),
        'account': profile.get('username') or None,
        'premium': premium,
        'max_characters': 25000 if premium else 280,
        'max_image_bytes': 5 * 1024 * 1024,
        'max_video_bytes': (16 * 1024 * 1024 * 1024) if premium else (512 * 1024 * 1024),
        'image_formats': ['png', 'jpg', 'jpeg', 'gif', 'webp'],
        'video_formats': ['mp4', 'mov', 'm4v'],
        'publishing_allowed': allow_publish(),
    }


def tool_list_documents(args):
    result = api_get('/api/agent/sources/list')
    if result.get('error'):
        raise ApiError(result['error'])
    return result


def tool_read_document(args):
    name = str(args.get('name') or '').strip()
    path = '/api/agent/sources/read'
    if name:
        path += '?name=' + urllib.parse.quote(name, safe='')
    result = api_get(path)
    if result.get('error'):
        raise ApiError(result['error'])
    return result


def _require_int(args, name):
    raw = args.get(name)
    if raw is None or str(raw).strip() == '':
        raise ApiError(f'Missing required argument: {name}')
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ApiError(f'{name} must be a whole number, got {raw!r}')



# --- The published timeline, as X reports it -------------------------------
#
# The mirror stores every count as text, because that is how the page gives
# them, and leaves it empty when the page showed none. An empty count is
# unknown, not zero: averaging it as zero would quietly halve every figure.

def _optional_int(args, name):
    """A whole number when the caller gave one, None when it left it out."""
    raw = args.get(name)
    if raw is None or str(raw).strip() == '':
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ApiError(f'{name} must be a whole number, got {raw!r}')


def _count(value):
    """One engagement count as a number, or None when X showed none."""
    text = '' if value is None else str(value).strip()
    return int(text) if text.isdigit() else None


def _tweet(row):
    return {
        'tweet_id': row.get('tweet_id'),
        'url': row.get('url'),
        'text': row.get('text') or '',
        'posted_at': row.get('posted_at'),
        'views': _count(row.get('views')),
        'likes': _count(row.get('likes')),
        'reposts': _count(row.get('reposts')),
        'replies': _count(row.get('replies')),
        'is_repost': str(row.get('is_repost') or '0') not in ('0', '', 'None'),
        'is_reply': str(row.get('is_reply') or '0') not in ('0', '', 'None'),
        'has_photo': str(row.get('has_photo') or '0') not in ('0', '', 'None'),
        'has_video': str(row.get('has_video') or '0') not in ('0', '', 'None'),
        'app_post_id': row.get('app_post_id'),
    }


def _mirror():
    """Every mirrored tweet for the connected account, newest first."""
    result = api_get('/api/history/x')
    tweets = result.get('tweets') if isinstance(result, dict) else None
    return [_tweet(row) for row in (tweets or []) if isinstance(row, dict)]


# Two hundred mirrored tweets came to 67,000 characters in a live run, past
# what a CLI will accept as one tool result: the agent asked for the timeline
# and got an error instead. Both the row count and each row's text are capped,
# and the reply says how to narrow the search rather than leaving it to guess.
MAX_PUBLISHED_ROWS = 50
MAX_TEXT_CHARS = 400


def tool_list_published(args):
    limit = _optional_int(args, 'limit') or 20
    limit = max(1, min(limit, MAX_PUBLISHED_ROWS))
    contains = str(args.get('contains') or '').strip().lower()
    sort = (str(args.get('sort') or 'recent').strip().lower() or 'recent')
    if sort not in ('recent', 'oldest', 'views', 'likes'):
        raise ApiError("sort must be recent, oldest, views or likes")
    include_reposts = bool(args.get('include_reposts'))

    rows = _mirror()
    total = len(rows)
    if not include_reposts:
        rows = [row for row in rows if not row['is_repost']]
    if contains:
        rows = [row for row in rows if contains in row['text'].lower()]

    matched = len(rows)
    if sort in ('views', 'likes'):
        # Unmeasured tweets sort last rather than as zero, so a tweet with no
        # figure never looks like the worst one.
        rows.sort(key=lambda row: (row[sort] is None, -(row[sort] or 0)))
    elif sort == 'oldest':
        rows.sort(key=lambda row: row['posted_at'] or '')
    else:
        rows.sort(key=lambda row: row['posted_at'] or '', reverse=True)

    kept = [_shorten(row) for row in rows[:limit]]
    result = {
        'mirrored_total': total,
        'matched': matched,
        'returned': len(kept),
        'sorted_by': sort,
        'tweets': kept,
    }
    if matched > len(kept):
        result['hint'] = (
            f'{matched} posts matched; the first {len(kept)} by {sort} are here. '
            'Narrow it with "contains", or change "sort", rather than asking '
            'for a bigger page.')
    return result


def _shorten(row):
    """One tweet, with a long body cut down to something quotable."""
    text = row['text']
    if len(text) <= MAX_TEXT_CHARS:
        return row
    short = dict(row)
    short['text'] = text[:MAX_TEXT_CHARS]
    short['text_truncated'] = True
    short['full_length'] = len(text)
    return short


def tool_get_stats(args):
    rows = _mirror()
    own = [row for row in rows if not row['is_repost']]

    metrics = {}
    for name in ('views', 'likes', 'reposts', 'replies'):
        numbers = [row[name] for row in own if row[name] is not None]
        metrics[name] = {
            'measured': len(numbers),
            'total': sum(numbers),
            'average': round(sum(numbers) / len(numbers), 1) if numbers else None,
            'best': max(numbers) if numbers else None,
        }

    dated = [row['posted_at'] for row in rows if row['posted_at']]
    by_views = sorted((row for row in own if row['views'] is not None),
                      key=lambda row: -row['views'])

    return {
        'mirrored_total': len(rows),
        'own_posts': len(own),
        'reposts': len(rows) - len(own),
        'replies': sum(1 for row in own if row['is_reply']),
        'with_media': sum(1 for row in own if row['has_photo'] or row['has_video']),
        'oldest': min(dated) if dated else None,
        'newest': max(dated) if dated else None,
        # Said plainly, because a total over a third of the posts is not a
        # total: X only shows a figure on some of them.
        'note': ('Counts come from the profile page, which shows a figure for '
                 'some posts and not others. Averages cover only the posts that '
                 'carried one - see "measured" on each metric.'),
        'metrics': metrics,
        'top_by_views': [
            {'text': row['text'][:180], 'views': row['views'],
             'likes': row['likes'], 'posted_at': row['posted_at'],
             'url': row['url'], 'has_media': row['has_photo'] or row['has_video']}
            for row in by_views[:5]
        ],
    }

# --- Tool catalogue --------------------------------------------------------

_ISO_HINT = 'Local time, ISO 8601, e.g. 2026-10-02T09:30. No timezone suffix.'

TOOLS = [
    {
        'name': 'get_limits',
        'title': 'Account limits',
        'description': ('The connected X account and what it allows: character limit, '
                        'media size ceilings, accepted formats, and whether this '
                        'session may publish. Call this first when it matters.'),
        'inputSchema': {'type': 'object', 'properties': {}},
        'handler': tool_get_limits,
        'publishes': False,
    },
    {
        'name': 'list_posts',
        'title': 'List posts',
        'description': ('Every post the app knows, which is also the calendar. '
                        'Optionally filter by a comma-separated status list: draft, '
                        'scheduled, posting, posted, scheduled_on_x, error.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'status': {'type': 'string', 'description': 'Comma-separated statuses to keep.'},
            },
        },
        'handler': tool_list_posts,
        'publishes': False,
    },
    {
        'name': 'get_post',
        'title': 'Read one post',
        'description': 'One post by id, including its status and any error message.',
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': 'Post id.'}},
            'required': ['id'],
        },
        'handler': tool_get_post,
        'publishes': False,
    },
    {
        'name': 'create_post',
        'title': 'Create a post',
        'description': ('Add a post to the app. With scheduled_at it lands in the '
                        'calendar as scheduled; without, it is a draft. Attach an '
                        'image or a video with media_path. This never publishes '
                        'anything - call it once per tweet for several tweets.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': 'The tweet text.'},
                'media_path': {
                    'type': 'string',
                    'description': ('Absolute path to one image (png, jpg, gif, webp) '
                                    'or video (mp4, mov, m4v) on this machine.'),
                },
                'scheduled_at': {'type': 'string', 'description': _ISO_HINT},
                'status': {
                    'type': 'string',
                    'enum': ['draft', 'scheduled'],
                    'description': "Defaults to 'scheduled' when scheduled_at is given, else 'draft'.",
                },
            },
        },
        'handler': tool_create_post,
        'publishes': False,
    },
    {
        'name': 'update_post',
        'title': 'Edit a post',
        'description': 'Change the text, the scheduled time or the status of an existing post.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': 'Post id.'},
                'text': {'type': 'string'},
                'scheduled_at': {'type': 'string', 'description': _ISO_HINT},
                'status': {'type': 'string', 'enum': ['draft', 'scheduled']},
            },
            'required': ['id'],
        },
        'handler': tool_update_post,
        'publishes': False,
    },
    {
        'name': 'delete_post',
        'title': 'Delete a post',
        'description': 'Remove a post from the app. Does not touch anything already on X.',
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': 'Post id.'}},
            'required': ['id'],
        },
        'handler': tool_delete_post,
        'publishes': False,
    },
    {
        'name': 'publish_now',
        'title': 'Publish now',
        'description': ('Publish a post to X immediately. Returns as soon as the '
                        'browser has taken the job; poll get_post for the outcome.'),
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': 'Post id.'}},
            'required': ['id'],
        },
        'handler': tool_publish_now,
        'publishes': True,
    },
    {
        'name': 'schedule_on_x',
        'title': 'Schedule inside X',
        'description': ("Hand a scheduled post to X's own scheduler, so it goes out "
                        'even when this app is closed. The post needs a scheduled_at.'),
        'inputSchema': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'description': 'Post id.'}},
            'required': ['id'],
        },
        'handler': tool_schedule_on_x,
        'publishes': True,
    },
]

# Reading what the user handed over. The agent has no file access; these serve
# one folder the user chose in the app, and refuse anything outside it.
TOOLS += [
    {
        'name': 'list_documents',
        'title': 'List the documents you were given',
        'description': ('The documents the user has made available to you, if any. '
                        'They chose a folder or a file in the app; you cannot see '
                        'anything else on their computer. Call this before claiming '
                        'you have nothing to work from.'),
        'inputSchema': {'type': 'object', 'properties': {}},
        'handler': tool_list_documents,
        'publishes': False,
    },
    {
        'name': 'read_document',
        'title': 'Read one of those documents',
        'description': ('The text of one document the user gave you, by the name '
                        'list_documents returned. Long files come back truncated, '
                        'and the result says so.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string',
                         'description': 'As listed by list_documents. Omit it when '
                                        'the user gave a single file.'},
            },
        },
        'handler': tool_read_document,
        'publishes': False,
    },
]

# The mirrored timeline: read-only, and available whether or not this session
# may publish. Knowing what was already said is how the assistant avoids
# saying it twice.
TOOLS += [
    {
        'name': 'list_published',
        'title': 'List published posts',
        'description': ('Posts already on X for this account, mirrored from the '
                        'profile - including ones sent from the phone or the '
                        'website, long before this app. Use it to check whether '
                        'something has been said before, or to see what did well. '
                        'Reposts are left out unless asked for.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'limit': {'type': 'integer',
                          'description': 'How many to return, 1-50 (default 20).'},
                'contains': {'type': 'string',
                             'description': 'Keep only posts whose text contains this.'},
                'sort': {'type': 'string',
                         'description': 'recent (default), oldest, views or likes.'},
                'include_reposts': {
                    'type': 'boolean',
                    'description': 'Include reposts of other people. Off by default.'},
            },
        },
        'handler': tool_list_published,
        'publishes': False,
    },
    {
        'name': 'get_stats',
        'title': 'Engagement so far',
        'description': ('Totals and averages for views, likes, reposts and '
                        'replies across the mirrored timeline, plus the five '
                        'most viewed posts. X only shows a figure on some posts, '
                        'so each metric reports how many it could measure.'),
        'inputSchema': {'type': 'object', 'properties': {}},
        'handler': tool_get_stats,
        'publishes': False,
    },
]


TOOLS_BY_NAME = {tool['name']: tool for tool in TOOLS}

MANUAL_MODE_REFUSAL = (
    'Publishing is off for this session: the user chose manual approval. '
    'Create the post as a draft or a scheduled post instead - it appears in '
    'the calendar and the user publishes it themselves.'
)

INSTRUCTIONS = (
    'Tools for X Post Management, a desktop app that publishes to X by driving a '
    'real browser. create_post stores a post; it never makes anything public. '
    'For a series of tweets, call create_post once per tweet. Times are local '
    'and have no timezone suffix. Check get_limits before writing long text or '
    'attaching large media.'
)


def visible_tools():
    """The catalogue for this session: publish tools only in auto mode."""
    publishing = allow_publish()
    return [t for t in TOOLS if publishing or not t['publishes']]


def public_tool(tool):
    """A tool entry as the wire format wants it, without our own keys."""
    return {key: tool[key] for key in ('name', 'title', 'description', 'inputSchema')}


# --- JSON-RPC plumbing -----------------------------------------------------

def _result(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _error(request_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def _text_result(payload, is_error=False):
    """A tools/call result. Structured content is duplicated as text, which the
    spec asks for so clients that ignore structuredContent still see it."""
    if is_error:
        return {'content': [{'type': 'text', 'text': str(payload)}], 'isError': True}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        'content': [{'type': 'text', 'text': text}],
        'structuredContent': payload if isinstance(payload, dict) else {'result': payload},
        'isError': False,
    }


def handle_initialize(params):
    requested = str(params.get('protocolVersion') or '')
    version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
    client = params.get('clientInfo') or {}
    log(f"initialize from {client.get('name', '?')} {client.get('version', '')} "
        f"(protocol {requested or 'unspecified'} -> {version}); "
        f"publishing {'enabled' if allow_publish() else 'disabled'}")
    return {
        'protocolVersion': version,
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {
            'name': SERVER_NAME,
            'title': 'X Post Management',
            'version': SERVER_VERSION,
        },
        'instructions': INSTRUCTIONS,
    }


def handle_tools_call(params):
    """Returns a tools/call result dict. Raises LookupError for unknown tools."""
    name = params.get('name')
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise LookupError(f'Unknown tool: {name}')

    # Hiding the tool is a hint to the model; this is the actual gate.
    if tool['publishes'] and not allow_publish():
        log(f'refused {name}: manual approval mode')
        return _text_result(MANUAL_MODE_REFUSAL, is_error=True)

    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        return _text_result('arguments must be an object', is_error=True)

    try:
        payload = tool['handler'](arguments)
    except ApiError as exc:
        # An expected failure - bad input, API said no. The agent can react to
        # it, so it belongs in the result, not in a protocol error.
        log(f'{name} failed: {exc}')
        return _text_result(str(exc), is_error=True)
    except OSError as exc:
        log(f'{name} failed: {exc}')
        return _text_result(f'File error: {exc}', is_error=True)
    log(f'{name} ok')
    return _text_result(payload)


def dispatch(message):
    """Handle one parsed JSON-RPC message. Returns a response, or None for a
    notification (which must never get one)."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _error(None, INVALID_REQUEST, 'Not a JSON-RPC 2.0 message')

    method = message.get('method')
    request_id = message.get('id')
    params = message.get('params') or {}
    if not isinstance(params, dict):
        params = {}

    is_notification = 'id' not in message

    try:
        if method == 'initialize':
            result = handle_initialize(params)
        elif method == 'ping':
            result = {}
        elif method in ('notifications/initialized', 'notifications/cancelled'):
            return None
        elif method == 'tools/list':
            result = {'tools': [public_tool(t) for t in visible_tools()]}
        elif method == 'tools/call':
            result = handle_tools_call(params)
        elif method in ('resources/list', 'prompts/list'):
            # We declare neither capability, but answering an empty list is
            # friendlier than an error for clients that probe anyway.
            result = {'resources': []} if method == 'resources/list' else {'prompts': []}
        elif isinstance(method, str) and method.startswith('notifications/'):
            return None
        else:
            if is_notification:
                return None
            return _error(request_id, METHOD_NOT_FOUND, f'Method not found: {method}')
    except LookupError as exc:
        return _error(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:                                  # never crash the loop
        log(f'internal error in {method}: {type(exc).__name__}: {exc}')
        return _error(request_id, INTERNAL_ERROR, f'{type(exc).__name__}: {exc}')

    if is_notification:
        return None
    return _result(request_id, result)


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from stdin until it closes."""
    if stdin is None or stdout is None:
        opened_in, opened_out = _stdio_streams()
        stdin = stdin or opened_in
        stdout = stdout or opened_out
    log(f'serving on stdio; API base {API_BASE}')

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            response = _error(None, PARSE_ERROR, f'Invalid JSON: {exc}')
        else:
            response = dispatch(message)
        if response is None:
            continue
        # ensure_ascii keeps the payload on a single line whatever the content.
        stdout.write(json.dumps(response, ensure_ascii=True) + '\n')
        stdout.flush()

    log('stdin closed, exiting')


def main():
    try:
        serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
