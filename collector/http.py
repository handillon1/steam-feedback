"""Shared HTTP for every Steam call: 1s politeness delay, retries only for errors that can go away."""
import logging
import time

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "steam-feedback student project (handillon1@gmail.com)"}
TIMEOUT = 30
DELAY_SECONDS = 1.0


class TransientHTTPError(Exception):
    """429 or 5xx: worth retrying with backoff."""


@retry(
    retry=retry_if_exception_type((TransientHTTPError, requests.Timeout, requests.ConnectionError)),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def get(url, params=None, cookies=None):
    """GET with retry on timeouts, 429 and 5xx.

    Anything else (200, 404 with result 42, 400...) is returned as-is: those won't
    change on a retry, so the caller logs them and moves on.
    """
    time.sleep(DELAY_SECONDS)
    r = requests.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=TIMEOUT)
    if r.status_code == 429 or r.status_code >= 500:
        log.warning("transient %s from %s, will retry", r.status_code, url)
        raise TransientHTTPError(f"{r.status_code} {url}")
    if r.status_code >= 400:
        log.info("permanent %s from %s params=%s, not retrying", r.status_code, url, params)
    return r
