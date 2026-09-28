import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import sys
import threading
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

# The frozen build has no python.exe for Claude Code to launch, so the executable
# re-runs itself in MCP mode. This has to happen before anything heavy is
# imported: the MCP child has no use for Flask, the scheduler or Playwright, and
# importing the browser stack in it would cost seconds for nothing.
if __name__ == '__main__' and '--mcp' in sys.argv:
    import mcp_server
    sys.exit(mcp_server.main())

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import agent
import bot
import config
import database
import paths
import scheduler
import security

BASE_DIR = paths.BASE_DIR
FRONTEND_DIR = paths.FRONTEND_DIR
UPLOAD_DIR = paths.UPLOAD_DIR
LOG_DIR = paths.LOG_DIR
LOG_FILE = paths.LOG_FILE
DATA_DIR = paths.DATA_DIR

def _read_version():
    """The version shown in the app, from the VERSION file at the repo root.

    Bumping that one file updates the interface, the health endpoint and the
    log line together; the release tag should match it.
    """
    for base in (paths.RESOURCE_DIR, paths.BASE_DIR):
        path = os.path.join(base, 'VERSION')
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                version = handle.read().strip()
            if version:
                return version
        except OSError:
            continue
    return 'dev'


APP_VERSION = _read_version()

PREFERENCES_PATH = os.path.join(DATA_DIR, 'preferences.json')


def profile_info_path():
    """Where this account's cached profile lives."""
    return paths.profile_info_path(database.current_account())


def profile_picture_name():
    return paths.profile_picture_name(database.current_account())


def adopt_legacy_profile_cache():
    """Move a pre-scoping profile cache under the account it describes.

    Before the cache was per-account there was one profile_info.json. It names
    the account it belongs to, so it can be filed correctly instead of being
    shown for whoever is configured now.
    """
    legacy_info = os.path.join(DATA_DIR, 'profile_info.json')
    if not os.path.isfile(legacy_info):
        return
    info = read_json_file(legacy_info)
    handle = database.normalise_account(info.get('username'))
    if not handle:
        return
    target = paths.profile_info_path(handle)
    if not os.path.exists(target):
        try:
            os.replace(legacy_info, target)
            logger.info("Filed the existing profile cache under @%s", handle)
        except OSError as exc:
            logger.warning("Could not move the profile cache: %s", exc)
            return
    legacy_picture = os.path.join(DATA_DIR, 'profile_picture.jpg')
    new_picture = os.path.join(DATA_DIR, paths.profile_picture_name(handle))
    if os.path.isfile(legacy_picture) and not os.path.exists(new_picture):
        try:
            os.replace(legacy_picture, new_picture)
        except OSError as exc:
            logger.warning("Could not move the profile picture: %s", exc)

# X accepts MP4 and MOV (H.264 + AAC). Everything else is rejected by its own
# uploader, so there is no point letting it through here.
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'm4v'}
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS

MAX_IMAGE_SIZE = 5 * 1024 * 1024              # 5 MB, what X takes for an image

# X's video ceiling depends on the account: 512 MB for everyone, far more for
# Premium. Capping a Premium account at the standard limit would refuse files X
# would have accepted.
MAX_VIDEO_SIZE_STANDARD = 512 * 1024 * 1024
MAX_VIDEO_SIZE_PREMIUM = 16 * 1024 * 1024 * 1024

# The hard ceiling Werkzeug enforces; the per-account limit is checked on top.
MAX_REQUEST_SIZE = MAX_VIDEO_SIZE_PREMIUM + 8 * 1024 * 1024
MAX_TEXT_LENGTH = 30_000                # hard ceiling above any X plan's limit

DEFAULT_PORT = int(os.getenv('PORT', '5000'))

# Statuses a client is allowed to set directly.
CLIENT_STATUSES = {'draft', 'scheduled', 'posting'}

# Publishing something already published would put a second, identical tweet on
# the timeline. Refused whoever asks - the calendar, the assistant, or a script.
# "Duplicate" is the endpoint for deliberately posting the same thing twice.
ALREADY_POSTED = ('This post has already been published. Duplicate it if you '
                  'want to post it again.')

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_SIZE
app.config['JSON_SORT_KEYS'] = False

app.before_request(security.check_request)
app.after_request(security.add_headers)


# --- Logging setup ---

def setup_logging():
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5,
                                       encoding='utf-8')
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    # Guard against duplicate handlers if this module is imported twice.
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)
        root_logger.addHandler(stream_handler)


setup_logging()
logger = logging.getLogger(__name__)
config.reload_env()


# --- Small helpers ---

def read_json_file(path, default=None):
    """Read a JSON file, tolerating a missing or corrupted file."""
    if not os.path.isfile(path):
        return {} if default is None else default
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else ({} if default is None else default)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", os.path.basename(path), exc)
        return {} if default is None else default


def write_json_file(path, data):
    """Write JSON atomically so a crash cannot leave a truncated file behind."""
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp_path, path)


def is_premium():
    """Does the stored profile say this account is verified?"""
    return bool(read_json_file(profile_info_path()).get('is_verified'))


def char_limit():
    """X Premium accounts get a much longer limit."""
    return 25_000 if is_premium() else 280


