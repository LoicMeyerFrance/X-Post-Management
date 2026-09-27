import logging
import threading
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

import bot
import config
import database

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(daemon=True)

JOB_ID = 'check_posts'

# Exponential backoff between retries, so a failing post does not hammer X
# every CHECK_INTERVAL_SECONDS.
_RETRY_BASE_DELAY = 60
_RETRY_MAX_DELAY = 30 * 60
_retry_after = {}
_retry_lock = threading.Lock()


def _current_interval():
    return config.env_int('CHECK_INTERVAL_SECONDS', 15, *config.CHECK_INTERVAL_BOUNDS)


def _backoff_seconds(retries):
    return min(_RETRY_BASE_DELAY * (2 ** max(0, retries)), _RETRY_MAX_DELAY)


def _is_throttled(post_id):
    with _retry_lock:
        due = _retry_after.get(post_id)
        if due is None:
            return False
        if time.monotonic() >= due:
            _retry_after.pop(post_id, None)
            return False
        return True


def _throttle(post_id, retries):
    with _retry_lock:
        _retry_after[post_id] = time.monotonic() + _backoff_seconds(retries)


def _clear_throttle(post_id):
    with _retry_lock:
        _retry_after.pop(post_id, None)


def _process_due_posts():
    """Hand posts marked 'scheduled' to X's native scheduler."""
    try:
        pending = database.get_pending_scheduled()
    except Exception as exc:
        logger.error("Could not read pending posts: %s", exc)
        return

    max_retries = config.env_int('MAX_RETRIES', 1, *config.MAX_RETRIES_BOUNDS)

    for post in pending:
        post_id = post['id']
        scheduled_at = post.get('scheduled_at')

        if not scheduled_at:
            database.update_post_status(
                post_id, 'error', error_message='Scheduled post has no date')
            continue

        try:
            when = datetime.fromisoformat(scheduled_at)
        except (TypeError, ValueError):
            database.update_post_status(
                post_id, 'error', error_message=f'Invalid scheduled date: {scheduled_at}')
            continue

        # X refuses dates in the past; fail fast with a message the user can act on
        # instead of burning retries on a request that cannot succeed.
        if when <= datetime.now():
            logger.warning("Post #%s scheduled date has passed (%s)", post_id, scheduled_at)
            database.update_post_status(
                post_id, 'error',
                error_message='Scheduled date has passed - publish now or pick a new date')
            _clear_throttle(post_id)
            continue

        if _is_throttled(post_id):
            continue

        logger.info("Scheduling post #%s on X for %s", post_id, scheduled_at)
        database.update_post_status(post_id, 'scheduling')

        result = bot.post_to_x(
            text=post.get('text', ''),
            image_path=post.get('image_path', ''),
            scheduled_at=scheduled_at,
        )

        if result.get('success'):
            database.update_post_status(post_id, 'scheduled_on_x')
            _clear_throttle(post_id)
            logger.info("Post #%s scheduled on X successfully", post_id)
            continue

        error_msg = result.get('error', 'Unknown error')
        retries = post.get('retries_count') or 0

        # A throttled account or a challenge X wants a human for will not be
        # fixed by trying again - retrying only makes the block worse.
        if result.get('rate_limited') or result.get('needs_manual_intervention'):
            database.update_post_status(post_id, 'error', error_message=error_msg)
            _clear_throttle(post_id)
            logger.error("Post #%s needs your attention, not a retry: %s", post_id, error_msg)
            continue

        if retries < max_retries:
            database.update_post(
                post_id,
                status='scheduled',
                error_message=error_msg,
                retries_count=retries + 1,
            )
            _throttle(post_id, retries)
            logger.warning("Post #%s scheduling failed, retry %s/%s in %ss: %s",
                           post_id, retries + 1, max_retries,
                           _backoff_seconds(retries), error_msg)
        else:
            database.update_post_status(post_id, 'error', error_message=error_msg)
            _clear_throttle(post_id)
            logger.error("Post #%s scheduling failed permanently: %s", post_id, error_msg)


def start():
    interval = _current_interval()
    scheduler.add_job(_process_due_posts, 'interval', seconds=interval, id=JOB_ID,
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started (checking every %ss)", interval)


def reschedule():
    """Apply a new CHECK_INTERVAL_SECONDS without restarting the app."""
    if not scheduler.running:
        return
    interval = _current_interval()
    try:
        scheduler.reschedule_job(JOB_ID, trigger='interval', seconds=interval)
        logger.info("Scheduler interval updated to %ss", interval)
    except Exception as exc:
        logger.warning("Could not reschedule the check job: %s", exc)


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
