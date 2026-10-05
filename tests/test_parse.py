import json
from pathlib import Path

import pytest

from collector.parse import REVIEW_FIELDS, english_rate_per_day, is_patch, parse_app_type, parse_reviews

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def post_titled(news, text):
    return next(p for p in news["newsitems"] if text in p["title"])


# parse_reviews

def test_reviews_drop_steamid_and_keep_playtime_at_review():
    page = load("reviews_413150_page1.json")
    rows = parse_reviews(page)
    assert len(rows) == len(page["reviews"])
    assert all("author" not in r and "steamid" not in r for r in rows)
    assert rows[0]["playtime_at_review"] == page["reviews"][0]["author"]["playtime_at_review"]
    assert rows[0]["recommendationid"] == page["reviews"][0]["recommendationid"]


def test_reviews_have_fixed_columns_and_drop_unknown_fields():
    page = load("reviews_413150_page1.json")
    page["reviews"][0]["new_personal_field"] = "someone@example.com"
    rows = parse_reviews(page)
    assert all(list(r) == REVIEW_FIELDS + ["playtime_at_review"] for r in rows)
    assert rows[0]["developer_response"] is None  # absent optional field


def test_reviews_keep_developer_response():
    rows = parse_reviews(load("reviews_892970_dev_response.json"))
    assert rows[0]["developer_response"]


def test_last_page_without_reviews_key_gives_no_rows():
    page = load("reviews_8200_end_of_stream.json")
    assert "reviews" not in page
    assert parse_reviews(page) == []


def test_non_english_review_raises():
    page = load("reviews_413150_page1.json")
    page["reviews"][0]["language"] = "schinese"
    with pytest.raises(ValueError):
        parse_reviews(page)


# english_rate_per_day

def test_rate_is_count_over_days_since_oldest():
    rows = parse_reviews(load("reviews_413150_page1.json"))
    oldest = min(r["timestamp_created"] for r in rows)
    assert english_rate_per_day(rows, now=oldest + 2 * 86400) == pytest.approx(len(rows) / 2)


def test_full_page_in_short_window_gives_real_rate_not_100():
    # CS2-like: 100 reviews spread over the last ~2.3 hours.
    now = 1_790_000_000
    span = int(2.3 * 3600)
    rows = [{"timestamp_created": now - span * i // 99} for i in range(100)]
    rate = english_rate_per_day(rows, now)
    assert rate == pytest.approx(100 * 86400 / span)
    assert 1000 < rate < 1100


def test_rate_span_is_at_least_an_hour():
    now = 1_790_000_000
    rows = [{"timestamp_created": now - 60}] * 5
    assert english_rate_per_day(rows, now) == pytest.approx(5 * 24)


def test_rate_of_no_reviews_is_zero():
    assert english_rate_per_day(parse_reviews(load("reviews_8200_end_of_stream.json")), now=0) == 0.0


# parse_app_type

def test_paid_game():
    app = parse_app_type(load("appdetails_413150.json"), 413150)
    assert app == {
        "appid": 413150,
        "name": "Stardew Valley",
        "type": "game",
        "is_free": False,
        "coming_soon": False,
        "genres": ["Indie", "RPG", "Simulation"],
    }


def test_free_game():
    app = parse_app_type(load("appdetails_730.json"), 730)
    assert app["type"] == "game"
    assert app["is_free"] is True
    assert "Free To Play" in app["genres"]


def test_missing_release_date_does_not_crash():
    details = load("appdetails_413150.json")
    del details["413150"]["data"]["release_date"]
    assert parse_app_type(details, 413150)["coming_soon"] is False


def test_fake_app_gives_none():
    assert parse_app_type(load("appdetails_99999999_fake.json"), 99999999) is None


# is_patch

def test_tagged_patch():
    news = load("news_892970_announcements.json")
    assert is_patch(post_titled(news, "Patch 1.0.16"))


def test_untagged_patch_title_valheim():
    post = post_titled(load("news_892970_announcements.json"), "Patch 0.221.10")
    assert not post.get("tags")
    assert is_patch(post)


def test_known_false_positive_terraria_launch_date():
    # Not a patch, but D9's version-number rule accepts it; this test pins that known behaviour.
    post = post_titled(load("news_105600_announcements.json"), "Launch Date Revealed")
    assert "patchnotes" not in post["tags"]
    assert is_patch(post)


# Known false positives (D9). They pin current behaviour; week 2 measures how often they happen.
@pytest.mark.parametrize("title", ["Community Update #12", "Roadmap Update", "Weekly Update", "Event 10.05.2026"])
def test_known_false_positive_titles(title):
    assert is_patch({"title": title, "tags": []})


@pytest.mark.parametrize("fixture, title", [
    ("news_892970_announcements.json", "Word from the Devs"),
    ("news_105600_announcements.json", "State of the Game"),
])
def test_regular_announcement_is_not_a_patch(fixture, title):
    assert not is_patch(post_titled(load(fixture), title))
