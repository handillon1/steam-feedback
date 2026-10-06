from pathlib import Path

import pytest

from collector import store_page
from collector.store_page import StorePageError, parse_tags

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_tags_from_saved_page():
    tags = parse_tags((FIXTURES / "store_page_413150.html").read_text(encoding="utf-8"))
    assert len(tags) >= 10
    assert tags[0]["name"] == "Farming Sim"
    assert all(isinstance(t["count"], int) and t["count"] > 0 for t in tags)
    assert [t["count"] for t in tags] == sorted((t["count"] for t in tags), reverse=True)


def test_parse_tags_fails_loudly_on_changed_layout():
    with pytest.raises(StorePageError):
        parse_tags("<html><div class='glance_tags popular_tags'></div></html>")


def test_parse_tags_fails_on_zero_tags():
    with pytest.raises(StorePageError):
        parse_tags("<script>InitAppTagModal( 413150, [], ...)</script>")


def test_fetch_store_page_rejects_age_gate_redirect(monkeypatch):
    monkeypatch.setattr(store_page.steam_http, "get_html",
                        lambda url, cookies=None: ("<html></html>", "https://store.steampowered.com/agecheck/app/1174180/"))
    with pytest.raises(StorePageError):
        store_page.fetch_store_page(1174180)


def test_fetch_store_page_returns_html_on_app_url(monkeypatch):
    monkeypatch.setattr(store_page.steam_http, "get_html",
                        lambda url, cookies=None: ("<html>ok</html>", "https://store.steampowered.com/app/1174180/"))
    assert store_page.fetch_store_page(1174180) == "<html>ok</html>"


def test_fetch_store_page_accepts_slug_url(monkeypatch):
    monkeypatch.setattr(store_page.steam_http, "get_html",
                        lambda url, cookies=None: ("<html>ok</html>", "https://store.steampowered.com/app/1174180/Red_Dead_Redemption_2/"))
    assert store_page.fetch_store_page(1174180) == "<html>ok</html>"
