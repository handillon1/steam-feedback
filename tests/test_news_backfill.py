import pandas as pd
import pytest

from collector import news_backfill, steam_http
from collector.news_backfill import backfill, fetch_news, to_row

DAY = 86400
SINCE = news_backfill.START


def post(date, gid=None, **extra):
    return {"gid": gid or f"g{date}", "date": date, "title": f"post {date}", "contents": "full body",
            "url": "https://example.com", "author": "dev", "feedname": "steam_community_announcements", **extra}


def news_page(*posts):
    return {"appnews": {"appid": 42, "newsitems": list(posts)}}


@pytest.fixture
def serve(monkeypatch):
    """serve(*pages): get_json returns these pages in order (or raises them). Returns the params of each call."""
    def install(*pages):
        calls, queue = [], list(pages)

        def fake_get_json(url, params):
            calls.append(params)
            assert queue, "fetched a page after it should have stopped"
            page = queue.pop(0)
            if isinstance(page, Exception):
                raise page
            return page

        monkeypatch.setattr(news_backfill.steam_http, "get_json", fake_get_json)
        return calls
    return install


# fetch_news

def test_asks_for_full_bodies_from_the_official_feed(serve):
    calls = serve(news_page())
    fetch_news(42, SINCE)
    assert calls[0]["feeds"] == "steam_community_announcements"
    assert "maxlength" not in calls[0]
    assert "enddate" not in calls[0]


def test_pages_back_with_enddate_and_dedupes_the_inclusive_boundary(serve):
    calls = serve(news_page(post(SINCE + 3 * DAY), post(SINCE + 2 * DAY)),
                  news_page(post(SINCE + 2 * DAY), post(SINCE + DAY)),  # first post repeats: enddate is inclusive
                  news_page(post(SINCE + DAY)))
    rows, pages = fetch_news(42, SINCE)
    assert [c.get("enddate") for c in calls] == [None, SINCE + 2 * DAY, SINCE + DAY]
    assert [r["date"] for r in rows] == [SINCE + 3 * DAY, SINCE + 2 * DAY, SINCE + DAY]
    assert pages == 3


def test_stops_once_past_since_and_drops_older_posts(serve):
    serve(news_page(post(SINCE + DAY), post(SINCE), post(SINCE - DAY)))
    rows, _ = fetch_news(42, SINCE)
    assert [r["date"] for r in rows] == [SINCE + DAY, SINCE]


def test_gives_up_after_max_pages(serve):
    serve(news_page(post(SINCE + 3 * DAY)), news_page(post(SINCE + 2 * DAY)))
    with pytest.raises(ValueError):
        fetch_news(42, SINCE, max_pages=2)


def test_to_row_fills_missing_tags_and_keeps_full_contents():
    row = to_row(post(SINCE, gid=123), 42)
    assert row["gid"] == "123" and row["appid"] == 42 and row["tags"] == []
    assert row["contents"] == "full body"
    assert to_row(post(SINCE, tags=["patchnotes"]), 42)["tags"] == ["patchnotes"]
    assert set(row) == set(news_backfill.SCHEMA.names)  # no is_patch: the rule is applied later


# backfill

def test_writes_one_file_per_game_including_empty_ones(serve, tmp_path):
    serve(news_page(post(SINCE + DAY, tags=["patchnotes"])), news_page(), news_page())
    failed, tripped = backfill([1, 2], SINCE, tmp_path)
    assert (failed, tripped) == ([], False)
    df = pd.read_parquet(tmp_path / "appid=1.parquet")
    assert list(df.columns) == news_backfill.SCHEMA.names
    assert list(df["tags"][0]) == ["patchnotes"]
    assert len(pd.read_parquet(tmp_path / "appid=2.parquet")) == 0


def test_skips_games_that_already_have_a_file(serve, tmp_path):
    (tmp_path / "appid=1.parquet").write_bytes(b"")
    calls = serve(news_page())
    backfill([1, 2], SINCE, tmp_path)
    assert [c["appid"] for c in calls] == [2]


def test_permanent_error_is_skipped_and_transient_is_failed(serve, tmp_path):
    serve(steam_http.PermanentError(404, "u"), steam_http.TransientHTTPError("503"), news_page())
    failed, tripped = backfill([1, 2, 3], SINCE, tmp_path)
    assert (failed, tripped) == ([2], False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["appid=3.parquet"]


def test_breaker_stops_after_transient_failures_in_a_row(serve, tmp_path):
    n = news_backfill.BREAKER
    serve(*[steam_http.TransientHTTPError("503")] * n)
    failed, tripped = backfill(list(range(n + 1)), SINCE, tmp_path)
    assert tripped and len(failed) == n
