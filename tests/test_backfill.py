import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from collector import backfill, reviews, steam_http
from collector.backfill import cursor_at, game_summary

from tests.test_reviews import fake_page, ids, serve  # noqa: F401 (serve is a fixture)

DAY = 86400
END = backfill.END
START = END - 10 * DAY


def test_window_is_fixed_to_the_year_before_the_first_daily_file():
    assert backfill.START == int(datetime(2025, 10, 5, tzinfo=timezone.utc).timestamp())
    assert backfill.END == int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())


# cursor_at

def test_cursor_at_skips_pages_newer_than_end(serve):
    cursors = serve(fake_page([END + 2 * DAY], "c1"), fake_page([END + DAY], "c2"), fake_page([END + 1, END - 1], "c3"))
    assert cursor_at(42, END) == "c2"
    assert cursors == ["*", "c1", "c2"]


def test_cursor_at_returns_star_when_the_first_page_reaches_end(serve):
    serve(fake_page([END + 1, END - 1], "c1"))
    assert cursor_at(42, END) == "*"


def test_cursor_at_stops_at_end_of_stream(serve):
    serve(fake_page([END + 1], "c1"), {"response": {"cursor": "c2"}})
    assert cursor_at(42, END) == "c1"


def test_cursor_at_gives_up_after_max_pages(serve):
    serve(fake_page([END + 2], "c1"), fake_page([END + 1], "c2"))
    with pytest.raises(ValueError):
        cursor_at(42, END, max_pages=2)


def test_page_cap_counts_only_pages_from_end(serve):
    # One page of reviews newer than END, then the crossing page is fetched twice: by cursor_at and fetch_window.
    cursors = serve(fake_page([END + 1], "c1"), fake_page([END - 1], "c2"),
                    fake_page([END - 1], "c2"), fake_page([END - 2], "c3"), fake_page([END - 3], "c4"))
    rows, stats = reviews.fetch_window(42, START, END, max_pages=3, cursor=cursor_at(42, END))
    assert cursors == ["*", "c1", "c1", "c2", "c3"]
    assert ids(rows) == [f"r{END - 1}", f"r{END - 2}", f"r{END - 3}"]
    assert stats["capped"] is True


# game_summary

def test_summary_of_an_uncapped_game_is_complete_from_start():
    stats = {"pages": 1, "rows": 1, "skipped": 0, "capped": False}
    s = game_summary([{"timestamp_created": START + 5 * DAY}], stats, START)
    assert s["earliest"] == reviews.day_name(START + 5 * DAY)
    assert s["complete_from"] == reviews.day_name(START)


def test_summary_of_a_capped_game_is_complete_from_the_day_after_its_earliest():
    stats = {"pages": 50, "rows": 1, "skipped": 0, "capped": True}
    s = game_summary([{"timestamp_created": START + 5 * DAY + 3600}], stats, START)
    assert s["earliest"] == reviews.day_name(START + 5 * DAY)
    assert s["complete_from"] == reviews.day_name(START + 6 * DAY)


def test_summary_of_a_game_without_reviews():
    stats = {"pages": 1, "rows": 0, "skipped": 0, "capped": False}
    s = game_summary([], stats, START)
    assert s["earliest"] is None
    assert s["complete_from"] == reviews.day_name(START)


# backfill

@pytest.fixture
def games(monkeypatch):
    """games(outcomes): appid -> (rows, stats) or an exception to raise. Records which games were fetched."""
    fetched = []

    def install(outcomes):
        def fake_fetch(appid, start, end, max_pages, cursor):
            fetched.append(appid)
            outcome = outcomes[appid]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(backfill, "cursor_at", lambda appid, end: "*")
        monkeypatch.setattr(backfill, "fetch_window", fake_fetch)
        return fetched
    return install


def ok(appid, *timestamps, capped=False):
    rows = [{**reviews.parse_reviews(fake_page([ts], "c")["response"])[0][0], "appid": appid} for ts in timestamps]
    rows = [{c: r[c] for c in reviews.COLUMNS} for r in rows]
    return rows, {"pages": 1, "rows": len(rows), "skipped": 0, "capped": capped}


def test_writes_one_file_per_game_and_a_summary(games, tmp_path):
    games({1: ok(1, END - 1, END - DAY), 2: ok(2, START + 3600, capped=True), 3: ok(3)})
    summary, failed, tripped = backfill.backfill([1, 2, 3], START, END, tmp_path)
    assert (failed, tripped) == ([], False)
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == ["appid=1.parquet", "appid=2.parquet", "appid=3.parquet"]
    assert len(pd.read_parquet(tmp_path / "appid=1.parquet")) == 2
    assert len(pd.read_parquet(tmp_path / "appid=3.parquet")) == 0
    saved = json.loads((tmp_path / "_summary.json").read_text())
    assert saved["2"]["capped"] is True and saved["2"]["complete_from"] == reviews.day_name(START + DAY)


def test_rerun_skips_games_with_a_file_and_keeps_their_summary(games, tmp_path):
    games({1: ok(1, END - 1), 2: steam_http.TransientHTTPError("503")})
    _, failed, _ = backfill.backfill([1, 2], START, END, tmp_path)
    assert failed == [2]
    fetched = games({2: ok(2, END - 5)})
    summary, failed, _ = backfill.backfill([1, 2], START, END, tmp_path)
    assert fetched == [1, 2, 2] and failed == []  # the rerun fetched only game 2
    assert summary["1"]["rows"] == 1 and summary["2"]["rows"] == 1


def test_failed_and_removed_games_get_no_file(games, tmp_path):
    games({1: steam_http.PermanentError(404, "u"), 2: ValueError("broken page")})
    summary, failed, _ = backfill.backfill([1, 2], START, END, tmp_path)
    assert failed == [2]
    assert not list(tmp_path.glob("*.parquet"))
    assert "error" in summary["1"] and "error" in summary["2"]


def test_breaker_trips_after_consecutive_transient_failures(games, tmp_path):
    fetched = games({a: steam_http.TransientHTTPError("503") for a in range(10)})
    _, failed, tripped = backfill.backfill(list(range(10)), START, END, tmp_path)
    assert tripped and fetched == failed == list(range(reviews.BREAKER))
