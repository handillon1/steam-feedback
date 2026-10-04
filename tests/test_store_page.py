from pathlib import Path

import pytest

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
