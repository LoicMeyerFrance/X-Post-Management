import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import threading
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

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

PROFILE_INFO_PATH = os.path.join(DATA_DIR, 'profile_info.json')
PREFERENCES_PATH = os.path.join(DATA_DIR, 'preferences.json')
PROFILE_PICTURE_NAME = 'profile_picture.jpg'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024        # 5 MB, the limit X accepts for images
MAX_REQUEST_SIZE = 8 * 1024 * 1024      # request bodies above this are refused outright
MAX_TEXT_LENGTH = 30_000                # hard ceiling above any X plan's limit

DEFAULT_PORT = int(os.getenv('PORT', '5000'))

# Statuses a client is allowed to set directly.
CLIENT_STATUSES = {'draft', 'scheduled', 'posting'}

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


def char_limit():
    """X Premium accounts get a much longer limit."""
    return 25_000 if read_json_file(PROFILE_INFO_PATH).get('is_verified') else 280


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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

    # Validate the upload fully before writing anything to disk, so a rejected
    # request never leaves an orphaned file in data/uploads.
    upload = request.files.get('image')
    has_upload = bool(upload and upload.filename)

    if not text and not has_upload:
        return jsonify({'error': 'Post must have text or an image'}), 400

    image_path = ''
    if has_upload:
        if not allowed_file(upload.filename):
            return jsonify({'error': 'Format not supported (use png, jpg, jpeg, gif, webp)'}), 400

        upload.seek(0, os.SEEK_END)
        size = upload.tell()
        upload.seek(0)
        if size > MAX_IMAGE_SIZE:
            return jsonify({'error': f'File too large (max {MAX_IMAGE_SIZE // 1024 // 1024}MB)'}), 400

        head = upload.read(12)
        upload.seek(0)
        if not looks_like_image(head):
            return jsonify({'error': 'This file is not a valid image'}), 400

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

    scheduled_at = post.get('scheduled_at')
    when = parse_iso_datetime(scheduled_at)
    if when is None:
        return jsonify({'error': 'Post has no valid scheduled date'}), 400
    if when <= datetime.now():
        return jsonify({'error': 'Scheduled date is in the past'}), 400

    return _queue_publish(post, post_id, f'scheduled on X for {scheduled_at}',
                          scheduled_at=scheduled_at)


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
PREFERENCE_KEYS = {'locale', 'theme', 'setupComplete'}


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

    config.write_env_file(cleaned)

    # Pick up the new browser settings and check interval without a restart.
    bot.restart_browser()
    scheduler.reschedule()

    logger.info("Environment settings updated")
    return jsonify({'success': True})


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
    write_json_file(PROFILE_INFO_PATH, info)
    database.add_follower_snapshot(
        info['followers_count'], info['following_count'], username=info['username'])
    return jsonify(result)


@app.route('/api/profile', methods=['GET'])
def api_get_profile():
    info = read_json_file(PROFILE_INFO_PATH)
    payload = _profile_payload(info)
    payload['has_picture'] = os.path.isfile(os.path.join(DATA_DIR, PROFILE_PICTURE_NAME))
    return jsonify(payload)


@app.route('/api/profile/stats', methods=['GET'])
def api_profile_stats():
    info = read_json_file(PROFILE_INFO_PATH)
    return jsonify({
        'profile': _profile_payload(info),
        'history': database.get_follower_history(username=info.get('username')),
    })


@app.route('/api/profile/picture')
def api_profile_picture():
    path = os.path.join(DATA_DIR, PROFILE_PICTURE_NAME)
    if not os.path.isfile(path):
        return jsonify({'error': 'No profile picture'}), 404
    return send_from_directory(DATA_DIR, PROFILE_PICTURE_NAME, max_age=60,
                               mimetype=detect_image_mime(path))


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

    database.init_db()
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
    _webview_window = webview.create_window('X Post Management', url, width=1200, height=800)
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