def video_size_limit():
    """The biggest video X will take for this account."""
    return MAX_VIDEO_SIZE_PREMIUM if is_premium() else MAX_VIDEO_SIZE_STANDARD


def file_extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def allowed_file(filename):
    return file_extension(filename) in ALLOWED_EXTENSIONS


# Magic bytes for the formats we accept. Checking the content, not just the
# extension, stops a renamed file from reaching disk or being served back.
_IMAGE_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', None),
    (b'\xff\xd8\xff', None),
    (b'GIF87a', None),
    (b'GIF89a', None),
    (b'RIFF', b'WEBP'),          # RIFF....WEBP
)


_MIME_BY_SIGNATURE = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF8', 'image/gif'),
    (b'RIFF', 'image/webp'),
)


def detect_image_mime(path, default='image/jpeg'):
    """Read the real format off the file.

    The avatar is always written as profile_picture.jpg whatever X actually
    served, so trusting the extension mislabels a PNG as JPEG.
    """
    try:
        with open(path, 'rb') as handle:
            head = handle.read(12)
    except OSError:
        return default
    for prefix, mime in _MIME_BY_SIGNATURE:
        if head.startswith(prefix):
            return mime
    return default


def looks_like_image(head):
    for prefix, marker in _IMAGE_SIGNATURES:
        if not head.startswith(prefix):
            continue
        if marker is None or head[8:12] == marker:
            return True
    return False


def looks_like_video(head):
    """MP4 and MOV are ISO base media files: bytes 4-8 spell 'ftyp'."""
    return len(head) >= 12 and head[4:8] == b'ftyp'


def media_kind(filename, head):
    """'image', 'video' or None when the bytes do not back up the extension."""
    extension = file_extension(filename)
    if extension in ALLOWED_IMAGE_EXTENSIONS and looks_like_image(head):
        return 'image'
    if extension in ALLOWED_VIDEO_EXTENSIONS and looks_like_video(head):
        return 'video'
    return None


