"""HTTP hardening for the local API.

The Flask server has no authentication: it trusts whoever can reach
127.0.0.1:5000. Any web page the user visits can also reach that address, so
without these guards a malicious site could publish tweets, read the logs or
delete posts through the browser. Two checks close that hole:

  * Host allow-list  -> blocks DNS rebinding (evil.com resolving to 127.0.0.1)
  * Origin / Sec-Fetch-Site check -> blocks cross-site requests (CSRF)

Requests without an Origin header (curl, the desktop webview's own loads) are
allowed: browsers always send Origin on cross-origin requests, so their absence
means the request did not come from another site.
"""

import ipaddress
import logging
from urllib.parse import urlsplit

from flask import jsonify, request

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

ALLOWED_HOSTNAMES = frozenset({'localhost'})

CONTENT_SECURITY_POLICY = '; '.join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))


def _hostname(value):
    """Extract the bare hostname from a Host header or an Origin URL."""
    if not value:
        return ''
    if '://' in value:
        value = urlsplit(value).hostname or ''
        return value.lower()
    # Host header: strip the port, keeping IPv6 brackets in mind.
    if value.startswith('['):
        return value.partition(']')[0].lstrip('[').lower()
    return value.rsplit(':', 1)[0].lower() if ':' in value else value.lower()


def is_loopback_host(value):
    """True when a Host header or an Origin URL points at this machine."""
    return is_loopback(_hostname(value))


def is_loopback(hostname):
    if not hostname:
        return False
    if hostname in ALLOWED_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _reject(reason, detail):
    logger.warning("Blocked request (%s): %s %s from origin=%r host=%r",
                   reason, request.method, request.path,
                   request.headers.get('Origin', ''), request.host)
    return jsonify({'error': detail}), 403


def check_request():
    """Flask before_request hook. Returns a response to abort, or None."""
    if not is_loopback(_hostname(request.host)):
        return _reject('bad host', 'Invalid Host header')

    origin = request.headers.get('Origin', '')
    if origin and origin != 'null' and not is_loopback(_hostname(origin)):
        return _reject('cross-origin', 'Cross-origin requests are not allowed')

    if request.headers.get('Sec-Fetch-Site', '') in ('cross-site', 'same-site'):
        return _reject('sec-fetch-site', 'Cross-site requests are not allowed')

    if request.method not in SAFE_METHODS:
        # A cross-site form post carries Origin; a same-origin fetch does too.
        # Only non-browser clients omit it, and those cannot be driven by a
        # malicious page.
        referer = request.headers.get('Referer', '')
        if referer and not is_loopback(_hostname(referer)):
            return _reject('cross-origin referer', 'Cross-origin requests are not allowed')

    return None


def add_headers(response):
    """Flask after_request hook: security and cache headers."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    response.headers.setdefault('Cross-Origin-Resource-Policy', 'same-origin')

    content_type = response.content_type or ''
    if 'text/html' in content_type:
        # Never cache the shell, so a rebuilt frontend is picked up at once.
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    elif request.path.startswith('/assets/') and response.status_code == 200:
        # Vite fingerprints these filenames, so they can be cached forever.
        # Set rather than setdefault: Flask's static handler already put a
        # `no-cache` there.
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif request.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control', 'no-store')

    return response
