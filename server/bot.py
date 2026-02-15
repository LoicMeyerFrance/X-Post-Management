import os
import sys
import json
import logging
import threading
import queue
from time import sleep
from random import uniform
from datetime import datetime
from dotenv import load_dotenv

import paths

load_dotenv(os.path.join(paths.BASE_DIR, '.env'))

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

# Realistic user agent to avoid headless detection
_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)


def _find_chrome():
    """Auto-detect Chrome/Edge executable on the system."""
    import platform
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


def _get_config():
    chrome_path = os.getenv('CHROME_PATH', '')
    if not chrome_path:
        chrome_path = _find_chrome()
        if chrome_path:
            logger.info(f"Auto-detected Chrome at: {chrome_path}")
    return {
        'username': os.getenv('X_USERNAME', ''),
        'password': os.getenv('X_PASSWORD', ''),
        'profile_path': os.getenv('CHROME_PROFILE_DIR', ''),
        'chrome_path': chrome_path,
        'headless': os.getenv('HEADLESS', 'true').lower() == 'true',
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
            # Page closed but context alive — open a new page
            if _context.pages:
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

    args = [
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-software-rasterizer',
        '--disable-infobars',
        '--window-size=1280,800',
    ]

    if cfg['headless']:
        args.append('--headless=new')

    launch_kwargs = {
        'headless': False,  # We handle headless via --headless=new flag
        'args': args,
        'viewport': {'width': 1280, 'height': 800},
        'ignore_https_errors': True,
        'user_agent': _USER_AGENT,
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

    # Apply stealth patches to hide automation
    from playwright_stealth import Stealth
    stealth = Stealth(
        navigator_languages_override=('fr-FR', 'fr'),
    )
    stealth.apply_stealth_sync(_page)
    logger.info("Stealth patches applied")

    # Load saved session cookies from state.json if it exists (from Google login)
    state_path = os.path.join(paths.DATA_DIR, 'state.json')
    if os.path.exists(state_path):
        try:
            import json
            with open(state_path, 'r') as f:
                state_data = json.load(f)
            cookies = state_data.get('cookies', [])
            if cookies:
                _context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} cookies from state.json")
        except Exception as e:
            logger.warning(f"Could not load state.json cookies: {e}")

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


def _dismiss_popups(page):
    """Dismiss cookie banners, notification prompts, etc."""
    dismiss_selectors = [
        'div[role="button"]:has-text("Accept all cookies")',
        'div[role="button"]:has-text("Accepter tous les cookies")',
        'div[role="button"]:has-text("Refuser les cookies non essentiels")',
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


def _login(page):
    cfg = _get_config()
    logger.info("Navigating to X home to check login state...")
    page.goto("https://x.com/home", wait_until='domcontentloaded')
    _dismiss_popups(page)

    # Already logged in?
    if 'login' not in page.url and 'flow' not in page.url:
        logger.info("Already logged in")
        return {'success': True}

    logger.info("Login required, starting login flow...")
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
            _human_delay(1, 2)

            # Username step
            username_input = _wait(page, 'input[name="text"]', timeout=8000)
            if username_input:
                username_input.click()
                _human_delay(0.3, 0.6)
                username_input.fill('')
                page.keyboard.type(cfg['username'], delay=uniform(30, 70))
                _human_delay(0.5, 1)

                next_btn = _wait(page, 'div[role="button"]:has-text("Next")', timeout=5000)
                if not next_btn:
                    next_btn = _wait(page, 'div[role="button"]:has-text("Suivant")', timeout=3000)
                if next_btn:
                    next_btn.click()
                    _human_delay(0.3, 0.5)

            # Check for checkpoint/verification
            checkpoint = _wait(page, 'text="Confirm your identity"', timeout=2000)
            if not checkpoint:
                checkpoint = _wait(page, 'text="Confirmez votre identite"', timeout=1500)
            if checkpoint:
                logger.warning("Checkpoint detected - manual intervention needed")
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X security checkpoint detected. Please login manually.'}

            # Extra verification step (phone/email)
            extra_input = _wait(page, 'input[data-testid="ocfEnterTextTextInput"]', timeout=3000)
            if extra_input:
                logger.warning("Extra verification step detected (phone/email)")
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X requires additional verification (phone/email). Please login manually.'}

            # Password step
            password_input = _wait(page, 'input[type="password"]', timeout=8000)
            if password_input:
                password_input.click()
                _human_delay(0.3, 0.6)
                page.keyboard.type(cfg['password'], delay=uniform(30, 70))
                _human_delay(0.5, 1)

                login_btn = _wait(page, 'div[role="button"]:has-text("Log in")', timeout=5000)
                if not login_btn:
                    login_btn = _wait(page, 'div[role="button"]:has-text("Se connecter")', timeout=3000)
                if login_btn:
                    login_btn.click()
                    try:
                        page.wait_for_load_state('networkidle', timeout=10000)
                    except Exception:
                        pass
                    _human_delay(0.3, 0.5)

            # Dismiss any post-login popups
            _dismiss_popups(page)

            # Verify login
            for selector in [
                'a[href="/home"]',
                'div[data-testid="SideNav_AccountSwitcher_Button"]',
                'a[data-testid="AppTabBar_Home_Link"]',
            ]:
                el = _wait(page, selector, timeout=5000)
                if el:
                    logger.info("Login successful")
                    return {'success': True}

            # Check for checkpoint again after login attempt
            checkpoint = _wait(page, 'text="Confirm your identity"', timeout=2000)
            if checkpoint:
                return {'success': False, 'needs_manual_intervention': True,
                        'error': 'X security checkpoint detected after login.'}

        except Exception as e:
            logger.error(f"Login attempt {attempt + 1} failed: {e}")
            if attempt < max_attempts - 1:
                _human_delay(1, 2)
                page.goto("https://x.com/home", wait_until='domcontentloaded')
                try:
                    page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    pass

    return {'success': False, 'error': 'Login failed after maximum attempts'}


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

        # Upload image if provided
        if image_path and os.path.isfile(image_path):
            file_input = _wait(page, 'input[data-testid="fileInput"]', timeout=3000)
            if not file_input:
                file_input = _wait(page, 'input[type="file"]', timeout=3000)
            if not file_input:
                _close_if_visible()
                return {'success': False, 'error': 'Could not find file input for media upload'}

            logger.info(f"Uploading image: {os.path.basename(image_path)}")
            file_input.set_input_files(image_path)

            _wait(page, 'div[data-testid="attachments"]', timeout=15000)
            _human_delay(0.3, 0.5)

        # --- Schedule on X natively, or post immediately ---
        if scheduled_at:
            result = _schedule_on_x(page, scheduled_at)
        else:
            result = _click_post(page)

        _close_if_visible()
        return result

    except Exception as e:
        logger.error(f"post_to_x error: {e}")
        _close_if_visible()
        return {'success': False, 'error': str(e)}


def _click_post(page):
    """Click the Post button and verify success. Returns tweet_url if found."""
    post_btn = None
    for selector in [
        'button[data-testid="tweetButton"]',
        'div[data-testid="tweetButton"]',
    ]:
        post_btn = _wait(page, selector, timeout=5000)
        if post_btn:
            break

    if not post_btn:
        return {'success': False, 'error': 'Could not find Post button'}

    # Wait up to 15s for the button to become enabled (image upload, X processing…)
    for _ in range(30):
        if post_btn.get_attribute('aria-disabled') != 'true':
            break
        sleep(0.5)
    else:
        return {'success': False, 'error': 'Post button is disabled - check text/image content'}

    post_btn.scroll_into_view_if_needed()
    _human_delay(0.2, 0.4)
    post_btn.click(timeout=10000)

    tweet_url = None
    toast_el = _wait(page, 'div[data-testid="toast"]', timeout=10000)
    if toast_el:
        toast_text = toast_el.inner_text().lower()
        success_keywords = ['sent', 'posted', 'envoy', 'publi', 'schedul', 'program']
        if any(kw in toast_text for kw in success_keywords):
            # Try to get the tweet URL from the "View" link in the toast
            try:
                view_link = toast_el.query_selector('a[href*="/status/"]')
                if view_link:
                    tweet_url = view_link.get_attribute('href')
                    if tweet_url and not tweet_url.startswith('http'):
                        tweet_url = 'https://x.com' + tweet_url
                    logger.info(f"Tweet URL captured: {tweet_url}")
            except Exception as e:
                logger.warning(f"Could not capture tweet URL from toast: {e}")
            logger.info("Post published successfully (confirmed by X toast)")
            return {'success': True, 'tweet_url': tweet_url}
        else:
            return {'success': False, 'error': f'X error: {toast_el.inner_text()}'}

    compose_still_visible = _wait(page, 'div[data-testid="tweetTextarea_0"]', timeout=2000)
    if not compose_still_visible:
        logger.info("Post published successfully (compose dialog closed)")
        return {'success': True, 'tweet_url': tweet_url}

    logger.warning("Post status uncertain - compose area still visible, no toast")
    return {'success': True, 'tweet_url': tweet_url}


def _identify_select_role(options):
    """Identify what a <select> represents by analyzing its option values.
    Returns one of: 'month', 'day', 'year', 'hour', 'minute', 'ampm', 'unknown'.
    """
    if not options:
        return 'unknown'

    cleaned = [o.strip() for o in options if o.strip()]
    if not cleaned:
        return 'unknown'

    # Month names (EN + FR, case-insensitive)
    all_month_lower = {
        'january', 'february', 'march', 'april', 'may', 'june',
        'july', 'august', 'september', 'october', 'november', 'december',
        'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
        'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
    }
    if any(o.lower() in all_month_lower for o in cleaned):
        return 'month'

    # AM/PM
    upper_vals = {o.upper() for o in cleaned}
    if upper_vals and upper_vals <= {'AM', 'PM'}:
        return 'ampm'

    # Numeric analysis
    numeric_vals = [int(o) for o in cleaned if o.lstrip('0').isdigit() or o == '0' or o == '00']
    if not numeric_vals:
        return 'unknown'

    max_val = max(numeric_vals)
    min_val = min(numeric_vals)
    count = len(numeric_vals)

    # Year: 4-digit numbers >= 2020
    if min_val >= 2020:
        return 'year'

    # Day: 1-31, at least 28 options
    if 28 <= count <= 31 and min_val >= 1 and max_val <= 31:
        return 'day'

    # Hour: 0-23 (24h) or 1-12 (12h), at most 24 options
    if count <= 24 and max_val <= 23:
        return 'hour'

    # Minute: typically 0-55 or 0-59, often in steps of 5
    if max_val <= 59 and count <= 60:
        return 'minute'

    return 'unknown'


def _schedule_on_x(page, scheduled_at):
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

    # Month name maps (index 0 unused, 1=Jan … 12=Dec)
    month_names_en = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                      'July', 'August', 'September', 'October', 'November', 'December']
    month_names_fr = ['', 'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
                      'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']

    # ── Identify each <select> by its option values (language-independent) ──
    selects = page.query_selector_all('select')
    logger.info(f"Found {len(selects)} select elements in schedule dialog")

    roles = {}  # role -> {'el': element, 'options': [str]}
    for i, sel in enumerate(selects):
        opts = page.evaluate('(el) => Array.from(el.options).map(o => o.value)', sel)
        role = _identify_select_role(opts)
        if role != 'unknown' and role not in roles:
            roles[role] = {'el': sel, 'options': opts}
            logger.info(f"  Select #{i}: {role} (sample: {opts[:3]})")
        else:
            logger.info(f"  Select #{i}: {role or 'unknown'} (sample: {(opts or [])[:3]})")

    # ── Set Month ──
    if 'month' in roles:
        month_el = roles['month']['el']
        month_opts = [o.lower() for o in roles['month']['options']]
        month_set = False
        # Try English name
        try:
            month_el.select_option(value=month_names_en[dt.month])
            month_set = True
        except Exception:
            pass
        # Try French name (exact)
        if not month_set:
            try:
                month_el.select_option(value=month_names_fr[dt.month])
                month_set = True
            except Exception:
                pass
        # Try French name capitalized
        if not month_set:
            try:
                month_el.select_option(value=month_names_fr[dt.month].capitalize())
                month_set = True
            except Exception:
                pass
        # Try case-insensitive match against actual options
        if not month_set:
            target = month_names_fr[dt.month].lower()
            for opt_val in roles['month']['options']:
                if opt_val.lower().strip() == target:
                    try:
                        month_el.select_option(value=opt_val)
                        month_set = True
                        break
                    except Exception:
                        pass
        # Last resort: select by index
        if not month_set:
            try:
                month_el.select_option(index=dt.month - 1)
            except Exception:
                logger.warning("Failed to set month")
        _human_delay(0.3, 0.5)
    else:
        logger.warning("Could not identify month select")

    # ── Set Day ──
    if 'day' in roles:
        try:
            roles['day']['el'].select_option(value=str(dt.day))
        except Exception:
            logger.warning("Failed to set day")
        _human_delay(0.3, 0.5)
    else:
        logger.warning("Could not identify day select")

    # ── Set Year ──
    if 'year' in roles:
        try:
            roles['year']['el'].select_option(value=str(dt.year))
        except Exception:
            logger.warning("Failed to set year")
        _human_delay(0.3, 0.5)
    else:
        logger.warning("Could not identify year select")

    # ── Set Hour (detect 12h vs 24h by presence of AM/PM select) ──
    use_24h = 'ampm' not in roles
    if 'hour' in roles:
        if use_24h:
            hour_value = dt.hour
        else:
            hour_value = dt.hour % 12
            if hour_value == 0:
                hour_value = 12
        try:
            roles['hour']['el'].select_option(value=str(hour_value))
        except Exception:
            logger.warning("Failed to set hour")
        _human_delay(0.3, 0.5)
    else:
        logger.warning("Could not identify hour select")

    # ── Set Minute ──
    if 'minute' in roles:
        try:
            roles['minute']['el'].select_option(value=str(dt.minute).zfill(2))
        except Exception:
            try:
                roles['minute']['el'].select_option(value=str(dt.minute))
            except Exception:
                logger.warning("Failed to set minute")
        _human_delay(0.3, 0.5)
    else:
        logger.warning("Could not identify minute select")

    # ── Set AM/PM (12h format only) ──
    if not use_24h and 'ampm' in roles:
        ampm = 'AM' if dt.hour < 12 else 'PM'
        try:
            roles['ampm']['el'].select_option(value=ampm)
        except Exception:
            logger.warning("Failed to set AM/PM")
        _human_delay(0.3, 0.5)

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
    return _click_post(page)


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

        # Detect verification badge
        is_verified = False
        verified_type = ''
        try:
            # Method 1: look for the verified badge SVG near the username
            badge_selectors = [
                'div[data-testid="UserName"] svg[aria-label*="Verified"]',
                'div[data-testid="UserName"] svg[aria-label*="erifi"]',
                'div[data-testid="UserName"] svg[aria-label*="Certifi"]',
            ]
            for sel in badge_selectors:
                badge_el = _wait(page, sel, timeout=2000)
                if badge_el:
                    aria = badge_el.get_attribute('aria-label') or ''
                    is_verified = True
                    verified_type = 'blue'  # default
                    # Gold badge = business, grey = government
                    if 'business' in aria.lower() or 'entreprise' in aria.lower():
                        verified_type = 'business'
                    elif 'government' in aria.lower() or 'gouvernement' in aria.lower():
                        verified_type = 'government'
                    logger.info(f"Verification badge detected: {verified_type} ({aria})")
                    break

            # Method 2: intercept GraphQL data embedded in the page
            if not is_verified:
                result_json = page.evaluate("""() => {
                    try {
                        const scripts = document.querySelectorAll('script[type="application/json"]');
                        for (const s of scripts) {
                            const txt = s.textContent || '';
                            if (txt.includes('is_blue_verified')) {
                                return txt;
                            }
                        }
                    } catch(e) {}
                    return '';
                }""")
                if result_json:
                    try:
                        data = json.loads(result_json)
                        data_str = json.dumps(data)
                        if '"is_blue_verified":true' in data_str:
                            is_verified = True
                            verified_type = 'blue'
                            logger.info("Verification detected via embedded GraphQL data")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Badge detection failed (non-critical): {e}")

        # Get bio
        bio = ''
        try:
            bio_el = _wait(page, 'div[data-testid="UserDescription"]', timeout=5000)
            if bio_el:
                bio = bio_el.inner_text().strip()
                logger.info(f"Bio: {bio[:80]}")
        except Exception as e:
            logger.warning(f"Could not scrape bio (non-critical): {e}")

        # Get join date (e.g. "Joined March 2020" / "A rejoint Twitter en mars 2020")
        join_date = ''
        try:
            join_el = _wait(page, 'span[data-testid="UserJoinDate"]', timeout=5000)
            if join_el:
                join_date = join_el.inner_text().strip()
                logger.info(f"Join date: {join_date}")
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


def _do_open_google_login():
    """Open a plain browser on X login page, click 'Sign in with Google',
    wait for user to complete login, then save session to state.json.
    Based on the ddd/save-session.js + login-google-auto.js approach:
    plain browser (not persistent) + storageState save/load.
    """
    global _playwright, _context, _page
    try:
        # Close any existing bot browser first
        _close_browser_internal()

        from playwright.sync_api import sync_playwright
        cfg = _get_config()
        pw = sync_playwright().start()

        chrome_path = cfg['chrome_path']

        # Launch a plain browser (NOT persistent context) like ddd/save-session.js
        launch_args = {
            'headless': False,
            'args': [
                '--disable-blink-features=AutomationControlled',
                '--window-size=1280,900',
            ],
        }
        # On macOS, use Playwright's bundled Chromium (system Chrome blocks in .app bundles)
        import platform
        if chrome_path and platform.system() != 'Darwin':
            launch_args['executable_path'] = chrome_path
            logger.info(f"Launching Chrome for Google login: {chrome_path}")
        else:
            logger.info("Launching Playwright Chromium for Google login")

        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 900},
            ignore_https_errors=True,
            user_agent=_USER_AGENT,
        )
        page = context.new_page()

        # Step 1: Go to X login page
        logger.info("Navigating to X login page...")
        page.goto("https://x.com/i/flow/login", wait_until='domcontentloaded', timeout=30000)
        _human_delay(3, 4)

        # Step 2: Click "Sign in with Google" button
        logger.info("Looking for 'Sign in with Google' button...")
        google_selectors = [
            '[data-provider="google"]',
            'button:has-text("Google")',
            'a:has-text("Google")',
            '[aria-label*="Google"]',
            'div[role="button"]:has-text("Google")',
        ]

        clicked = False
        for selector in google_selectors:
            try:
                el = page.wait_for_selector(selector, timeout=5000)
                if el:
                    el.click()
                    logger.info(f"Google button found and clicked ({selector})")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.info("Google button not found automatically — user must click manually")

        # Step 3: Wait for user to complete Google login (up to 5 minutes)
        logger.info("Waiting for user to complete Google login (max 5 min)...")
        try:
            page.wait_for_url('**/home**', timeout=300000)
            logger.info("Login successful! User redirected to /home")

            # Step 4: Save session to state.json (cookies + localStorage)
            state_path = os.path.join(paths.DATA_DIR, 'state.json')
            context.storage_state(path=state_path)
            logger.info(f"Session saved to {state_path}")

            browser.close()
            pw.stop()
            return {'success': True, 'message': 'Google login completed and session saved'}

        except Exception as timeout_err:
            logger.warning(f"Login wait timed out or was cancelled: {timeout_err}")
            browser.close()
            pw.stop()
            return {'success': False, 'error': 'Login not completed within timeout'}

    except Exception as e:
        logger.error(f"open_google_login error: {e}")
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        return {'success': False, 'error': str(e)}