def parse_iso_datetime(value):
    """Return a datetime for an ISO string, or None when it is unusable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def remove_media_file(image_path, context=''):
    if not image_path:
        return
    # Only ever delete inside our own uploads folder.
    try:
        resolved = os.path.realpath(image_path)
        if os.path.commonpath([resolved, os.path.realpath(UPLOAD_DIR)]) != os.path.realpath(UPLOAD_DIR):
            logger.warning("Refusing to delete a file outside uploads: %s", image_path)
            return
    except (OSError, ValueError):
        return
    if not os.path.isfile(resolved):
        return
    try:
        os.remove(resolved)
        logger.info("Media file deleted%s: %s", context, os.path.basename(resolved))
    except OSError as exc:
        logger.warning("Could not delete media file: %s", exc)


# --- Error handling ---

def _is_spa_route(path):
    """Client-side routes look like /schedule, not like /logo.png.

    Serving the SPA shell for anything at all turns every miss into a 200, which
    hides real 404s and makes a missing asset look like it loaded.
    """
    if path.startswith(('/api/', '/uploads/')):
        return False
    return '.' not in path.rsplit('/', 1)[-1]


@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    """JSON errors for the API; the SPA shell for unknown page routes."""
    if exc.code == 404 and _is_spa_route(request.path):
        index_path = os.path.join(FRONTEND_DIR, 'index.html')
        if os.path.isfile(index_path):
            return send_from_directory(FRONTEND_DIR, 'index.html')
        return jsonify({'error': 'Frontend not built. Run: cd ui && npm run build'}), 404

    if exc.code == 413:
        return jsonify({'error': f'Request too large (max {MAX_REQUEST_SIZE // 1024 // 1024}MB)'}), 413

    return jsonify({'error': exc.description or exc.name}), exc.code


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    logger.exception("Unhandled error on %s %s", request.method, request.path)
    return jsonify({'error': 'Internal server error'}), 500


# --- Frontend (SPA) ---

@app.route('/')
def serve_frontend():
    index_path = os.path.join(FRONTEND_DIR, 'index.html')
    if not os.path.isfile(index_path):
        return jsonify({'error': 'Frontend not built. Run: cd ui && npm run build'}), 404
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/api/health')
def api_health():
    return jsonify({'status': 'ok', 'version': APP_VERSION})


# --- Posts ---

@app.route('/api/posts', methods=['POST'])
def api_create_post():
    text = request.form.get('text', '').strip()
    scheduled_at = request.form.get('scheduled_at', '').strip()
    status = request.form.get('status', 'draft').strip()

    if status not in CLIENT_STATUSES:
        return jsonify({'error': f'Invalid status: {status}'}), 400

    limit = char_limit()
    if len(text) > min(limit, MAX_TEXT_LENGTH):
        return jsonify({'error': f'Text exceeds {limit} characters'}), 400

    if status == 'scheduled':
        if not scheduled_at:
            return jsonify({'error': 'Scheduled posts need a date/time'}), 400
        if parse_iso_datetime(scheduled_at) is None:
            return jsonify({'error': 'Invalid date/time format'}), 400
    elif scheduled_at and parse_iso_datetime(scheduled_at) is None:
        return jsonify({'error': 'Invalid date/time format'}), 400

    # Refuse an oversized body from its Content-Length, before Werkzeug spools
    # gigabytes to disk only for the size check below to throw them away.
    declared = request.content_length or 0
    if declared > video_size_limit() + 8 * 1024 * 1024:
        return jsonify({'error': f'Upload too large (max '
                                 f'{video_size_limit() // 1024 // 1024}MB for your account)'}), 413

    # Validate the upload fully before writing anything to disk, so a rejected
    # request never leaves an orphaned file in data/uploads.
    upload = request.files.get('image')
    has_upload = bool(upload and upload.filename)

    if not text and not has_upload:
        return jsonify({'error': 'Post must have text or an image'}), 400

    image_path = ''
    if has_upload:
        if not allowed_file(upload.filename):
            return jsonify({'error': 'Format not supported (images: png, jpg, jpeg, gif, '
                                     'webp - videos: mp4, mov)'}), 400

        head = upload.read(12)
        upload.seek(0)
        kind = media_kind(upload.filename, head)
        if kind is None:
            return jsonify({'error': 'This file is not a valid image or video '
                                     '(X accepts png, jpg, gif, webp, mp4, mov)'}), 400

        upload.seek(0, os.SEEK_END)
        size = upload.tell()
        upload.seek(0)
        limit = video_size_limit() if kind == 'video' else MAX_IMAGE_SIZE
        if size > limit:
            return jsonify({'error': f'{kind.capitalize()} too large '
                                     f'(max {limit // 1024 // 1024}MB)'}), 400

        # A random name avoids collisions between two uploads in the same second
        # and keeps any user-controlled string out of the filesystem path.
        ext = upload.filename.rsplit('.', 1)[1].lower()
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
        image_path = os.path.join(UPLOAD_DIR, filename)
        upload.save(image_path)

    try:
        post_id = database.create_post(
            text=text,
            image_path=image_path,
            scheduled_at=scheduled_at or None,
            status=status,
        )
    except Exception:
        remove_media_file(image_path, ' (rolled back)')
        raise

    logger.info("Post #%s created (status=%s)", post_id, status)
    return jsonify({'id': post_id, 'status': status}), 201


@app.route('/api/posts', methods=['GET'])
def api_list_posts():
    status = request.args.get('status')
    if not status:
        return jsonify(database.get_all_posts())
    # Accept a comma-separated list so a page needing several statuses can ask once.
    statuses = [s.strip() for s in status.split(',') if s.strip()]
    unknown = [s for s in statuses if s not in database.VALID_STATUSES]
    if unknown:
        return jsonify({'error': f"Unknown status: {', '.join(unknown)}"}), 400
    return jsonify(database.get_posts_by_statuses(statuses))


@app.route('/api/posts/<int:post_id>', methods=['GET'])
def api_get_post(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    return jsonify(post)


@app.route('/api/posts/<int:post_id>', methods=['PUT'])
def api_update_post(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid payload'}), 400

    text = data.get('text', post['text'])
    if not isinstance(text, str):
        return jsonify({'error': 'Invalid text'}), 400
    limit = char_limit()
    if len(text) > min(limit, MAX_TEXT_LENGTH):
        return jsonify({'error': f'Text exceeds {limit} characters'}), 400

    scheduled_at = data.get('scheduled_at', post['scheduled_at'])
    if scheduled_at and parse_iso_datetime(scheduled_at) is None:
        return jsonify({'error': 'Invalid date/time format'}), 400

    status = data.get('status', post['status'])
    if status not in database.VALID_STATUSES:
        return jsonify({'error': f'Invalid status: {status}'}), 400
    if status == 'scheduled' and not scheduled_at:
        return jsonify({'error': 'Scheduled posts need a date/time'}), 400

    database.update_post(post_id, text=text, scheduled_at=scheduled_at or None, status=status)
    logger.info("Post #%s updated", post_id)
    return jsonify({'id': post_id, 'updated': True})


@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
def api_delete_post(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    remove_media_file(post.get('image_path', ''))
    database.delete_post(post_id)
    logger.info("Post #%s deleted", post_id)
    return jsonify({'deleted': True})


def _on_publish_done(post_id, action, scheduled=False):
    """Build the callback that records what the browser worker ended up doing."""
    def done(result):
        result = result or {'success': False, 'error': 'No result from the browser'}
        if result.get('success'):
            if scheduled:
                database.update_post(post_id, status='scheduled_on_x', error_message='')
                logger.info("Post #%s %s", post_id, action)
            else:
                tweet_url = result.get('tweet_url') or ''
                database.update_post(post_id, status='posted', error_message='',
                                     posted_at=datetime.now().isoformat(),
                                     tweet_url=tweet_url)
                logger.info("Post #%s %s (tweet_url=%s)", post_id, action,
                            tweet_url or 'unknown')
            return
        error = result.get('error', 'Unknown error')
        database.update_post_status(post_id, 'error', error_message=error)
        logger.error("Post #%s %s failed: %s", post_id, action, error)
    return done


def _queue_publish(post, post_id, action, scheduled_at=None):
    """Hand the post to the browser worker and answer immediately.

    The client watches the post's status instead of holding a request open for
    the half-minute or so the browser needs.
    """
    status = 'scheduling' if scheduled_at else 'posting'
    database.update_post(post_id, status=status, error_message='')
    bot.post_to_x_async(
        text=post.get('text', ''),
        image_path=post.get('image_path', ''),
        scheduled_at=scheduled_at,
        on_done=_on_publish_done(post_id, action, scheduled=bool(scheduled_at)),
    )
    logger.info("Post #%s queued (%s)", post_id, status)
    return jsonify({'queued': True, 'id': post_id, 'status': status}), 202


@app.route('/api/posts/<int:post_id>/post-now', methods=['POST'])
def api_post_now(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    if post['status'] in ('posting', 'scheduling'):
        return jsonify({'error': 'This post is already being processed'}), 409
    if post['status'] == 'posted':
        return jsonify({'error': ALREADY_POSTED}), 409

    return _queue_publish(post, post_id, 'published immediately')


@app.route('/api/posts/<int:post_id>/retry', methods=['POST'])
def api_retry_post(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    if post['status'] in ('posting', 'scheduling'):
        return jsonify({'error': 'This post is already being processed'}), 409

    database.update_post(post_id, retries_count=0)
    return _queue_publish(post, post_id, 'retry')


@app.route('/api/posts/<int:post_id>/schedule-now', methods=['POST'])
def api_schedule_now(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    if post['status'] in ('posting', 'scheduling'):
        return jsonify({'error': 'This post is already being processed'}), 409
    if post['status'] == 'posted':
        return jsonify({'error': ALREADY_POSTED}), 409

    scheduled_at = post.get('scheduled_at')
    when = parse_iso_datetime(scheduled_at)
    if when is None:
        return jsonify({'error': 'Post has no valid scheduled date'}), 400
    if when <= datetime.now():
        return jsonify({'error': 'Scheduled date is in the past'}), 400

    return _queue_publish(post, post_id, f'scheduled on X for {scheduled_at}',
                          scheduled_at=scheduled_at)


# Checking opens each tweet in turn, so the batch is capped to keep the request
# and the traffic to X reasonable.
MAX_TWEETS_CHECKED = 25


@app.route('/api/posts/check-on-x', methods=['POST'])
def api_check_on_x():
    """Find posts the app still lists but X no longer has.

    Nothing is deleted here: the answer is a list the user confirms. A tweet we
    could not read conclusively is reported as 'unknown' and left alone.
    """
    posts = [p for p in database.get_all_posts() if p.get('tweet_url')]
    if not posts:
        return jsonify({'checked': 0, 'missing': [], 'unknown': 0, 'truncated': False})

    truncated = len(posts) > MAX_TWEETS_CHECKED
    posts = posts[:MAX_TWEETS_CHECKED]

    result = bot.check_tweets([p['tweet_url'] for p in posts])
    if not result.get('success'):
        return jsonify({'error': result.get('error', 'Check failed')}), 200

    states = result.get('states', {})
    missing, unknown = [], 0
    for post in posts:
        state = states.get(post['tweet_url'], 'unknown')
        if state == 'missing':
            missing.append({'id': post['id'], 'text': (post.get('text') or '')[:80]})
        elif state == 'unknown':
            unknown += 1

    logger.info("Checked %s post(s) on X: %s missing, %s inconclusive",
                len(posts), len(missing), unknown)
    return jsonify({'checked': len(posts), 'missing': missing,
                    'unknown': unknown, 'truncated': truncated})


@app.route('/api/posts/<int:post_id>/delete-from-x', methods=['POST'])
def api_delete_from_x(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    tweet_url = post.get('tweet_url', '')
    if not tweet_url:
        return jsonify({'error': 'No tweet URL stored for this post'}), 400

    result = bot.delete_tweet(tweet_url)
    if not result.get('success'):
        error = result.get('error', 'Unknown error')
        logger.error("Post #%s delete from X failed: %s", post_id, error)
        return jsonify({'success': False, 'error': error})

    remove_media_file(post.get('image_path', ''))
    database.delete_post(post_id)
    logger.info("Post #%s deleted from X and database", post_id)
    return jsonify({'success': True, 'already_deleted': result.get('already_deleted', False)})


@app.route('/api/posts/<int:post_id>/delete-scheduled-from-x', methods=['POST'])
def api_delete_scheduled_from_x(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    if post.get('status') != 'scheduled_on_x':
        return jsonify({'error': 'Post is not scheduled on X'}), 400

    post_text = (post.get('text') or '').strip()
    if not post_text:
        return jsonify({'error': 'Post has no text, cannot match on X'}), 400

    result = bot.delete_scheduled_tweet(post_text)
    if not result.get('success'):
        error = result.get('error', 'Unknown error')
        logger.error("Post #%s delete scheduled from X failed: %s", post_id, error)
        return jsonify({'success': False, 'error': error})

    remove_media_file(post.get('image_path', ''))
    database.delete_post(post_id)
    logger.info("Post #%s scheduled tweet deleted from X and database", post_id)
    return jsonify({'success': True})


@app.route('/api/posts/<int:post_id>/remove-media', methods=['POST'])
def api_remove_media(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    remove_media_file(post.get('image_path', ''))
    database.update_post(post_id, image_path='')
    logger.info("Post #%s media removed", post_id)
    return jsonify({'success': True})


@app.route('/api/posts/<int:post_id>/duplicate', methods=['POST'])
def api_duplicate_post(post_id):
    post = database.get_post(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    # Copy the media file so each post owns its own copy.
    new_image_path = ''
    image_path = post.get('image_path', '')
    if image_path and os.path.isfile(image_path):
        ext = os.path.splitext(image_path)[1]
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
        new_image_path = os.path.join(UPLOAD_DIR, name)
        shutil.copy2(image_path, new_image_path)

    try:
        new_id = database.create_post(text=post['text'], image_path=new_image_path, status='draft')
    except Exception:
        remove_media_file(new_image_path, ' (rolled back)')
        raise

    logger.info("Post #%s duplicated as #%s", post_id, new_id)
    return jsonify({'id': new_id}), 201


# --- Settings ---

# Every UI preference the frontend persists. Keeping this closed stops the
# preferences file from growing without bound.
# Whether the assistant may publish without asking. Declared here so the
# preference allow-list below can name it.
AGENT_AUTO_KEY = 'agentAutoApprove'

# Whether the assistant may look things up online. Read-only web tools, opt-in,
# so the "it can only do what this app does" guarantee holds unless asked.
AGENT_WEB_KEY = 'agentWebAccess'

# Anything the interface may remember. A key missing from here is rejected with a
# 400, which the frontend swallows - so a toggle whose key was never added here
# looks like it saved and silently forgets itself.
PREFERENCE_KEYS = {'locale', 'theme', 'setupComplete', 'browserHintSeen',
                   'calendarShowFromX', AGENT_AUTO_KEY, AGENT_WEB_KEY}


@app.route('/api/settings/preferences', methods=['GET'])
def api_get_preferences():
    return jsonify(read_json_file(PREFERENCES_PATH))


@app.route('/api/settings/preferences', methods=['POST'])
def api_save_preferences():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({'error': 'No data'}), 400

    updates = {}
    for key, value in data.items():
        if key not in PREFERENCE_KEYS:
            return jsonify({'error': f'Unknown preference: {key}'}), 400
        if not isinstance(value, str) or len(value) > 64:
            return jsonify({'error': f'Invalid value for {key}'}), 400
        updates[key] = value

    prefs = read_json_file(PREFERENCES_PATH)
    prefs.update(updates)
    write_json_file(PREFERENCES_PATH, prefs)
    return jsonify({'success': True})


@app.route('/api/settings/env', methods=['GET'])
def api_get_env():
    return jsonify(config.public_values())


@app.route('/api/settings/env', methods=['POST'])
def api_save_env():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({'error': 'No data'}), 400

    try:
        cleaned = config.validate(config.merge_secrets(data))
    except config.ConfigError as exc:
        return jsonify({'error': str(exc)}), 400

    previous = database.current_account()
    config.write_env_file(cleaned)
    switched_to = database.current_account()

    # Rows created before any username was configured belong to whoever is now
    # configured; rows already stamped with another handle are left where they
    # are, so a switch never drags the other account's history along.
    database.adopt_orphan_rows(switched_to)

    if previous and switched_to and previous != switched_to:
        logger.info("Account switched from @%s to @%s; each keeps its own posts, "
                    "timeline mirror and profile", previous, switched_to)

    # Pick up the new browser settings and check interval without a restart.
    bot.restart_browser()
    scheduler.reschedule()

    logger.info("Environment settings updated")
    return jsonify({
        'success': True,
        'account': switched_to,
        'switched_from': previous if previous and previous != switched_to else '',
    })


@app.route('/api/settings/test-connection', methods=['GET'])
def api_test_connection():
    return jsonify(bot.test_connection())


@app.route('/api/settings/connect-x', methods=['POST'])
def api_connect_x():
    """Sign in to X in a visible window, so the user can answer any challenge."""
    return jsonify(bot.connect_x())


@app.route('/api/settings/connection-status', methods=['GET'])
def api_connection_status():
    """Last known connection state. Does not open a browser."""
    return jsonify(bot.session_status())


# --- Assistant (Claude Code over MCP) ---
#
# The app does not talk to any model itself. It runs the user's own Claude Code
# CLI and streams what it prints, with this app's MCP server attached so the
# agent can read and write posts. Whether it may publish is decided here, from
# the stored preference - never from the request body, so the answer cannot
# change per call.

def agent_auto_approve():
    """True when the user ticked automatic approval in the assistant tab."""
    return str(read_json_file(PREFERENCES_PATH).get(AGENT_AUTO_KEY, '')).lower() == 'true'


def agent_web_access():
    """True when the user allowed the assistant to search and fetch the web."""
    return str(read_json_file(PREFERENCES_PATH).get(AGENT_WEB_KEY, '')).lower() == 'true'


def _api_base():
    """The URL the MCP child should call back on: this very server.

    Read from the request, not from DEFAULT_PORT: pick_port() settles on another
    port whenever 5000 is busy, and a child told the wrong port reports the app
    as unreachable. check_request has already established that the Host is
    loopback; it is re-checked here so this can never point the child elsewhere.
    """
    host = request.host
    if host and security.is_loopback_host(host):
        return f'{request.scheme}://{host}'
    return f'http://127.0.0.1:{DEFAULT_PORT}'


@app.route('/api/agent/status', methods=['GET'])
def api_agent_status():
    info = agent.status()
    info['auto_approve'] = agent_auto_approve()
    info['web_access'] = agent_web_access()
    info['running'] = agent.is_running()
    return jsonify(info)


@app.route('/api/agent/key', methods=['POST'])
def api_agent_key():
    """Store or clear the Anthropic API key. Never read back in clear text."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Expected a JSON object'}), 400

    key = str(data.get('api_key', '')).strip()
    if key and key == config.MASK:
        return jsonify({'saved': True, 'has_api_key': True})      # unchanged
    if key and ('\x00' in key or '\n' in key or len(key) > 400):
        return jsonify({'error': 'That does not look like an API key'}), 400

    if not agent.set_api_key(key):
        return jsonify({'error': 'No OS credential store is available, so the key '
                                 'cannot be stored safely. Sign in to Claude Code '
                                 'instead and leave this blank.'}), 500
    logger.info("Anthropic API key %s", 'stored' if key else 'cleared')
    return jsonify({'saved': True, 'has_api_key': bool(key)})


