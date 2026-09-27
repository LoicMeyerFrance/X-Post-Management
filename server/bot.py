import os
import sys
import json
import logging
import platform
import queue
import stat
import threading
from time import sleep, monotonic
from random import uniform
from datetime import datetime

import config
import paths

config.reload_env()

# Playwright work can be slow (login flows, media upload) but a wedged browser
# must never block an HTTP request forever.
DEFAULT_TASK_TIMEOUT = 300      # seconds
CONNECT_TIMEOUT = 420           # the flow itself waits up to 5 min for the user

# Point Playwright to bundled browsers when running from PyInstaller
if getattr(sys, 'frozen', False):
    import platform
    if platform.system() == 'Darwin':
        # macOS .app: pw-browsers is next to the .app bundle
        # sys.executable = .../X Post Management.app/Contents/MacOS/X Post Management
        _app_bundle = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
        _browsers = os.path.join(os.path.dirname(_app_bundle), 'pw-browsers')
    else:
        # Windows: pw-browsers is in the _MEIPASS temp dir
        _browsers = os.path.join(sys._MEIPASS, 'pw-browsers')
    if os.path.isdir(_browsers):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _browsers

logger = logging.getLogger(__name__)

# Dedicated thread for all Playwright operations.
# Playwright sync API uses greenlets and cannot be called across threads.
_task_queue = queue.Queue()
_worker_thread = None
_worker_started = False
_worker_lock = threading.Lock()

# Playwright state (only accessed from _worker_thread)
_playwright = None
_context = None
_page = None

# Set while the user is signing in by hand: the window must be on screen for
# them to answer a code or a captcha, whatever HEADLESS says.
_force_visible = False

# The user agent is deliberately NOT overridden.
#
# Playwright's user_agent option only rewrites navigator.userAgent. It does not
# touch the Sec-CH-UA client hints, navigator.userAgentData or the TLS/HTTP2
# fingerprint, which keep reporting the browser's real version. Claiming
# "Chrome/131" from a Chrome 153 binary is therefore a glaring inconsistency and
# one of the easiest automation signals to detect. Letting the browser speak for
# itself is both simpler and far less detectable.


def _find_chrome():
    """Auto-detect Chrome/Edge executable on the system."""
    candidates = []
    system = platform.system()
    if system == 'Windows':
        candidates = [
            os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%LocalAppData%\Google\Chrome\Application\chrome.exe'),
            os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'),
            os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
        ]
    elif system == 'Darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        ]
    else:
        candidates = [
            '/usr/bin/google-chrome',
            '/usr/bin/google-chrome-stable',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/usr/bin/microsoft-edge',
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ''


def _harden_permissions(path):
    """Restrict a credential file to the current user (best effort)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.debug(f"Could not restrict permissions on {path}: {exc}")


def _get_config():
    chrome_path = config.env_str('CHROME_PATH', '')
    if not chrome_path:
        chrome_path = _find_chrome()
        if chrome_path:
            logger.info(f"Auto-detected Chrome at: {chrome_path}")
    return {
        'username': config.env_str('X_USERNAME', '').lstrip('@'),
        'password': config.get_password(),
        'profile_path': config.env_str('CHROME_PROFILE_DIR', ''),
        'chrome_path': chrome_path,
        'headless': False if _force_visible else config.env_bool('HEADLESS', True),
    }


def _wait(page, selector, timeout=8000):
    """Wait for a selector and return element, or None on timeout."""
    try:
        el = page.wait_for_selector(selector, timeout=timeout)
        return el
    except Exception:
        return None


def _human_delay(low=1.0, high=2.5):
    """Small randomized delay to mimic human behavior."""
    sleep(uniform(low, high))


def _ensure_browser():
    global _playwright, _context, _page
    if _context is not None:
        try:
            if _page and not _page.is_closed():
                return _page
            # Page closed but context still alive - just open a new tab.
            _page = _context.new_page()
            return _page
        except Exception:
            pass
        _close_browser_internal()

    from playwright.sync_api import sync_playwright
    cfg = _get_config()
    _playwright = sync_playwright().start()

    chrome_path = cfg['chrome_path']
    if chrome_path:
        logger.info(f"Using real Chrome: {chrome_path}")
    else:
        logger.warning("No Chrome found - using Playwright Chromium")

    # The browser sandbox is deliberately left enabled (no --no-sandbox): this
    # browser visits the live web while holding the X session.
    #
    # --disable-gpu / --disable-software-rasterizer are NOT used: they leave the
    # browser without a WebGL renderer, which no real Chrome ever is, and that
    # absence is one of the signals bot detection looks for.
    args = [
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--disable-infobars',
        '--window-size=1280,800',
    ]

    if cfg['headless']:
        args.append('--headless=new')

    launch_kwargs = {
        'headless': False,  # We handle headless via --headless=new flag
        'args': args,
        'viewport': {'width': 1280, 'height': 800},
        'ignore_https_errors': False,
        'locale': 'fr-FR',
        'timezone_id': 'Europe/Paris',
    }
    if chrome_path:
        launch_kwargs['executable_path'] = chrome_path

    profile_path = cfg['profile_path']
    if not profile_path:
        profile_path = os.path.join(paths.DATA_DIR, 'chrome_profile')
    launch_kwargs['user_data_dir'] = profile_path

    logger.info(f"Launching browser (headless={cfg['headless']}, profile={profile_path})")
    _context = _playwright.chromium.launch_persistent_context(**launch_kwargs)

    _page = _context.new_page()

    # Close the default blank tab opened by persistent context
    for p in _context.pages:
        if p != _page:
            try:
                p.close()
            except Exception:
                pass

    # Stealth patches are applied ONLY to the bundled Chromium.
    #
    # Against real Chrome they do more harm than good: the automation flag is
    # already cleared by --disable-blink-features=AutomationControlled (verified:
    # navigator.webdriver is False without them), while the patches replace the
    # genuine WebGL renderer with a hardcoded "Intel Iris OpenGL Engine" - a
    # macOS string reported by a Windows Chrome. That contradiction is exactly
    # what fingerprinting looks for. A real browser left alone is consistent.
    if chrome_path:
        logger.info("Real Chrome: keeping its native fingerprint (no stealth patches)")
    else:
        from playwright_stealth import Stealth
        Stealth(navigator_languages_override=('fr-FR', 'fr')).apply_stealth_sync(_page)
        logger.info("Bundled Chromium: stealth patches applied")

    try:
        ident = _page.evaluate('''() => ({
            ua: navigator.userAgent,
            brands: (navigator.userAgentData && navigator.userAgentData.brands || [])
                .map(b => b.brand + ' ' + b.version).join(', '),
            webdriver: navigator.webdriver,
        })''')
        logger.info(f"Browser identity: {ident['ua']}")
        logger.info(f"  client hints: {ident['brands'] or 'n/a'} | webdriver={ident['webdriver']}")
        if 'Headless' in (ident['ua'] or ''):
            logger.warning("Browser reports itself as headless - X will likely block it. "
                           "Set HEADLESS=false in Settings.")
    except Exception as e:
        logger.debug(f"Could not read browser identity: {e}")



    return _page


def _close_browser_internal():
    global _playwright, _context, _page
    try:
        if _context:
            _context.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _playwright = None
    _context = None
    _page = None


def _close_if_visible():
    """Close browser after action if running in visible mode."""
    cfg = _get_config()
    if not cfg['headless']:
        logger.info("Closing browser (visible mode)")
        _close_browser_internal()


# X wraps its buttons in overlay elements that swallow pointer events, so
# Playwright's click() fails its actionability checks. Locating the element and
# clicking its centre with the real mouse behaves like a human click and works.
_FIND_BY_TEXT_JS = """
(args) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const sel = 'button, a, div[role="button"], span[role="button"],'
              + ' [tabindex="0"], input[type="submit"], span, div';
    const clickable = el =>
        !!el.closest('button, a, [role="button"], [tabindex], input[type="submit"]')
        || getComputedStyle(el).cursor === 'pointer';
    const out = [];
    for (const el of document.querySelectorAll(sel)) {
        const texts = [norm(el.textContent), norm(el.getAttribute('aria-label')),
                       norm(el.value)];
        const hit = args.prefix
            ? texts.some(t => t && args.patterns.some(p => t.startsWith(p)))
            : texts.some(t => t && args.patterns.includes(t));
        if (!hit) continue;
        if (!clickable(el)) continue;          // skip headings and plain labels
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 5) continue;
        // Hit test: only keep what a user could actually click. X keeps a copy
        // of the login form on the page behind its modal, and that background
        // copy matches the same label while sitting under the modal backdrop.
        const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
        if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) continue;
        const top = document.elementFromPoint(cx, cy);
        if (!top || !(top === el || el.contains(top) || top.contains(el))) continue;
        out.push({x: Math.round(r.x + r.width / 2),
                  y: Math.round(r.y + r.height / 2),
                  w: Math.round(r.width), h: Math.round(r.height),
                  area: r.width * r.height});
    }
    // Smallest match = the most specific element; the click point is what counts.
    out.sort((a, b) => a.area - b.area);
    return out[0] || null;
}
"""

# Exact labels, so "Continuer" never matches "Continuer avec Apple".
_COOKIE_LABELS = ('accepter tous les cookies', 'accept all cookies')
_SUBMIT_LABELS = ('continuer', 'continue', 'suivant', 'next')
# 'connexion' is deliberately absent: it is the heading of X's password
# step, not a control. The new flow submits with 'Continuer'.
_LOGIN_LABELS = ('se connecter', 'log in', 'login', 'continuer', 'continue')


def _locate(page, labels, prefix=False):
    """Find a visible, hittable element matching one of `labels`.

    With prefix=True the label only has to start the element's text, for things
    like "Will send on Sep 28, 2026 at 12:00".
    """
    try:
        return page.evaluate(_FIND_BY_TEXT_JS,
                             {'patterns': list(labels), 'prefix': bool(prefix)})
    except Exception:
        return None


def _click_label(page, labels, what='', prefix=False):
    """Click an element by its label using a real mouse click."""
    point = _locate(page, labels, prefix=prefix)
    if not point:
        return False
    try:
        page.mouse.move(point['x'], point['y'])
        _human_delay(0.2, 0.4)
        page.mouse.click(point['x'], point['y'])
        if what:
            logger.info(f"Clicked {what}")
        return True
    except Exception as e:
        logger.warning(f"Could not click {what or labels[0]}: {e}")
        return False


# A full-screen, pointer-events-auto backdrop means a modal is up and every
# click on the page behind it is swallowed.
_BLOCKING_OVERLAY_JS = """
() => {
    const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
    for (let n = el; n; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (cs.position !== 'fixed' || cs.pointerEvents === 'none') continue;
        const r = n.getBoundingClientRect();
        if (r.width >= innerWidth * 0.9 && r.height >= innerHeight * 0.9) {
            return (n.className || '').toString().slice(0, 60) || 'unnamed overlay';
        }
    }
    return null;
}
"""


def _blocking_overlay(page):
    """Name of the full-screen overlay swallowing clicks, or None."""
    try:
        return page.evaluate(_BLOCKING_OVERLAY_JS)
    except Exception:
        return None


def _accept_cookies(page):
    """Accept X's cookie banner, and make sure it is really gone.

    This is the single most important step of the whole login flow. While the
    banner is up, X keeps a `fixed inset-0 z-50` backdrop over the entire
    viewport: every click on the form behind it is swallowed, which silently
    breaks the password step: the real submit button sits under that backdrop.

    Clicking is therefore verified rather than assumed, and falls back to the
    element's own click() when the backdrop covers the banner button too.
    """
    for attempt in range(5):
        point = _locate(page, _COOKIE_LABELS)
        if not point:
            # No cookie banner left. A backdrop may still be there because a
            # legitimate modal (compose, schedule) is open, which is fine.
            overlay = _blocking_overlay(page)
            if overlay:
                logger.debug(f"Full-screen overlay present (a modal is open): {overlay}")
            return True

        if attempt < 3:
            _click_label(page, _COOKIE_LABELS, 'cookie banner (accept)')
        else:
            # The backdrop can cover the banner itself; dispatch on the element.
            try:
                page.evaluate("""(patterns) => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    for (const el of document.querySelectorAll('button, div[role="button"], [tabindex]')) {
                        if (patterns.includes(norm(el.textContent))) { el.click(); return true; }
                    }
                    return false;
                }""", list(_COOKIE_LABELS))
                logger.info("Cookie banner accepted via element.click()")
            except Exception as e:
                logger.warning(f"Cookie fallback click failed: {e}")

        _human_delay(1.5, 2.5)

    still = _locate(page, _COOKIE_LABELS)
    if still:
        logger.error("Could not dismiss the cookie banner - clicks will be swallowed")
    return not still


def _fill_field(page, selectors, value, what=''):
    """Type into the first matching input.

    focus() instead of click(): X's floating-label wrapper sits on top of the
    input and intercepts pointer events.
    """
    for selector in selectors:
        field = _wait(page, selector, timeout=6000)
        if not field:
            continue
        try:
            if not field.is_visible():
                continue
            field.evaluate('el => el.focus()')
            field.evaluate('el => el.value = ""')
            page.keyboard.type(value, delay=uniform(30, 70))
            logger.info(f"Filled {what or selector}")
            return True
        except Exception as e:
            logger.warning(f"Could not fill {what or selector}: {e}")
    return False


def _dismiss_popups(page):
    """Dismiss cookie banners, notification prompts, etc."""
    _accept_cookies(page)
    dismiss_selectors = [
        'div[role="button"]:has-text("Not now")',
        'div[role="button"]:has-text("Pas maintenant")',
    ]
    for sel in dismiss_selectors:
        try:
            btn = page.wait_for_selector(sel, timeout=1500)
            if btn:
                btn.click()
                _human_delay(0.5, 1)
        except Exception:
            pass


# X answers a burst of sign-in attempts with a temporary block. Retrying then
# only digs the hole deeper, so it is detected and treated as terminal.
_RATE_LIMIT_PHRASES = (
    'temporairement limité',
    'temporarily limited',
    'nous avons temporairement limité',
    'we have temporarily limited',
    'suspicious login',
    'connexion suspecte',
    'too many',
    'trop de tentatives',
    'réessayer plus tard',
    'try again later',
)


def _rate_limited(page):
    """Return X's block message if the account is being throttled."""
    try:
        text = page.inner_text('body')[:2000].lower()
    except Exception:
        return None
    for phrase in _RATE_LIMIT_PHRASES:
        if phrase in text:
            # Give back the sentence X actually shows, it is the clearest message.
            for line in page.inner_text('body').splitlines():
                if phrase in line.lower():
                    return line.strip()[:200]
            return phrase
    return None


