"""User tags from the store page HTML.

Depends on Steam's page code, so both functions fail loudly instead of returning
empty data when the page changes.
"""
import json
import re
from urllib.parse import urlparse

from collector import steam_http

# Gets past the age gate on mature games (see decisions.md).
AGE_GATE_COOKIES = {"birthtime": "0", "lastagecheckage": "1-0-1990", "wants_mature_content": "1"}

_TAG_MODAL = re.compile(r"InitAppTagModal\(\s*\d+,\s*(\[.*?\])", re.S)


class StorePageError(Exception):
    pass


def fetch_store_page(appid):
    """Store page HTML. Raises if Steam didn't land us on /app/<appid>/ (age gate, removed app...)."""
    html, final_url = steam_http.get_html(
        f"https://store.steampowered.com/app/{appid}/?cc=us&l=english",
        cookies=AGE_GATE_COOKIES,
    )
    if not urlparse(final_url).path.startswith(f"/app/{appid}/"):
        raise StorePageError(f"app {appid}: redirected to {final_url}")
    return html


def parse_tags(html):
    """[{tagid, name, count, browseable}, ...] from the InitAppTagModal(...) JSON. Raises if none found."""
    m = _TAG_MODAL.search(html)
    if not m:
        raise StorePageError("InitAppTagModal not found: store page layout may have changed")
    tags = json.loads(m.group(1))
    if not tags:
        raise StorePageError("InitAppTagModal had zero tags")
    return tags
