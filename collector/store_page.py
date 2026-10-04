"""User tags from the store page HTML.

Depends on Steam's page code, so both functions fail loudly instead of returning
empty data when the page changes.
"""
import json
import re

from collector import http

# Gets past the age gate on mature games (see decisions.md).
AGE_GATE_COOKIES = {"birthtime": "0", "lastagecheckage": "1-0-1990", "wants_mature_content": "1"}

_TAG_MODAL = re.compile(r"InitAppTagModal\(\s*\d+,\s*(\[.*?\])", re.S)


class StorePageError(Exception):
    pass


def fetch_store_page(appid):
    """Store page HTML. Raises if Steam didn't land us on /app/<appid>/ (age gate, removed app...)."""
    r = http.get(
        f"https://store.steampowered.com/app/{appid}/",
        params={"cc": "us", "l": "english"},
        cookies=AGE_GATE_COOKIES,
    )
    if f"/app/{appid}/" not in r.url:
        raise StorePageError(f"app {appid}: redirected to {r.url}")
    return r.text


def parse_tags(html):
    """[{tagid, name, count, browseable}, ...] from the InitAppTagModal(...) JSON. Raises if none found."""
    m = _TAG_MODAL.search(html)
    if not m:
        raise StorePageError("InitAppTagModal not found: store page layout may have changed")
    tags = json.loads(m.group(1))
    if not tags:
        raise StorePageError("InitAppTagModal had zero tags")
    return tags