def _do_check_google_connected():
    """Check if a Google account is connected by verifying state.json exists with cookies.
    state.json is only created by open_google_login(), so its existence means Google login succeeded."""
    try:
        import json
        state_path = os.path.join(paths.DATA_DIR, 'state.json')
        if not os.path.exists(state_path):
            logger.info("Google not connected: state.json does not exist")
            return {'connected': False}

        with open(state_path, 'r') as f:
            state_data = json.load(f)

        cookies = state_data.get('cookies', [])
        if cookies:
            logger.info(f"Google connected: state.json has {len(cookies)} cookies")
            return {'connected': True}

        logger.info("Google not connected: state.json has no cookies")
        return {'connected': False}

    except Exception as e:
        logger.error(f"check_google_connected error: {e}")
        return {'connected': False, 'error': str(e)}


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
        delete_option = None
        for selector in [
            'div[data-testid="Dropdown"] div[role="menuitem"]:has-text("Delete")',
            'div[data-testid="Dropdown"] div[role="menuitem"]:has-text("Supprimer")',
            'div[role="menuitem"]:has-text("Delete")',
            'div[role="menuitem"]:has-text("Supprimer")',
        ]:
            delete_option = _wait(page, selector, timeout=3000)
            if delete_option:
                break

        if not delete_option:
            # Close the menu and return error
            page.keyboard.press('Escape')
            _close_if_visible()
            return {'success': False, 'error': 'Could not find Delete option in menu'}

        delete_option.click()
        _human_delay(0.5, 1)

        # Confirm deletion in the dialog
        confirm_btn = None
        for selector in [
            'button[data-testid="confirmationSheetConfirm"]',
            'div[data-testid="confirmationSheetDialog"] button:has-text("Delete")',
            'div[data-testid="confirmationSheetDialog"] button:has-text("Supprimer")',
            'button:has-text("Delete")',
            'button:has-text("Supprimer")',
        ]:
            confirm_btn = _wait(page, selector, timeout=3000)
            if confirm_btn:
                break

        if not confirm_btn:
            _close_if_visible()
            return {'success': False, 'error': 'Could not find confirmation button'}

        confirm_btn.click()
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
        tab_result = page.evaluate('''() => {
            const tabLabels = ["Scheduled", "Programmés", "Planifiés", "Programmé"];
            const allElements = document.querySelectorAll('span, a, div[role="tab"], div[role="button"]');
            for (const el of allElements) {
                const text = el.textContent.trim();
                if (tabLabels.some(label => text === label)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        el.click();
                        return {clicked: true, tag: el.tagName, text: text};
                    }
                }
            }
            return {clicked: false};
        }''')
        logger.info(f"Scheduled tab click: {tab_result}")
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
        will_send_result = page.evaluate('''() => {
            const allElements = document.querySelectorAll('span, div, a');
            const candidates = [];
            for (const el of allElements) {
                const text = el.textContent.trim();
                if (text.startsWith("Will send on") || text.startsWith("Sera envoyé")) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        candidates.push({el: el, text: text, len: text.length});
                    }
                }
            }
            // Sort by text length — shortest = most specific element
            candidates.sort((a, b) => a.len - b.len);
            if (candidates.length > 0) {
                candidates[0].el.click();
                return {found: true, text: candidates[0].text, total: candidates.length};
            }
            return {found: false};
        }''')
        logger.info(f"'Will send on' click result: {will_send_result}")

        if not will_send_result.get('found'):
            logger.error("Could not find 'Will send on...' in editor")
            page.keyboard.press('Escape')
            _human_delay(0.5, 1)
            _close_if_visible()
            return {'success': False, 'error': 'Could not find "Will send on..." link in editor'}

        _human_delay(3, 4)

        # Step 4: In the schedule picker, click "Clear" (top-right)
        # Retry a few times in case the picker takes time to open
        logger.info("Looking for 'Clear' button in schedule picker...")
        clear_result = {'found': False}
        for attempt in range(4):
            clear_result = page.evaluate('''() => {
                const allElements = document.querySelectorAll('span, button, a, div[role="button"]');
                for (const el of allElements) {
                    const text = el.textContent.trim();
                    if (text === "Clear" || text === "Effacer") {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            el.click();
                            return {found: true, text: text};
                        }
                    }
                }
                return {found: false};
            }''')
            if clear_result.get('found'):
                break
            logger.info(f"Clear not found yet (attempt {attempt + 1}/4), waiting...")
            _human_delay(2, 3)

        logger.info(f"Clear click result: {clear_result}")

        if not clear_result.get('found'):
            logger.error("Could not find 'Clear' button after retries")
            page.keyboard.press('Escape')
            _human_delay(0.5, 1)
            _close_if_visible()
            return {'success': False, 'error': 'Could not find Clear button in schedule picker'}

        _human_delay(2, 3)

        # Step 5: Handle any confirmation dialog (Discard/Delete/Confirm)
        confirm_result = page.evaluate('''() => {
            const buttonTexts = ["Discard", "Delete", "Confirm", "Supprimer", "Confirmer"];
            for (const btnText of buttonTexts) {
                const buttons = document.querySelectorAll('button, div[role="button"], [data-testid="confirmationSheetConfirm"]');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    if (text === btnText || btn.getAttribute("data-testid") === "confirmationSheetConfirm") {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.click();
                            return {found: true, text: text};
                        }
                    }
                }
            }
            return {found: false};
        }''')
        if confirm_result.get('found'):
            logger.info(f"Clicked confirmation: '{confirm_result.get('text')}'")
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

        func, args, result_event, result_holder = task
        try:
            result_holder['result'] = func(*args)
        except Exception as e:
            result_holder['result'] = {'success': False, 'error': str(e)}
        finally:
            result_event.set()