@app.route('/api/agent/auto', methods=['POST'])
def api_agent_auto():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'auto_approve' not in data:
        return jsonify({'error': 'Expected {"auto_approve": true|false}'}), 400
    enabled = bool(data['auto_approve'])
    prefs = read_json_file(PREFERENCES_PATH)
    prefs[AGENT_AUTO_KEY] = 'true' if enabled else 'false'
    write_json_file(PREFERENCES_PATH, prefs)
    logger.info("Assistant approval mode set to %s", 'automatic' if enabled else 'manual')
    return jsonify({'auto_approve': enabled})


@app.route('/api/agent/install', methods=['POST'])
def api_agent_install():
    """Run Anthropic's own installer so the user never opens a terminal.

    This fetches and runs a remote script, which is what the official docs tell
    people to do by hand. The exact command is in /api/agent/status so the
    interface can show it before the user agrees to it.
    """
    if agent.find_cli():
        return jsonify({'installed': True, 'output': '', 'already': True})

    ok, output = agent.install()
    status_code = 200 if ok else 500
    return jsonify({'installed': ok, 'output': output}), status_code


@app.route('/api/agent/login', methods=['POST'])
def api_agent_login():
    """Open the Claude sign-in. The app never sees those credentials."""
    ok, detail = agent.start_login()
    return jsonify({'started': ok, 'detail': detail}), (200 if ok else 500)


