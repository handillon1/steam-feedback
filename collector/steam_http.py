"""Shared HTTP for every Steam call: 1s politeness delay, retries only for errors that can go away (D6)."""
import logging
import time

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "steam-feedback student project (handillon1@gmail.com)"}
TIMEOUT = 30
DELAY_SECONDS = 1.0

_last_request_at = None  # time.monotonic() of the previous request, shared by every caller


class TransientHTTPError(Exception):
    """429 or 5xx: worth retrying with backoff."""


class PermanentError(Exception):
    """400, 404 and other 4xx: won't change on a retry, so the caller logs it and skips."""

    def __init__(self, status_code, url):
        super().__init__(f"{status_code} {url}")
        self.status_code = status_code
        self.url = url


def _wait_for_rate_limit():
    """Sleep only as long as needed so requests are at least DELAY_SECONDS apart."""
    global _last_request_at
    if _last_request_at is not None:
        remaining = DELAY_SECONDS - (time.monotonic() - _last_request_at)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at = time.monotonic()

def _redact(params):
    """Copy of params safe to log: the API key is replaced with ***."""
    if not params:
        return params
    return {k: ("***" if k == "key" else v) for k, v in params.items()}

@retry(
    retry=retry_if_exception_type(TransientHTTPError),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(url, params=None, cookies=None, want_json=False):
    """One rate-limited GET. Every retry attempt also waits its turn."""
    _wait_for_rate_limit()
    log.debug("GET %s params=%s", url, _redact(params))
    try:
        r = requests.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=TIMEOUT)
    except (requests.Timeout, requests.ConnectionError) as e:
        log.warning("%s for %s, will retry", type(e).__name__, url)
        raise TransientHTTPError(f"{type(e).__name__} {url}") from None
    if r.status_code == 429 or r.status_code >= 500:
        log.warning("transient %s from %s, will retry", r.status_code, url)
        raise TransientHTTPError(f"{r.status_code} {url}")
    if r.status_code >= 400:
        log.info("permanent %s from %s params=%s, not retrying", r.status_code, url, _redact(params))
        raise PermanentError(r.status_code, url)
    if not want_json:
        return r
    try:
        data = r.json()
    except ValueError:
        log.warning("invalid JSON from %s, will retry", url)
        raise TransientHTTPError(f"invalid JSON {url}") from None
    if data is None:
        log.warning("null JSON body from %s, will retry", url)
        raise TransientHTTPError(f"null JSON {url}")
    return data

def get_json(url: str, params: dict) -> dict:
    """GET a Steam API endpoint and return the parsed JSON body."""
    return _get(url, params=params, want_json=True)

def get_html(url: str, cookies: dict | None = None) -> tuple[str, str]:
    """GET a web page. Returns (html, final_url) so callers can spot redirects."""
    r = _get(url, cookies=cookies)
    return r.text, r.url