def _ensure_worker():
    global _worker_thread, _worker_started
    with _worker_lock:
        if not _worker_started:
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name='playwright-worker')
            _worker_thread.start()
            _worker_started = True


def _run_in_worker(func, *args):
    """Submit a task to the Playwright worker thread and wait for the result."""
    _ensure_worker()
    result_event = threading.Event()
    result_holder = {}
    _task_queue.put((func, args, result_event, result_holder))
    result_event.wait()
    return result_holder.get('result', {'success': False, 'error': 'No result'})


# ===== Public API (thread-safe, callable from any thread) =====

def post_to_x(text='', image_path='', scheduled_at=None):
    """Post or schedule on X. Returns dict with success, error, needs_manual_intervention keys."""
    return _run_in_worker(_do_post, text, image_path, scheduled_at)


def test_connection():
    """Test X connection by checking login state. Returns dict."""
    return _run_in_worker(_do_test_connection)


def fetch_profile():
    """Fetch profile picture and info from X. Returns dict."""
    return _run_in_worker(_do_fetch_profile)


def restart_browser():
    """Close the browser so it gets re-created with new settings on next use."""
    return _run_in_worker(_do_close)


def delete_tweet(tweet_url):
    """Delete a tweet from X. Returns dict with success, error keys."""
    return _run_in_worker(_do_delete_tweet, tweet_url)


def delete_scheduled_tweet(post_text):
    """Delete a scheduled tweet from X by matching text. Returns dict with success, error keys."""
    return _run_in_worker(_do_delete_scheduled_tweet, post_text)


def open_google_login():
    """Open Chrome in visible mode on Google login page. Returns dict."""
    return _run_in_worker(_do_open_google_login)


def check_google_connected():
    """Check if Google account is connected. Returns dict with 'connected' boolean."""
    return _do_check_google_connected()


def close():
    """Shutdown the Playwright worker thread and close browser."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            _task_queue.put(None)
            _worker_thread.join(timeout=10)
            _worker_started = False