SESSION_FILE = os.path.join(paths.DATA_DIR, 'session.json')


def _mark_session(connected, username='', error=''):
    """Record the outcome of a sign-in so the UI can show it instantly.

    Launching a browser just to answer "are we connected?" would take seconds and
    fight for the profile lock, so the answer is cached here instead.
    """
    try:
        with open(SESSION_FILE, 'w', encoding='utf-8') as handle:
            json.dump({'connected': bool(connected), 'username': username,
                       'checked_at': datetime.now().isoformat(), 'error': error},
                      handle, ensure_ascii=False)
        _harden_permissions(SESSION_FILE)
    except OSError as e:
        logger.warning(f"Could not write the session marker: {e}")


def session_status():
    """Last known connection state. Cheap: reads a file, never opens a browser."""
    cfg = _get_config()
    profile = cfg['profile_path'] or os.path.join(paths.DATA_DIR, 'chrome_profile')
    if not os.path.isdir(profile):
        return {'connected': False, 'reason': 'no browser profile yet'}
    if not os.path.isfile(SESSION_FILE):
        return {'connected': False, 'reason': 'never signed in'}
    try:
        with open(SESSION_FILE, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {'connected': False, 'reason': 'unreadable session marker'}

    # A marker for a different account is stale.
    if data.get('username') and cfg['username'] and data['username'] != cfg['username']:
        return {'connected': False, 'reason': 'signed in as a different account'}
    return {
        'connected': bool(data.get('connected')),
        'username': data.get('username', ''),
        'checked_at': data.get('checked_at', ''),
        'error': data.get('error', ''),
    }


def _is_logged_in(page):
    for selector in (
        'a[data-testid="AppTabBar_Home_Link"]',
        'div[data-testid="SideNav_AccountSwitcher_Button"]',
        'a[href="/home"][role="link"]',
    ):
        if page.query_selector(selector):
            return True
    return False


def _login(page):
    """Log in to X with the stored credentials.

    Handles X's current onboarding flow (/i/jf/onboarding/web: one form with
    `username_or_email`, then a password field) and falls back to the older
    two-step flow (`input[name="text"]` + Next) when X serves that instead.
    """
    cfg = _get_config()
    if not cfg['username'] or not cfg['password']:
        return {'success': False, 'error': 'X username or password is not set. Fill them in Settings.'}

    logger.info("Navigating to X home to check login state...")
    page.goto("https://x.com/home", wait_until='domcontentloaded')
    _human_delay(2, 3)
    _accept_cookies(page)

    if _is_logged_in(page):
        logger.info("Already logged in")
        _mark_session(True, cfg['username'])
        return {'success': True}

    # One credential submission per call. The loop only goes round again when
    # the username field was never reached (a page-load problem), never after a
    # password has been sent - that is what gets an account throttled.
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            _human_delay(1, 2)

            # --- Step 1: username / email ---
            if not _fill_field(
                page,
                ['input[name="username_or_email"]', 'input[name="text"]',
                 'input[autocomplete="username"]'],
                cfg['username'], 'username',
            ):
                logger.warning("Could not find the username field")
                if attempt < max_attempts - 1:
                    page.goto("https://x.com/home", wait_until='domcontentloaded')
                    _human_delay(2, 3)
                    _accept_cookies(page)
                    continue
                return {'success': False,
                        'error': 'Could not find the username field on the X login page'}

            _human_delay(0.5, 1)
            # Exact label: "Continuer" must not match "Continuer avec Apple".
            if not _click_label(page, _SUBMIT_LABELS, 'Continue'):
                page.keyboard.press('Enter')
            _human_delay(2.5, 4)

            # X re-renders its dialog here and the banner can come back with it.
            _accept_cookies(page)

            blocked = _rate_limited(page)
            if blocked:
                logger.error(f"X is throttling sign-in: {blocked}")
                return {'success': False, 'rate_limited': True,
                        'needs_manual_intervention': True,
                        'error': f'X has temporarily limited sign-in for this account '
                                 f'("{blocked}"). Wait before trying again - repeated '
                                 f'attempts make it worse.'}

            # --- Checkpoints X may raise before the password ---
            checkpoint = _locate(page, ('confirm your identity', 'confirmez votre identité'))
            if checkpoint:
                logger.warning("Checkpoint detected - manual intervention needed")
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X asks you to confirm your identity. Set HEADLESS=false '
                                 'in Settings and sign in once by hand.'}

            extra = page.query_selector('input[data-testid="ocfEnterTextTextInput"]')
            if extra and extra.is_visible():
                logger.warning("Extra verification step detected (phone/email)")
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X requires an extra verification (phone or email). Set '
                                 'HEADLESS=false in Settings and sign in once by hand.'}

            # --- Step 2: password ---
            if not _fill_field(
                page,
                ['input[name="password"]', 'input[type="password"]'],
                cfg['password'], 'password',
            ):
                logger.warning("Could not find the password field")
                return {'success': False,
                        'error': 'Could not reach the password step on the X login page'}

            _human_delay(0.5, 1)
            overlay = _blocking_overlay(page)
            if overlay:
                logger.warning(f"Overlay over the submit button ({overlay}), clearing it")
                _accept_cookies(page)
                _human_delay(0.5, 1)

            submitted = _click_label(page, _LOGIN_LABELS, 'Log in')
            if not submitted:
                submitted = _click_label(page, _SUBMIT_LABELS, 'Continue')
            if not submitted:
                logger.info("No submit button matched, pressing Enter")
                page.keyboard.press('Enter')

            try:
                page.wait_for_url('**/home**', timeout=15000)
            except Exception:
                # The click may have landed on something inert. Submit the form
                # from the password field itself before giving up.
                if not _is_logged_in(page) and not _rate_limited(page):
                    logger.info("Still on the form, submitting with Enter")
                    field = page.query_selector('input[name="password"], input[type="password"]')
                    if field:
                        try:
                            field.evaluate('el => el.focus()')
                            page.keyboard.press('Enter')
                        except Exception:
                            pass
                    try:
                        page.wait_for_url('**/home**', timeout=15000)
                    except Exception:
                        _human_delay(3, 5)

            _dismiss_popups(page)
            if _is_logged_in(page):
                logger.info("Login successful")
                _mark_session(True, cfg['username'])
                return {'success': True}

            blocked = _rate_limited(page)
            if blocked:
                logger.error(f"X is throttling sign-in: {blocked}")
                return {'success': False, 'rate_limited': True,
                        'needs_manual_intervention': True,
                        'error': f'X has temporarily limited sign-in for this account '
                                 f'("{blocked}"). Wait before trying again - repeated '
                                 f'attempts make it worse.'}

            checkpoint = _locate(page, ('confirm your identity', 'confirmez votre identité'))
            if checkpoint:
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X asks you to confirm your identity after sign-in.'}

            # The password has been sent: stop here rather than submit it again.
            logger.warning("Sign-in did not reach the timeline; not retrying")
            _mark_session(False, cfg['username'], 'sign-in did not complete')
            return {'success': False, 'needs_manual_intervention': True,
                    'error': 'Sign-in did not complete. Set HEADLESS=false in Settings and '
                             'sign in once by hand - the session is then reused.'}

        except Exception as e:
            logger.error(f"Login attempt {attempt + 1} failed: {e}")
            return {'success': False, 'error': f'Login error: {e}'}

    return {'success': False,
            'error': 'Could not reach the X login form. Check your connection and try again.'}