@app.route('/api/agent/web', methods=['POST'])
def api_agent_web():
    """Allow or refuse the assistant's read-only web tools."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'web_access' not in data:
        return jsonify({'error': 'Expected {"web_access": true|false}'}), 400
    enabled = bool(data['web_access'])
    prefs = read_json_file(PREFERENCES_PATH)
    prefs[AGENT_WEB_KEY] = 'true' if enabled else 'false'
    write_json_file(PREFERENCES_PATH, prefs)
    logger.info("Assistant web access %s", 'allowed' if enabled else 'refused')
    return jsonify({'web_access': enabled})


@app.route('/api/agent/stop', methods=['POST'])
def api_agent_stop():
    return jsonify({'stopped': agent.stop()})


@app.route('/api/agent/reset', methods=['POST'])
def api_agent_reset():
    """Forget the conversation, so the next message starts a fresh session."""
    agent.stop()
    agent.clear_session()
    return jsonify({'reset': True})


@app.route('/api/agent/chat', methods=['POST'])
def api_agent_chat():
    """Run one turn, streaming the CLI's events back as server-sent events."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Expected a JSON object'}), 400

    message = str(data.get('message', '')).strip()
    if not message:
        return jsonify({'error': 'Message is empty'}), 400

    # Both come from the stored preference, never from the request body: a
    # per-call flag would let a caller grant itself more than the user chose.
    auto = agent_auto_approve()
    web = agent_web_access()
    fresh = bool(data.get('new_conversation'))

    try:
        events = agent.stream(message, auto=auto, api_base=_api_base(),
                              resume=not fresh, web_access=web)
    except agent.AgentError as exc:
        return jsonify({'error': str(exc)}), 400

    def emit():
        try:
            for event in events:
                yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
        except agent.AgentError as exc:
            yield 'data: ' + json.dumps({'type': 'xpm', 'subtype': 'failed',
                                         'error': str(exc)}) + '\n\n'
        except Exception:
            logger.exception("Assistant stream failed")
            yield 'data: ' + json.dumps({'type': 'xpm', 'subtype': 'failed',
                                         'error': 'The assistant stopped unexpectedly. '
                                                  'Check the logs.'}) + '\n\n'
        finally:
            yield 'data: [DONE]\n\n'

    return app.response_class(emit(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-store',
        # Without this a proxy or the webview may hold the whole stream back.
        'X-Accel-Buffering': 'no',
    })