def _do_post(text, image_path, scheduled_at=None):
    """Actual posting logic - runs in the worker thread."""
    try:
        page = _ensure_browser()

        # Login if needed
        login_result = _login(page)
        if not login_result['success']:
            _close_if_visible()
            return login_result

        # Navigate to compose
        page.goto("https://x.com/compose/tweet", wait_until='domcontentloaded')

        # Type text if provided
        if text:
            text_input = _wait(page, 'div[data-testid="tweetTextarea_0"]', timeout=10000)
            if not text_input:
                _close_if_visible()
                return {'success': False, 'error': 'Could not find tweet text area'}

            text_input.click()
            _human_delay(0.3, 0.5)
            page.keyboard.type(text, delay=uniform(20, 50))
            _human_delay(0.3, 0.5)

        # Upload the media if provided
        is_video = False
        if image_path and os.path.isfile(image_path):
            file_input = _wait(page, 'input[data-testid="fileInput"]', timeout=3000)
            if not file_input:
                file_input = _wait(page, 'input[type="file"]', timeout=3000)
            if not file_input:
                _close_if_visible()
                return {'success': False, 'error': 'Could not find file input for media upload'}

            is_video = os.path.splitext(image_path)[1].lower() in VIDEO_EXTENSIONS
            size_mb = os.path.getsize(image_path) / (1024 * 1024)
            logger.info(f"Uploading {'video' if is_video else 'image'}: "
                        f"{os.path.basename(image_path)} ({size_mb:.1f} MB)")
            file_input.set_input_files(image_path)

            ready = _wait_for_media(page, is_video=is_video)
            if not ready.get('ready'):
                _close_if_visible()
                return {'success': False, 'error': ready.get('error', 'Media upload failed')}

        # --- Schedule on X natively, or post immediately ---
        if scheduled_at:
            result = _schedule_on_x(page, scheduled_at, text=text)
        else:
            result = _click_post(page, text=text)

        _close_if_visible()
        return result

    except Exception as e:
        logger.error(f"post_to_x error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


_PUBLISH_LABELS = ('post', 'publier', 'poster', 'envoyer', 'tweet')
_DELETE_LABELS = ('delete', 'supprimer')
_CONFIRM_DELETE_LABELS = ('delete', 'supprimer', 'confirm', 'confirmer', 'discard')
_SCHEDULED_TAB_LABELS = ('scheduled', 'programmés', 'programmes', 'planifiés',
                         'planifies', 'programmé', 'programme')
_WILL_SEND_PREFIXES = ('will send on', 'sera envoyé', 'sera envoye')
_CLEAR_LABELS = ('clear', 'effacer')
_SCHEDULE_CONFIRM_LABELS = ('schedule', 'programmer', 'planifier')

_FIND_TEXT_IN_ARTICLES_JS = """
(needle) => {
    for (const article of document.querySelectorAll('article')) {
        const text = (article.innerText || '').replace(/\\s+/g, ' ');
        if (!text.includes(needle)) continue;
        const link = article.querySelector('a[href*="/status/"]');
        return link ? link.getAttribute('href') : 'FOUND';
    }
    return null;
}
"""


def _needle(text, length=60):
    """A distinctive slice of the post, normalised the way innerText reads."""
    return ' '.join((text or '').split())[:length]


def _verify_published(page, text):
    """Look for the post on our own timeline.

    X's confirmation toast is the happy path, but it is easy to miss and its
    absence proves nothing. Reading the timeline back is the only way to tell a
    successful post from a silent failure - and reporting a guess here is how a
    post gets marked published when it never went out, or published twice on a
    retry.

    Returns the tweet URL, 'FOUND' without a URL, or None.
    """
    needle = _needle(text)
    if not needle:
        return None
    cfg = _get_config()
    if not cfg['username']:
        return None
    try:
        page.goto(f"https://x.com/{cfg['username']}", wait_until='domcontentloaded')
        _human_delay(2.5, 3.5)
        _dismiss_popups(page)
        found = page.evaluate(_FIND_TEXT_IN_ARTICLES_JS, needle)
    except Exception as e:
        logger.warning(f"Could not verify the post on the timeline: {e}")
        return None
    if found and found != 'FOUND' and not found.startswith('http'):
        found = 'https://x.com' + found
    return found


def _verify_scheduled(page, text):
    """Look for the post in X's own list of scheduled posts."""
    needle = _needle(text)
    if not needle:
        return None
    try:
        page.goto('https://x.com/compose/tweet/unsent/scheduled',
                  wait_until='domcontentloaded')
        _human_delay(3, 4)
        _dismiss_popups(page)
        return page.evaluate("""(needle) => {
            const body = (document.body.innerText || '').replace(/\\s+/g, ' ');
            return body.includes(needle) ? 'FOUND' : null;
        }""", needle)
    except Exception as e:
        logger.warning(f"Could not verify the scheduled post: {e}")
        return None


VIDEO_EXTENSIONS = ('.mp4', '.mov', '.m4v')

# X transcodes a video after the upload finishes, and keeps the Post button
# disabled until it is done. A short wait is why video never worked here: the
# app gave up after fifteen seconds and reported the button as stuck.
_IMAGE_READY_TIMEOUT = 60
_VIDEO_READY_TIMEOUT = 15 * 60


def _wait_for_media(page, is_video=False):
    """Wait until X has accepted the media and is ready to post.

    The Post button becoming enabled is the signal: X keeps it disabled while
    the file uploads and, for a video, while it transcodes.
    """
    budget = _VIDEO_READY_TIMEOUT if is_video else _IMAGE_READY_TIMEOUT
    deadline = monotonic() + budget

    if not _wait(page, 'div[data-testid="attachments"]', timeout=30000):
        logger.warning("No attachment preview appeared after the upload")

    last_log = 0.0
    while monotonic() < deadline:
        # X reports a rejected file through its usual toast.
        toast = page.query_selector('div[data-testid="toast"]')
        if toast:
            message = (toast.inner_text() or '').strip()
            lowered = message.lower()
            if any(word in lowered for word in
                   ('error', 'erreur', 'not supported', 'non pris en charge',
                    'too long', 'trop long', 'failed', 'échou')):
                logger.error(f"X refused the media: {message[:160]}")
                return {'ready': False, 'error': f'X refused the media: {message[:160]}'}

        button = (page.query_selector('button[data-testid="tweetButton"]')
                  or page.query_selector('div[data-testid="tweetButton"]'))
        if button and button.get_attribute('aria-disabled') != 'true':
            logger.info("Media ready")
            return {'ready': True}

        waited = budget - (deadline - monotonic())
        if is_video and waited - last_log >= 30:
            last_log = waited
            logger.info(f"Still processing the video... ({int(waited)}s)")
        sleep(1)

    return {'ready': False,
            'error': f'X did not finish processing the media within {budget // 60} minutes. '
                     'For a video, check it is MP4 or MOV (H.264/AAC) and within the length '
                     'your account allows.'}


def _click_post(page, text='', scheduled=False):
    """Click Post (or Schedule) and confirm the outcome.

    `text` lets the result be verified against X itself when the confirmation
    toast does not appear.
    """
    labels = _SCHEDULE_CONFIRM_LABELS if scheduled else _PUBLISH_LABELS

    post_btn = None
    for selector in ['button[data-testid="tweetButton"]',
                     'div[data-testid="tweetButton"]',
                     'button[data-testid="tweetButtonInline"]']:
        post_btn = _wait(page, selector, timeout=4000)
        if post_btn:
            break

    if post_btn:
        # Wait up to 15s for it to become enabled (media upload, X processing…)
        for _ in range(30):
            if post_btn.get_attribute('aria-disabled') != 'true':
                break
            sleep(0.5)
        else:
            return {'success': False,
                    'error': 'The Post button stayed disabled - check the text or the image'}
        try:
            post_btn.scroll_into_view_if_needed()
            _human_delay(0.2, 0.4)
            post_btn.click(timeout=10000)
        except Exception as e:
            logger.warning(f"Direct click on the Post button failed ({e}), trying by label")
            post_btn = None

    if not post_btn:
        # Fall back to the label, the same way the login flow does.
        if not _click_label(page, labels, 'Post/Schedule'):
            return {'success': False, 'error': 'Could not find the Post button'}

    # --- Read X's own confirmation ---
    toast_el = _wait(page, 'div[data-testid="toast"]', timeout=10000)
    if toast_el:
        toast_text = toast_el.inner_text()
        lowered = toast_text.lower()
        if any(kw in lowered for kw in
               ('sent', 'posted', 'envoy', 'publi', 'schedul', 'program')):
            tweet_url = None
            try:
                link = toast_el.query_selector('a[href*="/status/"]')
                if link:
                    tweet_url = link.get_attribute('href') or ''
                    if tweet_url and not tweet_url.startswith('http'):
                        tweet_url = 'https://x.com' + tweet_url
            except Exception:
                pass
            logger.info("X confirmed the post" + (f" ({tweet_url})" if tweet_url else ""))
            return {'success': True, 'tweet_url': tweet_url}
        logger.error(f"X refused the post: {toast_text[:160]}")
        return {'success': False, 'error': f'X refused the post: {toast_text[:160]}'}

    # --- No toast: ask X directly instead of guessing ---
    logger.info("No confirmation toast, checking X for the post")
    if scheduled:
        if _verify_scheduled(page, text):
            logger.info("Found in X's scheduled list")
            return {'success': True, 'tweet_url': ''}
    else:
        found = _verify_published(page, text)
        if found:
            url = found if found != 'FOUND' else ''
            logger.info(f"Found on the timeline{f' ({url})' if url else ''}")
            return {'success': True, 'tweet_url': url}

    if not _needle(text):
        # Nothing to search for (image-only post). The compose dialog closing is
        # the only signal available.
        if not _wait(page, 'div[data-testid="tweetTextarea_0"]', timeout=2000):
            logger.info("Compose dialog closed; treating the post as sent")
            return {'success': True, 'tweet_url': ''}

    logger.error("Could not confirm the post on X")
    return {'success': False,
            'error': 'X gave no confirmation and the post was not found on your account. '
                     'Check X before trying again to avoid posting twice.'}


MONTH_NAMES_EN = ('january', 'february', 'march', 'april', 'may', 'june',
                  'july', 'august', 'september', 'october', 'november', 'december')
MONTH_NAMES_FR = ('janvier', 'février', 'mars', 'avril', 'mai', 'juin',
                  'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre')


def _as_int(text):
    """Parse an option value as an int, tolerating zero padding. None if not numeric."""
    text = (text or '').strip()
    if not text or not text.lstrip('+-').isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _option_for(role, dt, options, use_24h=True):
    """Pick the option value that expresses `dt` for this role.

    Matching is done on the *meaning* of each option (its numeric value or month
    name), never on a formatting guess, so zero padding, 0-indexed months and
    localised month names all work. Returns None when the target simply is not
    offered - a 37-minute mark in a select that only lists multiples of five, for
    instance. Callers must treat None as a failure rather than carry on: picking
    a near-enough option would schedule the post at the wrong time.
    """
    options = _clean_options(options)
    if not options:
        return None

    if role == 'month':
        numeric = [_as_int(o) for o in options]
        if all(n is not None for n in numeric):
            values = set(numeric)
            # A 0-indexed month list (0..11) means January is "0".
            offset = 1 if values == set(range(12)) else 0
            target = dt.month - offset
            for option, number in zip(options, numeric):
                if number == target:
                    return option
            return None
        names = {MONTH_NAMES_EN[dt.month - 1], MONTH_NAMES_FR[dt.month - 1]}
        for option in options:
            if option.strip().lower() in names:
                return option
        # Only fall back to position when the list is exactly twelve months.
        if len(options) == 12:
            return options[dt.month - 1]
        return None

    if role == 'ampm':
        want = 'am' if dt.hour < 12 else 'pm'
        for option in options:
            if option.strip().lower() == want:
                return option
        return None

    if role == 'day':
        target = dt.day
    elif role == 'year':
        target = dt.year
    elif role == 'hour':
        target = dt.hour if use_24h else (dt.hour % 12 or 12)
    elif role == 'minute':
        target = dt.minute
    else:
        return None

    for option in options:
        if _as_int(option) == target:
            return option
    return None


def _clean_options(options):
    """Drop placeholder entries. X prefixes every schedule select with an empty
    option, which otherwise skews the counts the classifier relies on."""
    return [o for o in (options or []) if o is not None and o.strip() != '']


def _describe(options):
    """Summarise a select so the classifier can reason about it."""
    cleaned = _clean_options(options)
    if not cleaned:
        return None
    lowered = [o.strip().lower() for o in cleaned]

    if any(o in MONTH_NAMES_EN or o in MONTH_NAMES_FR for o in lowered):
        return {'kind': 'month_names'}
    if set(lowered) <= {'am', 'pm'} and lowered:
        return {'kind': 'ampm'}

    numbers = [_as_int(o) for o in cleaned]
    if any(n is None for n in numbers):
        return None
    return {'kind': 'numeric', 'min': min(numbers), 'max': max(numbers),
            'count': len(numbers)}


def _classify_selects(option_lists):
    """Assign a role to every select in the schedule dialog, as a set.

    Judging each select on its own cannot work: a month (1-12) and a 12-hour
    clock (1-12) are identical in isolation. X's current dialog is exactly that
    trap - a numeric month next to a 0-23 hour - and reading them one by one put
    the hour into the month field. Roles are therefore resolved against the whole
    dialog, and the 1-12 ambiguity is broken last, once we know whether a 24-hour
    select or an AM/PM box is present.

    Returns a list of roles aligned with `option_lists` ('unknown' when unsure).
    """
    descriptions = [_describe(opts) for opts in option_lists]
    roles = ['unknown'] * len(option_lists)
    ambiguous = []          # indices of 1-12 numeric selects

    for i, d in enumerate(descriptions):
        if d is None:
            continue
        if d['kind'] == 'month_names':
            roles[i] = 'month'
        elif d['kind'] == 'ampm':
            roles[i] = 'ampm'
        elif d['min'] >= 2020:
            roles[i] = 'year'
        elif d['max'] >= 32:
            roles[i] = 'minute'          # 0-59 or 0-55; days never exceed 31
        elif d['count'] >= 28 and d['max'] <= 31:
            roles[i] = 'day'
        elif d['min'] == 0 and d['max'] <= 23:
            roles[i] = 'hour'            # a 24-hour clock starts at zero
        elif d['min'] == 1 and d['max'] == 12:
            ambiguous.append(i)

    has_month = 'month' in roles
    has_hour = 'hour' in roles
    has_ampm = 'ampm' in roles

    for position, i in enumerate(ambiguous):
        if has_month:
            # The month is already accounted for, so 1-12 can only be a clock.
            roles[i] = 'hour'
        elif has_hour and not has_ampm:
            # A 24-hour select is present, so this 1-12 list is the month.
            roles[i] = 'month'
        elif has_ampm and len(ambiguous) == 2:
            # Month and 12-hour clock both present: X lists them in that order.
            roles[i] = 'month' if position == 0 else 'hour'
        elif has_ampm:
            roles[i] = 'hour'
        else:
            roles[i] = 'month' if position == 0 else 'hour'
        if roles[i] == 'month':
            has_month = True
        elif roles[i] == 'hour':
            has_hour = True

    return roles


def _schedule_on_x(page, scheduled_at, text=''):
    """Use X's native scheduling UI to schedule a post.
    Detects UI language (EN/FR/other) by reading select option values,
    so it works regardless of X interface language.
    """
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError) as e:
        return {'success': False, 'error': f'Invalid scheduled_at date: {e}'}

    logger.info(f"Scheduling post on X for {dt.isoformat()}")

    # Click the schedule button (calendar icon) in compose toolbar
    schedule_btn = None
    for selector in [
        'button[data-testid="scheduleOption"]',
        'button[aria-label*="Schedule"]',
        'button[aria-label*="Planifier"]',
        'button[aria-label*="chedul"]',
        'button[aria-label*="lanifi"]',
    ]:
        schedule_btn = _wait(page, selector, timeout=2000)
        if schedule_btn:
            break

    if not schedule_btn:
        return {'success': False, 'error': 'Could not find Schedule button in compose toolbar'}

    schedule_btn.scroll_into_view_if_needed()
    _human_delay(0.3, 0.6)
    schedule_btn.click()
    _wait(page, 'select', timeout=8000)
    _human_delay(0.5, 1)

    # ── Identify each <select> by its option values (language-independent) ──
    selects = page.query_selector_all('select')
    logger.info(f"Found {len(selects)} select elements in schedule dialog")

    option_lists = [
        page.evaluate('(el) => Array.from(el.options).map(o => o.value)', sel)
        for sel in selects
    ]
    assigned = _classify_selects(option_lists)

    roles = {}  # role -> {'el': element, 'options': [str]}
    for i, (sel, opts, role) in enumerate(zip(selects, option_lists, assigned)):
        sample = _clean_options(opts)[:3]
        if role != 'unknown' and role not in roles:
            roles[role] = {'el': sel, 'options': opts}
            logger.info(f"  Select #{i}: {role} (sample: {sample})")
        else:
            logger.info(f"  Select #{i}: {role} - ignored (sample: {sample})")

    use_24h = 'ampm' not in roles

    # Every field must be set exactly. A field we cannot express is a hard
    # failure: confirming the dialog anyway would schedule the post at whatever
    # X had pre-filled, which is how a post ends up at the wrong time.
    required = ['month', 'day', 'year', 'hour', 'minute']
    if not use_24h:
        required.append('ampm')

    missing = [r for r in required if r not in roles]
    if missing:
        return {'success': False,
                'error': f"X's schedule dialog changed: could not identify the "
                         f"{', '.join(missing)} field(s). Schedule this post from X directly."}

    for role in required:
        options = roles[role]['options']
        value = _option_for(role, dt, options, use_24h=use_24h)
        if value is None:
            available = ', '.join(options[:8]) + ('…' if len(options) > 8 else '')
            logger.error(f"No option for {role}: wanted {dt.isoformat()}, X offers [{available}]")
            return {'success': False,
                    'error': f'X does not offer the requested {role} for '
                             f'{dt.strftime("%d/%m/%Y %H:%M")}. Available: {available}'}
        try:
            roles[role]['el'].select_option(value=value)
        except Exception as e:
            logger.error(f"Could not select {role}={value!r}: {e}")
            return {'success': False, 'error': f'Could not set the {role} in X\'s schedule dialog'}
        _human_delay(0.2, 0.4)

    # Read the dialog back: this is the only proof the date really took.
    chosen = {}
    for role in required:
        try:
            chosen[role] = roles[role]['el'].input_value()
        except Exception:
            chosen[role] = None

    for role in required:
        expected = _option_for(role, dt, roles[role]['options'], use_24h=use_24h)
        if chosen[role] != expected:
            logger.error(f"Read-back mismatch on {role}: dialog shows {chosen[role]!r}, "
                         f"expected {expected!r}")
            return {'success': False,
                    'error': f'X did not accept the {role} ({chosen[role]!r} instead of '
                             f'{expected!r}); the post was not scheduled.'}

    logger.info(f"Schedule dialog verified: {chosen}")

    fmt_h = dt.hour if use_24h else (dt.hour % 12 or 12)
    fmt_suffix = '' if use_24h else (' AM' if dt.hour < 12 else ' PM')
    logger.info(f"Date/time set: {dt.day}/{dt.month}/{dt.year} {fmt_h}:{dt.minute:02d}{fmt_suffix} ({'24h' if use_24h else '12h'} format)")

    _human_delay(0.5, 1)

    # Click Confirm button
    confirm_btn = None
    for selector in [
        'button[data-testid="scheduledConfirmationPrimaryAction"]',
        'button[data-testid="confirmationSheetConfirm"]',
    ]:
        confirm_btn = _wait(page, selector, timeout=3000)
        if confirm_btn:
            break

    # Fallback: find button by text
    if not confirm_btn:
        for label in ['Confirm', 'Confirmer']:
            confirm_btn = page.query_selector(f'button:has-text("{label}")')
            if confirm_btn:
                break

    if not confirm_btn:
        return {'success': False, 'error': 'Could not find Confirm button in schedule dialog'}

    confirm_btn.click()
    _human_delay(0.3, 0.5)
    logger.info("Schedule confirmed, clicking Schedule button")

    # Now click the "Schedule" button (same as tweet button but text changed)
    return _click_post(page, text=text, scheduled=True)