# --- Profile ---

def _profile_payload(info):
    fallback = config.env_str('X_USERNAME', '')
    return {
        'display_name': info.get('display_name', fallback),
        'username': info.get('username', fallback),
        'is_verified': info.get('is_verified', False),
        'verified_type': info.get('verified_type', ''),
        'followers_count': info.get('followers_count', 0),
        'following_count': info.get('following_count', 0),
        'bio': info.get('bio', ''),
        'join_date': info.get('join_date', ''),
    }


@app.route('/api/profile/fetch', methods=['POST'])
def api_fetch_profile():
    result = bot.fetch_profile()
    if not result.get('success'):
        return jsonify(result)

    info = {
        'display_name': result.get('display_name', ''),
        'username': result.get('username', ''),
        'is_verified': result.get('is_verified', False),
        'verified_type': result.get('verified_type', ''),
        'followers_count': result.get('followers_count', 0),
        'following_count': result.get('following_count', 0),
        'bio': result.get('bio', ''),
        'join_date': result.get('join_date', ''),
    }
    write_json_file(profile_info_path(), info)
    database.add_follower_snapshot(
        info['followers_count'], info['following_count'], username=info['username'])
    return jsonify(result)


@app.route('/api/profile', methods=['GET'])
def api_get_profile():
    info = read_json_file(profile_info_path())
    payload = _profile_payload(info)
    payload['has_picture'] = os.path.isfile(
        os.path.join(DATA_DIR, profile_picture_name()))
    return jsonify(payload)