def _do_test_connection():
    """Test connection logic - runs in the worker thread."""
    try:
        page = _ensure_browser()
        result = _login(page)
        _close_if_visible()
        return result
    except Exception as e:
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _parse_count(text):
    """Parse follower/following count strings like '1.2K', '3.4M', '500' into integers."""
    import re
    text = text.strip().split('\n')[0].strip()
    # Remove non-numeric suffixes like " Followers", " Following"
    text = re.split(r'\s', text)[0]
    text = text.replace(',', '').replace('\u202f', '').replace('\xa0', '')
    multiplier = 1
    if text.upper().endswith('K'):
        multiplier = 1000
        text = text[:-1]
    elif text.upper().endswith('M'):
        multiplier = 1000000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except (ValueError, TypeError):
        return 0


# --- verification badge -----------------------------------------------------
#
# The label is whatever X's interface language says, so it is compared with
# accents stripped and case folded: a French "Compte certifié" has to count just
# as much as an English "Verified account".

_VERIFIED_MARKERS = ('verified', 'verifie', 'certifie')
_BUSINESS_MARKERS = ('business', 'entreprise', 'organisation', 'organization')
_GOVERNMENT_MARKERS = ('government', 'gouvernement', 'gouvernemental')


def _fold(text):
    """Lowercase and strip accents, so 'Vérifié' and 'verifie' compare equal."""
    import unicodedata
    decomposed = unicodedata.normalize('NFD', (text or '').lower())
    return ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn')


def _badge_type(aria_label):
    """'' if this label is not a verification badge, else blue/business/government."""
    folded = _fold(aria_label)
    if not any(marker in folded for marker in _VERIFIED_MARKERS):
        return ''
    if any(marker in folded for marker in _BUSINESS_MARKERS):
        return 'business'
    if any(marker in folded for marker in _GOVERNMENT_MARKERS):
        return 'government'
    return 'blue'


def _verified_in_payload(data, username):
    """Read is_blue_verified for `username` out of X's embedded JSON.

    Scoped on purpose: the page also carries data about accounts X suggests, and
    a blanket search for is_blue_verified would happily pick up a stranger's
    badge and hand this user a 25,000 character limit they do not have.
    """
    target = _fold(username).lstrip('@')
    if not target:
        return None

    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            screen_name = node.get('screen_name') or node.get('username')
            if screen_name and _fold(screen_name) == target and 'is_blue_verified' in node:
                return bool(node['is_blue_verified'])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _do_fetch_profile():
    """Fetch profile picture and display name from X. Runs in worker thread."""
    try:
        page = _ensure_browser()

        login_result = _login(page)
        if not login_result['success']:
            _close_if_visible()
            return login_result

        cfg = _get_config()
        username = cfg['username']
        page.goto(f"https://x.com/{username}", wait_until='domcontentloaded')
        _wait(page, 'div[data-testid="UserName"]', timeout=10000)
        _dismiss_popups(page)

        # Get display name
        display_name = username
        name_el = _wait(page, 'div[data-testid="UserName"] span span', timeout=8000)
        if name_el:
            display_name = name_el.inner_text().strip()

        # Detect the verification badge. This drives the character limit, so a
        # wrong answer either blocks a long post or lets X reject one.
        is_verified = False
        verified_type = ''
        try:
            # Method 1: the badge sits next to the display name. Read every
            # label there rather than guessing at the wording.
            labels = page.evaluate("""() => {
                const out = [];
                for (const el of document.querySelectorAll(
                        'div[data-testid="UserName"] [aria-label]')) {
                    const label = el.getAttribute('aria-label');
                    if (label) out.push(label);
                }
                return out;
            }""") or []
            for label in labels:
                badge = _badge_type(label)
                if badge:
                    is_verified = True
                    verified_type = badge
                    logger.info(f"Verification badge detected: {badge} ({label})")
                    break

            # Method 2: the JSON X embeds in the page, read for this account only.
            if not is_verified:
                payloads = page.evaluate("""() => Array.from(
                    document.querySelectorAll('script[type="application/json"]'))
                    .map(s => s.textContent || '')
                    .filter(t => t.includes('is_blue_verified'))""") or []
                for raw in payloads:
                    try:
                        found = _verified_in_payload(json.loads(raw), username)
                    except Exception:
                        continue
                    if found is None:
                        continue
                    is_verified = found
                    verified_type = 'blue' if found else ''
                    logger.info(f"Verification for @{username} from embedded data: {found}")
                    break
        except Exception as e:
            logger.warning(f"Badge detection failed (non-critical): {e}")

        if not is_verified:
            logger.info("No verification badge found; the 280 character limit applies")

        # Get bio
        bio = ''
        try:
            bio_el = _wait(page, 'div[data-testid="UserDescription"]', timeout=5000)
            if bio_el:
                bio = bio_el.inner_text().strip()
                logger.info(f"Bio: {bio[:80]}")
        except Exception as e:
            logger.warning(f"Could not scrape bio (non-critical): {e}")

        # Get join date. X dropped the UserJoinDate test id, so fall back to
        # reading the phrase out of the profile header.
        join_date = ''
        try:
            join_el = _wait(page, 'span[data-testid="UserJoinDate"]', timeout=3000)
            if join_el:
                join_date = join_el.inner_text().strip()
            else:
                join_date = page.evaluate("""() => {
                    const re = /(A rejoint[^\\n]{0,40}|Joined[^\\n]{0,40})/;
                    const m = (document.body.innerText || '').match(re);
                    return m ? m[0].trim() : '';
                }""") or ''
            if join_date:
                logger.info(f"Join date: {join_date}")
            else:
                logger.info("No join date found on the profile (non-critical)")
        except Exception as e:
            logger.warning(f"Could not scrape join date (non-critical): {e}")

        # Get followers / following counts
        followers_count = 0
        following_count = 0
        try:
            followers_link = _wait(page, f'a[href="/{username}/verified_followers"]', timeout=5000)
            if not followers_link:
                followers_link = _wait(page, f'a[href="/{username}/followers"]', timeout=3000)
            if followers_link:
                raw = followers_link.inner_text().strip()
                followers_count = _parse_count(raw)
                logger.info(f"Followers count: {followers_count} (raw: '{raw}')")

            following_link = _wait(page, f'a[href="/{username}/following"]', timeout=5000)
            if following_link:
                raw = following_link.inner_text().strip()
                following_count = _parse_count(raw)
                logger.info(f"Following count: {following_count} (raw: '{raw}')")
        except Exception as e:
            logger.warning(f"Could not scrape follower counts (non-critical): {e}")

        # Get profile image URL from the avatar
        avatar_url = ''
        avatar_selectors = [
            f'div[data-testid="UserAvatar-Container-{username}"] img',
            'a[href$="/photo"] img',
            'div[data-testid^="UserAvatar"] img',
        ]
        for sel in avatar_selectors:
            img_el = _wait(page, sel, timeout=5000)
            if img_el:
                avatar_url = img_el.get_attribute('src') or ''
                if avatar_url:
                    break

        if not avatar_url:
            logger.warning("Could not find profile picture element")
            _close_if_visible()
            return {'success': False, 'error': 'Profile picture not found on page'}

        # Get the highest resolution version
        avatar_url_hq = avatar_url.replace('_normal.', '_400x400.').replace('_200x200.', '_400x400.').replace('_bigger.', '_400x400.')

        # Download the image using the browser context (authenticated, no 403)
        save_dir = paths.DATA_DIR
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, 'profile_picture.jpg')

        response = page.context.request.get(avatar_url_hq)
        if response.ok:
            with open(save_path, 'wb') as f:
                f.write(response.body())
            logger.info(f"Profile picture saved to {save_path}")
        else:
            # Fallback: try original URL
            response = page.context.request.get(avatar_url)
            if response.ok:
                with open(save_path, 'wb') as f:
                    f.write(response.body())
                logger.info(f"Profile picture saved (original size) to {save_path}")
            else:
                _close_if_visible()
                return {'success': False, 'error': f'Failed to download image (HTTP {response.status})'}

        _close_if_visible()
        return {
            'success': True,
            'display_name': display_name,
            'username': username,
            'avatar_url': avatar_url_hq,
            'is_verified': is_verified,
            'verified_type': verified_type,
            'followers_count': followers_count,
            'following_count': following_count,
            'bio': bio,
            'join_date': join_date,
        }

    except Exception as e:
        logger.error(f"fetch_profile error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _do_close():
    """Close browser - runs in the worker thread."""
    _close_browser_internal()
    return {'success': True}


def _do_connect_x():
    """Sign in to X, showing the window so the user can finish by hand.

    Runs the normal sign-in first. If X answers with a code, a captcha or any
    other challenge, the window is left open and we simply watch for the
    timeline to appear - whatever the user does to get there is fine.
    """
    global _force_visible
    _force_visible = True
    try:
        # Relaunch on screen if a headless browser is already up.
        _close_browser_internal()
        page = _ensure_browser()

        result = _login(page)
        if result.get('success'):
            logger.info("Connected to X")
            _close_browser_internal()
            return {'success': True, 'message': 'Connected to X'}

        # Hand over to the user and wait for them to reach the timeline.
        logger.info("Sign-in needs a hand - waiting up to 5 min for the user")
        deadline = monotonic() + 300
        while monotonic() < deadline:
            sleep(3)
            try:
                if page.is_closed():
                    logger.info("User closed the window")
                    break
                if _is_logged_in(page) or '/home' in page.url:
                    cfg = _get_config()
                    _mark_session(True, cfg['username'])
                    logger.info("Connected to X (finished by the user)")
                    _close_browser_internal()
                    return {'success': True, 'message': 'Connected to X'}
            except Exception:
                break

        cfg = _get_config()
        error = result.get('error', 'Sign-in was not completed')
        _mark_session(False, cfg['username'], error)
        _close_browser_internal()
        return {'success': False, 'error': error,
                'rate_limited': result.get('rate_limited', False)}

    except Exception as e:
        logger.error(f"connect_x error: {e}")
        _close_browser_internal()
        return {'success': False, 'error': str(e)}
    finally:
        _force_visible = False


_DELETED_MARKERS = (
    'this post was deleted',
    'ce post a été supprimé',
    'ce post a ete supprime',
    'post unavailable',
    'post indisponible',
    'page does not exist',
    "cette page n'existe pas",
)


def _tweet_state(page, tweet_url):
    """Is this tweet still on X?

    Returns 'present', 'missing' or 'unknown'. Anything we are not sure about is
    'unknown' on purpose: the caller offers to delete what came back missing, and
    a wrong guess would throw away a post that is actually still up.
    """
    if not tweet_url or '/status/' not in tweet_url:
        return 'unknown'
    try:
        page.goto(tweet_url, wait_until='domcontentloaded', timeout=30000)
        _human_delay(1.5, 2.5)
        if _wait(page, 'article[data-testid="tweet"]', timeout=8000):
            return 'present'
        body = (page.inner_text('body') or '').lower()
        if any(marker in body for marker in _DELETED_MARKERS):
            return 'missing'
        return 'unknown'
    except Exception as e:
        logger.warning(f"Could not check {tweet_url[-24:]}: {e}")
        return 'unknown'


def _do_check_tweets(tweet_urls):
    """Check a batch of tweets in one browser session."""
    try:
        page = _ensure_browser()
        login_result = _login(page)
        if not login_result.get('success'):
            _close_if_visible()
            return login_result

        states = {}
        for index, url in enumerate(tweet_urls, 1):
            states[url] = _tweet_state(page, url)
            logger.info(f"  [{index}/{len(tweet_urls)}] {states[url]}: {url[-24:]}")
        _close_if_visible()
        return {'success': True, 'states': states}
    except Exception as e:
        logger.error(f"check_tweets error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _do_delete_tweet(tweet_url):
    """Delete a tweet from X. Runs in worker thread."""
    try:
        if not tweet_url or '/status/' not in tweet_url:
            return {'success': False, 'error': 'Invalid tweet URL'}

        page = _ensure_browser()

        login_result = _login(page)
        if not login_result['success']:
            _close_if_visible()
            return login_result

        logger.info(f"Navigating to tweet: {tweet_url}")
        page.goto(tweet_url, wait_until='domcontentloaded')
        _human_delay(1, 2)
        _dismiss_popups(page)

        # Check if the tweet exists
        tweet_article = _wait(page, 'article[data-testid="tweet"]', timeout=10000)
        if not tweet_article:
            # Tweet might already be deleted or doesn't exist
            deleted_text = _wait(page, 'text="This post was deleted"', timeout=2000)
            if not deleted_text:
                deleted_text = _wait(page, 'text="Ce post a été supprimé"', timeout=1000)
            if deleted_text:
                logger.info("Tweet already deleted")
                _close_if_visible()
                return {'success': True, 'already_deleted': True}
            _close_if_visible()
            return {'success': False, 'error': 'Tweet not found'}

        # Click the "More" button (three dots) on the tweet
        more_btn = None
        for selector in [
            'article[data-testid="tweet"] button[data-testid="caret"]',
            'article[data-testid="tweet"] div[aria-label*="More"]',
            'article[data-testid="tweet"] div[aria-label*="Plus"]',
        ]:
            more_btn = _wait(page, selector, timeout=3000)
            if more_btn:
                break

        if not more_btn:
            _close_if_visible()
            return {'success': False, 'error': 'Could not find More button on tweet'}

        more_btn.click()
        _human_delay(0.5, 1)

        # Click "Delete" in the dropdown menu
        # has-text() matches substrings and would happily hit "Delete all"; the
        # label helper matches exactly and clicks what is really on top.
        if not _click_label(page, _DELETE_LABELS, 'Delete in the menu'):
            page.keyboard.press('Escape')
            _close_if_visible()
            return {'success': False, 'error': 'Could not find the Delete entry in the menu'}
        _human_delay(0.5, 1)

        # Confirm deletion in the dialog
        confirm_btn = _wait(page, 'button[data-testid="confirmationSheetConfirm"]', timeout=3000)
        if confirm_btn:
            confirm_btn.click()
        elif not _click_label(page, _CONFIRM_DELETE_LABELS, 'the delete confirmation'):
            _close_if_visible()
            return {'success': False, 'error': 'Could not find the confirmation button'}
        _human_delay(1, 2)

        # Verify deletion - the tweet should disappear or show deleted message
        toast_el = _wait(page, 'div[data-testid="toast"]', timeout=5000)
        if toast_el:
            toast_text = toast_el.inner_text().lower()
            if 'deleted' in toast_text or 'supprimé' in toast_text:
                logger.info("Tweet deleted successfully (confirmed by toast)")
                _close_if_visible()
                return {'success': True}

        # Check if we're redirected away from the tweet
        _human_delay(0.5, 1)
        if '/status/' not in page.url:
            logger.info("Tweet deleted successfully (redirected away)")
            _close_if_visible()
            return {'success': True}

        # Check if the tweet article is gone
        tweet_still_visible = _wait(page, 'article[data-testid="tweet"]', timeout=2000)
        if not tweet_still_visible:
            logger.info("Tweet deleted successfully (tweet disappeared)")
            _close_if_visible()
            return {'success': True}

        logger.warning("Tweet deletion status uncertain")
        _close_if_visible()
        return {'success': True}

    except Exception as e:
        logger.error(f"delete_tweet error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _do_delete_scheduled_tweet(post_text):
    """Delete a scheduled tweet from X by matching its text content.
    Uses JavaScript DOM traversal for reliable element detection inside modal overlays.
    Flow: Drafts modal (Scheduled tab) -> click tweet -> click "Will send on..." -> click "Clear"
    """
    try:
        if not post_text or not post_text.strip():
            return {'success': False, 'error': 'No text provided to match scheduled tweet'}

        page = _ensure_browser()

        login_result = _login(page)
        if not login_result['success']:
            _close_if_visible()
            return login_result

        # Navigate to scheduled tweets page — opens the "Drafts" modal with "Scheduled" tab
        scheduled_url = 'https://x.com/compose/tweet/unsent/scheduled'
        logger.info(f"Navigating to scheduled tweets: {scheduled_url}")
        page.goto(scheduled_url, wait_until='domcontentloaded')
        _human_delay(4, 6)
        _dismiss_popups(page)

        search_text = post_text.strip()
        logger.info(f"Looking for scheduled tweet: '{search_text[:80]}'")

        # Step 1: Wait for the page to settle and click the "Scheduled" tab
        # Use JavaScript to find and click the "Scheduled" tab text
        _human_delay(2, 3)

        # Click the "Scheduled" tab to make sure we see scheduled tweets
        tab_clicked = _click_label(page, _SCHEDULED_TAB_LABELS, 'the Scheduled tab')
        logger.info(f"Scheduled tab clicked: {tab_clicked}")
        _human_delay(3, 4)

        # Step 2: Find and click the scheduled tweet matching our text
        # Use JS to scan ALL visible text nodes and find the one matching search_text
        click_result = page.evaluate('''(searchText) => {
            const searchLower = searchText.toLowerCase().trim();
            const allElements = document.querySelectorAll('span, div, p');
            const candidates = [];

            for (const el of allElements) {
                // Only check direct text content (not children) to avoid clicking containers
                const directText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim())
                    .join(' ')
                    .trim();

                if (!directText) continue;

                const elText = directText.toLowerCase();
                if (elText.includes(searchLower) || searchLower.includes(elText)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && rect.top > 0) {
                        candidates.push({
                            element: el,
                            text: directText,
                            exactMatch: elText === searchLower,
                            top: rect.top,
                            tag: el.tagName
                        });
                    }
                }
            }

            // Sort: prefer exact matches, then by position (higher = more likely in modal)
            candidates.sort((a, b) => {
                if (a.exactMatch && !b.exactMatch) return -1;
                if (!a.exactMatch && b.exactMatch) return 1;
                return a.top - b.top;
            });

            // Log what we found
            const debugInfo = candidates.slice(0, 5).map(c => ({text: c.text, tag: c.tag, top: Math.round(c.top)}));

            if (candidates.length > 0) {
                candidates[0].element.click();
                return {found: true, clicked: candidates[0].text, tag: candidates[0].tag, allCandidates: debugInfo};
            }

            // Debug: list all visible text fragments to understand what's on screen
            const visibleTexts = [];
            for (const el of document.querySelectorAll('span')) {
                const t = el.textContent.trim();
                if (t && t.length > 1 && t.length < 100) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        visibleTexts.push(t.substring(0, 60));
                    }
                }
                if (visibleTexts.length >= 30) break;
            }
            return {found: false, visibleTexts: visibleTexts};
        }''', search_text)

        logger.info(f"Tweet search result: {click_result}")

        if not click_result.get('found'):
            visible = click_result.get('visibleTexts', [])
            logger.error(f"Tweet '{search_text}' not found. Visible texts on page: {visible}")
            _close_if_visible()
            return {'success': False, 'error': 'Tweet not found in scheduled list'}

        logger.info(f"Clicked tweet text: '{click_result.get('clicked')}'")
        _human_delay(2, 3)

        # Step 3: In the editor, click "Will send on..." to open the schedule picker
        # We need the SMALLEST element (shortest text) to avoid clicking on a parent container
        logger.info("Looking for 'Will send on...' text in editor...")
        will_send_clicked = _click_label(page, _WILL_SEND_PREFIXES,
                                         'the "Will send on..." line', prefix=True)
        logger.info(f"'Will send on' clicked: {will_send_clicked}")

        if not will_send_clicked:
            logger.error("Could not find 'Will send on...' in editor")
            page.keyboard.press('Escape')
            _human_delay(0.5, 1)
            _close_if_visible()
            return {'success': False, 'error': 'Could not find "Will send on..." link in editor'}

        _human_delay(3, 4)

        # Step 4: In the schedule picker, click "Clear" (top-right)
        # Retry a few times in case the picker takes time to open
        logger.info("Looking for 'Clear' button in schedule picker...")
        clear_clicked = False
        for attempt in range(4):
            clear_clicked = _click_label(page, _CLEAR_LABELS, 'Clear')
            if clear_clicked:
                break
            logger.info(f"Clear not found yet (attempt {attempt + 1}/4), waiting...")
            _human_delay(2, 3)

        logger.info(f"Clear clicked: {clear_clicked}")

        if not clear_clicked:
            logger.error("Could not find 'Clear' button after retries")
            page.keyboard.press('Escape')
            _human_delay(0.5, 1)
            _close_if_visible()
            return {'success': False, 'error': 'Could not find Clear button in schedule picker'}

        _human_delay(2, 3)

        # Step 5: Handle any confirmation dialog (Discard/Delete/Confirm)
        confirmed = _click_label(page, _CONFIRM_DELETE_LABELS, 'the confirmation')
        if confirmed:
            _human_delay(1, 2)

        logger.info("Scheduled tweet deleted successfully")
        _close_if_visible()
        return {'success': True}

    except Exception as e:
        logger.error(f"delete_scheduled_tweet error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _worker_loop():
    """Worker thread main loop. Processes all Playwright tasks sequentially."""
    while True:
        task = _task_queue.get()
        if task is None:
            _close_browser_internal()
            break

        func, args, result_event, result_holder, on_done = task
        try:
            result_holder['result'] = func(*args)
        except Exception as e:
            logger.exception(f"{getattr(func, '__name__', 'task')} raised")
            result_holder['result'] = {'success': False, 'error': str(e)}
        finally:
            result_event.set()
            if on_done is not None:
                try:
                    on_done(result_holder.get('result'))
                except Exception:
                    logger.exception("Job completion callback failed")


def _ensure_worker():
    global _worker_thread, _worker_started
    with _worker_lock:
        if not _worker_started:
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name='playwright-worker')
            _worker_thread.start()
            _worker_started = True


def _submit(func, *args, on_done=None):
    """Queue work on the Playwright worker and return at once.

    Publishing takes tens of seconds. Holding the HTTP request open for that
    long blocks the UI and leaves the outcome to a connection that may not
    survive it, so callers hand in a callback and read the result from the
    database instead.
    """
    _ensure_worker()
    _task_queue.put((func, args, threading.Event(), {}, on_done))


def _run_in_worker(func, *args, timeout=DEFAULT_TASK_TIMEOUT):
    """Submit a task to the Playwright worker thread and wait for the result.

    On timeout the task keeps running in the worker (Playwright objects cannot be
    cancelled from another thread) but the caller gets an error instead of
    hanging forever.
    """
    _ensure_worker()
    result_event = threading.Event()
    result_holder = {}
    _task_queue.put((func, args, result_event, result_holder, None))
    if not result_event.wait(timeout):
        logger.error(f"{func.__name__} timed out after {timeout}s")
        return {'success': False,
                'error': f'Browser operation timed out after {timeout}s. '
                         'Try again, or disable headless mode to see what X is asking for.'}
    return result_holder.get('result', {'success': False, 'error': 'No result'})


# ===== Public API (thread-safe, callable from any thread) =====

def post_to_x(text='', image_path='', scheduled_at=None):
    """Post or schedule on X, waiting for the result. Returns a dict."""
    return _run_in_worker(_do_post, text, image_path, scheduled_at)


def post_to_x_async(text='', image_path='', scheduled_at=None, on_done=None):
    """Queue a post or a schedule; `on_done` receives the result dict."""
    _submit(_do_post, text, image_path, scheduled_at, on_done=on_done)


def test_connection():
    """Test X connection by checking login state. Returns dict."""
    return _run_in_worker(_do_test_connection)


def fetch_profile():
    """Fetch profile picture and info from X. Returns dict."""
    return _run_in_worker(_do_fetch_profile)


def restart_browser():
    """Close the browser so it gets re-created with new settings on next use."""
    return _run_in_worker(_do_close, timeout=60)


def delete_tweet(tweet_url):
    """Delete a tweet from X. Returns dict with success, error keys."""
    return _run_in_worker(_do_delete_tweet, tweet_url)


def check_tweets(tweet_urls):
    """Check which of these tweets still exist on X. Returns dict with states."""
    # Roughly four seconds per tweet, plus the sign-in check.
    timeout = 60 + 15 * len(tweet_urls)
    return _run_in_worker(_do_check_tweets, list(tweet_urls), timeout=timeout)


def delete_scheduled_tweet(post_text):
    """Delete a scheduled tweet from X by matching text. Returns dict with success, error keys."""
    return _run_in_worker(_do_delete_scheduled_tweet, post_text)


def connect_x(timeout=None):
    """Sign in to X in a visible window. Returns dict."""
    return _run_in_worker(_do_connect_x, timeout=CONNECT_TIMEOUT)


def close():
    """Shutdown the Playwright worker thread and close browser."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            _task_queue.put(None)  # sentinel: shut the worker down
            _worker_thread.join(timeout=10)
            _worker_started = False