@app.route('/api/profile/stats', methods=['GET'])
def api_profile_stats():
    info = read_json_file(profile_info_path())
    return jsonify({
        'profile': _profile_payload(info),
        'history': database.get_follower_history(username=info.get('username')),
    })


@app.route('/api/profile/picture')
def api_profile_picture():
    name = profile_picture_name()
    path = os.path.join(DATA_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'error': 'No profile picture'}), 404
    return send_from_directory(DATA_DIR, name, max_age=60,
                               mimetype=detect_image_mime(path))


# --- The X timeline, read back from the account itself ---
#
# The app only ever knew the posts it sent. This reads the profile, so anything
# published from the phone or the website shows up too. It is a mirror: nothing
# here is scheduled, retried or deleted, and the rows live in their own table.

@app.route('/api/history/x', methods=['GET'])
def api_x_history():
    posts = database.get_x_posts()
    return jsonify({
        'tweets': posts,
        'count': len(posts),
        'last_sync': max((p.get('fetched_at') or '') for p in posts) if posts else '',
    })


@app.route('/api/history/x/sync', methods=['POST'])
def api_x_history_sync():
    """Read the profile and store what is there.

    Synchronous like the other browser sweeps: it holds the single worker, and
    the client has nothing useful to do until it finishes. Bounded by the scroll
    ceiling in bot.py so it cannot run away.
    """
    data = request.get_json(silent=True) or {}
    try:
        max_tweets = int(data.get('max_tweets') or bot.TIMELINE_MAX_TWEETS)
    except (TypeError, ValueError):
        return jsonify({'error': 'max_tweets must be a whole number'}), 400
    max_tweets = max(1, min(max_tweets, bot.TIMELINE_MAX_TWEETS))

    known_before = database.count_x_posts()
    result = bot.fetch_timeline(max_tweets=max_tweets)
    if not result.get('success'):
        return jsonify({'error': result.get('error', 'Could not read the timeline')}), 200

    tweets = result.get('tweets') or []
    added, updated = database.save_x_posts(tweets)
    logger.info("X history synced: %s read, %s new, %s refreshed (had %s)",
                len(tweets), added, updated, known_before)
    return jsonify({
        'read': len(tweets),
        'added': added,
        'updated': updated,
        'total': database.count_x_posts(),
        'reached_ceiling': len(tweets) >= max_tweets,
    })


# --- Logs ---

LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 256 * 1024


@app.route('/api/logs', methods=['GET'])
def api_get_logs():
    """Return the tail of the log file, reading only the last chunk of it.

    The Logs page polls every few seconds and the tail is mostly unchanged, so
    the client sends back the fingerprint it already holds and gets a short
    'unchanged' reply instead of the same 25 kB again.
    """
    if not os.path.isfile(LOG_FILE):
        return jsonify({'logs': '', 'fingerprint': ''})
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as handle:
            if size > LOG_TAIL_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
                handle.readline()  # drop the partial first line
            lines = handle.readlines()
        content = ''.join(lines[-LOG_TAIL_LINES:])
    except OSError as exc:
        logger.warning("Could not read log file: %s", exc)
        return jsonify({'logs': '', 'error': 'Could not read logs'}), 500

    fingerprint = hashlib.sha1(content.encode('utf-8', 'replace')).hexdigest()[:16]
    if request.args.get('fingerprint') == fingerprint:
        return jsonify({'unchanged': True, 'fingerprint': fingerprint})
    return jsonify({'logs': content, 'fingerprint': fingerprint})


# --- Native dialogs (pywebview) ---

_webview_window = None


def _file_dialog(dialog_type, **kwargs):
    if _webview_window is None:
        return jsonify({'path': None, 'error': 'No window available'})
    try:
        import webview
        result = _webview_window.create_file_dialog(getattr(webview, dialog_type), **kwargs)
        return jsonify({'path': result[0] if result else None})
    except Exception as exc:
        logger.warning("File dialog failed: %s", exc)
        return jsonify({'path': None, 'error': str(exc)})


@app.route('/api/browse/folder', methods=['POST'])
def api_browse_folder():
    return _file_dialog('FOLDER_DIALOG')


@app.route('/api/browse/file', methods=['POST'])
def api_browse_file():
    return _file_dialog(
        'OPEN_DIALOG',
        file_types=('Executable Files (*.exe)', 'All Files (*.*)'),
    )


@app.route('/api/detect-chrome', methods=['GET'])
def api_detect_chrome():
    """Auto-detect Chrome's executable and default profile on this system."""
    system = platform.system()

    if system == 'Windows':
        chrome_candidates = [
            os.path.join(os.environ.get('PROGRAMFILES', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        ]
        profile_candidates = [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'User Data', 'Default'),
        ]
    elif system == 'Darwin':
        chrome_candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
        profile_candidates = [os.path.expanduser('~/Library/Application Support/Google/Chrome/Default')]
    else:
        chrome_candidates = [
            '/usr/bin/google-chrome',
            '/usr/bin/google-chrome-stable',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
        ]
        profile_candidates = [
            os.path.expanduser('~/.config/google-chrome/Default'),
            os.path.expanduser('~/.config/chromium/Default'),
        ]

    chrome_path = next((p for p in chrome_candidates if p and os.path.isfile(p)), None)
    profile_dir = next((p for p in profile_candidates if p and os.path.isdir(p)), None)

    return jsonify({
        'chrome_path': chrome_path,
        'profile_dir': profile_dir,
        'detected': chrome_path is not None or profile_dir is not None,
    })


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    # send_from_directory rejects any path that escapes UPLOAD_DIR.
    if not allowed_file(filename):
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory(UPLOAD_DIR, filename, max_age=3600)


# --- Start ---

# Registry entries the Edge WebView2 runtime writes when it is installed.
_WEBVIEW2_KEYS = (
    r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients'
    r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    r'SOFTWARE\Microsoft\EdgeUpdate\Clients'
    r'\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
)

WEBVIEW2_DOWNLOAD = 'https://developer.microsoft.com/microsoft-edge/webview2/'


def _webview2_installed():
    """Is the Edge WebView2 runtime present on this machine?"""
    import winreg
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in _WEBVIEW2_KEYS:
            try:
                with winreg.OpenKey(root, key) as handle:
                    version, _ = winreg.QueryValueEx(handle, 'pv')
                    if version and version != '0.0.0.0':
                        return True
            except OSError:
                continue
    return False


def native_window_usable():
    """Can we open a real window, or would it come up blank?

    On Windows pywebview falls back to MSHTML - Internet Explorer's engine -
    when EdgeChromium is unavailable. MSHTML cannot run this interface, so the
    window opens blank white with nothing but a deprecation warning in the log.
    Better to detect that and use the browser, which always works.

    Returns (usable, reason).
    """
    if platform.system() != 'Windows':
        return True, ''
    try:
        import webview.platforms.edgechromium  # noqa: F401
    except Exception as exc:
        return False, f'the EdgeChromium backend is missing from this build ({exc})'
    if not _webview2_installed():
        return False, ('the Microsoft Edge WebView2 runtime is not installed '
                       f'({WEBVIEW2_DOWNLOAD})')
    return True, ''


def pick_port(preferred=DEFAULT_PORT, host='127.0.0.1'):
    """Return `preferred` if free, otherwise a port the OS hands us."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    return preferred


def main():
    import webbrowser

    if config.migrate_password():
        logger.info("Your X password is now stored in the OS credential store")

    database.init_db()
    adopt_legacy_profile_cache()
    stranded = database.recover_interrupted()
    if stranded:
        logger.warning("%s post(s) were interrupted by a previous shutdown "
                       "and are flagged for review", stranded)
    scheduler.start()

    host = '127.0.0.1'
    port = pick_port()
    url = f'http://{host}:{port}'
    if port != DEFAULT_PORT:
        logger.warning("Port %s is busy, using %s instead", DEFAULT_PORT, port)
    logger.info("X Post Management %s starting on %s", APP_VERSION, url)

    def run_server():
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    def run_in_browser(reason):
        logger.warning("Opening in your browser instead: %s", reason)
        print('\n' + '=' * 60)
        print('  X Post Management')
        print(f'  {reason}')
        print(f'  The app is running at {url}')
        print('  Press Ctrl+C to stop')
        print('=' * 60 + '\n')
        webbrowser.open(url)
        try:
            run_server()
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()

    try:
        import webview
    except ImportError as exc:
        run_in_browser(f'pywebview is not available ({exc})')
        return

    usable, reason = native_window_usable()
    if not usable:
        run_in_browser(f'a native window would not render here: {reason}')
        return

    global _webview_window
    threading.Thread(target=run_server, daemon=True).start()
    # text_select defaults to False in pywebview, which makes the whole interface
    # unselectable - so a post's text could not be copied out of the app at all.
    _webview_window = webview.create_window('X Post Management', url,
                                            width=1200, height=800,
                                            text_select=True)
    gui_backend = 'edgechromium' if platform.system() == 'Windows' else None
    try:
        webview.start(gui=gui_backend)
    except Exception as exc:
        logger.error("The native window failed to start: %s", exc)
        run_in_browser(f'the native window could not start ({exc})')
        return
    finally:
        logger.info("Window closed, shutting down...")
        shutdown()


def shutdown():
    try:
        scheduler.stop()
    except Exception as exc:
        logger.warning("Scheduler shutdown failed: %s", exc)
    try:
        bot.close()
    except Exception as exc:
        logger.warning("Browser shutdown failed: %s", exc)


if __name__ == '__main__':
    main()
