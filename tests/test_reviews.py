import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from collector import reviews, steam_http
from collector.parse import REVIEW_FIELDS, parse_reviews
from collector.reviews import COLUMNS, collect, fetch_window, split_by_day, window, write_day_files

FIXTURES = Path(__file__).parent / "fixtures"
DAY = 86400
OCT8 = int(datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp())
START, END = OCT8 - 3 * DAY, OCT8  # the window of any run on Oct 8


def fake_page(timestamps, cursor):
    """A GetAppReviews body with one English review per timestamp; the id comes from the timestamp."""
    return {"response": {"cursor": cursor, "reviews": [
        {"recommendationid": f"r{ts}", "language": "english", "review": "ok", "timestamp_created": ts,
         "timestamp_updated": ts, "voted_up": True, "votes_up": 0, "votes_funny": 0, "weighted_vote_score": 0.5,
         "comment_count": 0, "steam_purchase": True, "received_for_free": False,
         "written_during_early_access": False, "primarily_steam_deck": False, "refunded": False,
         "author": {"steamid": "7656119", "playtime_at_review": 60}}
        for ts in timestamps]}}


@pytest.fixture
def serve(monkeypatch):
    """serve(*pages): get_json returns these pages in order. Returns the list of cursors it was called with."""
    def install(*pages):
        cursors, queue = [], list(pages)

        def fake_get_json(url, params):
            cursors.append(params["cursor"])
            assert queue, "fetched a page after it should have stopped"
            return queue.pop(0)

        monkeypatch.setattr(reviews.steam_http, "get_json", fake_get_json)
        return cursors
    return install


def ids(rows):
    return [r["recommendationid"] for r in rows]


# window

def test_window_is_the_three_complete_utc_days_before_today():
    assert window(OCT8 + 17 * 3600) == (START, END)


def test_window_at_exactly_midnight_excludes_the_new_day():
    assert window(OCT8) == (START, END)


# fetch_window

def test_fetch_stops_once_reviews_are_older_than_start(serve):
    cursors = serve(fake_page([END - 10, END - 100], "c1"),
                    fake_page([START + 5, START - 5], "c2"))
    rows, stats = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 10}", f"r{END - 100}", f"r{START + 5}"]
    assert cursors == ["*", "c1"]
    assert stats == {"pages": 2, "rows": 3, "skipped": 0, "capped": False}


def test_fetch_drops_reviews_from_today(serve):
    serve(fake_page([END + 100, END, END - 1], "c1"), fake_page([START - 1], "c2"))
    rows, _ = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 1}"]


def test_fetch_stops_on_missing_reviews_key(serve):
    cursors = serve(fake_page([END - 1], "c1"), {"response": {"cursor": "c2"}})
    rows, _ = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 1}"]
    assert cursors == ["*", "c1"]


def test_fetch_stops_on_unchanged_cursor(serve):
    cursors = serve(fake_page([END - 1], "c1"), fake_page([END - 2], "c1"))
    rows, _ = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 1}", f"r{END - 2}"]
    assert cursors == ["*", "c1"]


def test_fetch_removes_duplicates_across_pages(serve):
    # A review posted mid-run pushes the page boundary down, so the next page repeats one.
    serve(fake_page([END - 1, END - 2], "c1"), fake_page([END - 2, END - 3], "c2"), fake_page([START - 1], "c3"))
    rows, stats = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 1}", f"r{END - 2}", f"r{END - 3}"]
    assert stats["rows"] == 3


def test_fetch_tags_rows_with_appid_and_drops_author(serve):
    serve(fake_page([END - 1, START - 1], "c1"))
    rows, _ = fetch_window(42, START, END)
    assert rows[0]["appid"] == 42
    assert list(rows[0]) == COLUMNS
    assert rows[0]["playtime_at_review"] == 60


def test_fetch_counts_skipped_non_english(serve):
    page = fake_page([END - 1, END - 2, START - 1], "c1")
    page["response"]["reviews"][0]["language"] = "turkish"
    serve(page)
    rows, stats = fetch_window(42, START, END)
    assert ids(rows) == [f"r{END - 2}"]
    assert stats["skipped"] == 1


def test_fetch_reports_capped_at_the_page_limit(serve, caplog):
    cursors = serve(fake_page([END - 1], "c1"), fake_page([END - 2], "c2"), fake_page([END - 3], "c3"))
    with caplog.at_level(logging.WARNING):
        rows, stats = fetch_window(42, START, END, max_pages=2)
    assert stats["capped"] is True
    assert cursors == ["*", "c1"]
    assert "42" in caplog.text


def test_fetch_that_finishes_on_the_last_allowed_page_is_not_capped(serve):
    serve(fake_page([END - 1], "c1"), fake_page([START - 1], "c2"))
    _, stats = fetch_window(42, START, END, max_pages=2)
    assert stats["capped"] is False


# split_by_day

def test_split_by_day_uses_utc_midnight():
    oct6 = START + DAY
    days = split_by_day([{"timestamp_created": oct6 - 1}, {"timestamp_created": oct6}])
    assert {d: len(rows) for d, rows in days.items()} == {"2026-10-05": 1, "2026-10-06": 1}


# write_day_files

def rows_for(day_start, n, developer_response=None):
    return [{"appid": 1, "recommendationid": f"r{day_start}_{i}", "timestamp_created": day_start + i,
             "voted_up": True, "developer_response": developer_response} for i in range(n)]


def test_schema_covers_every_column():
    assert COLUMNS == ["appid"] + REVIEW_FIELDS + ["playtime_at_review"]


def test_write_skips_an_existing_day(tmp_path):
    existing = tmp_path / "date=2026-10-05.parquet"
    existing.write_bytes(b"already here")
    written = write_day_files({"2026-10-05": rows_for(START, 2), "2026-10-06": rows_for(START + DAY, 2)}, tmp_path)
    assert [p.name for p in written] == ["date=2026-10-06.parquet"]
    assert existing.read_bytes() == b"already here"


def test_days_with_and_without_developer_responses_read_back_together(tmp_path):
    write_day_files({"2026-10-05": rows_for(START, 2),  # every developer_response is None
                     "2026-10-06": rows_for(START + DAY, 3, developer_response="thanks!")}, tmp_path)
    assert str(pq.read_schema(tmp_path / "date=2026-10-05.parquet").field("developer_response").type) == "string"
    df = pd.read_parquet(tmp_path)
    assert len(df) == 5
    assert df["developer_response"].notna().sum() == 3
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("fixture", ["reviews_413150_page1.json", "reviews_413150_page2.json",
                                     "reviews_892970_dev_response.json"])
def test_schema_accepts_rows_parsed_from_real_pages(fixture):
    # If Steam changes a type (vote score as a string, say), this fails here instead of mid-run.
    page = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    rows, _ = parse_reviews(page)
    table = pa.Table.from_pylist([{"appid": 1, **r} for r in rows], schema=reviews.SCHEMA)
    assert table.num_rows == len(rows) > 0


# collect: circuit breaker

class Capped(list):
    """An outcome for fake_fetch: these rows, but the game hit the page cap."""


def fake_fetch(monkeypatch, outcomes):
    """fetch_window replaced: outcomes[appid] is an exception to raise, or a list of rows. Returns the appids called."""
    called = []

    def fetch(appid, start, end):
        called.append(appid)
        result = outcomes[appid]
        if isinstance(result, Exception):
            raise result
        stats = {"pages": 1, "rows": len(result), "skipped": 0, "capped": isinstance(result, Capped)}
        return [{"appid": appid, **r} for r in result], stats
    monkeypatch.setattr(reviews, "fetch_window", fetch)
    return called


def test_breaker_trips_after_consecutive_transient_failures(monkeypatch):
    down = steam_http.TransientHTTPError("503")
    called = fake_fetch(monkeypatch, {a: down for a in range(1, 11)})
    _, _, failed, tripped = collect(list(range(1, 11)), START, END)
    assert tripped
    assert called == [1, 2, 3, 4, 5]


def test_one_failed_game_mid_run_does_not_trip_the_breaker(monkeypatch):
    down = steam_http.TransientHTTPError("503")
    outcomes = {1: [], 2: down, 3: down, 4: down, 5: down, 6: [], 7: down, 8: []}
    called = fake_fetch(monkeypatch, outcomes)
    _, _, failed, tripped = collect(list(outcomes), START, END)
    assert not tripped
    assert called == list(outcomes)
    assert failed == [2, 3, 4, 5, 7]


def test_permanent_errors_are_skipped_not_failed(monkeypatch):
    fake_fetch(monkeypatch, {1: steam_http.PermanentError(404, "url"), 2: []})
    _, summary, failed, tripped = collect([1, 2], START, END)
    assert failed == [] and not tripped
    assert "404" in summary[1]["error"]


# main: what gets written

def review(ts):
    return {"recommendationid": f"r{ts}", "timestamp_created": ts}


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(outcomes): main() at 17:00 on Oct 8 over games 1..n. Returns (exit code, files written, run summary)."""
    def go(outcomes):
        games = tmp_path / "games.csv"
        games.write_text("appid\n" + "".join(f"{a}\n" for a in outcomes), encoding="utf-8")
        out = tmp_path / "reviews"
        monkeypatch.setattr(reviews, "GAMES_CSV", games)
        monkeypatch.setattr(reviews, "REVIEWS_DIR", out)
        monkeypatch.setattr(reviews.time, "time", lambda: OCT8 + 17 * 3600)
        fake_fetch(monkeypatch, outcomes)
        code = 0
        try:
            reviews.main()
        except SystemExit as e:
            code = e.code
        summary = json.loads(next((out / "_runs").glob("*.json")).read_text(encoding="utf-8"))
        return code, sorted(p.name for p in out.glob("*.parquet")), summary
    return go


THREE_DAYS = [review(START + 10), review(START + DAY + 10), review(START + 2 * DAY + 10)]


def test_clean_run_writes_every_day(run):
    code, files, summary = run({1: THREE_DAYS, 2: THREE_DAYS})
    assert code == 0
    assert files == ["date=2026-10-05.parquet", "date=2026-10-06.parquet", "date=2026-10-07.parquet"]
    assert summary["missing"] == []


def test_a_day_without_reviews_still_gets_a_file(run, tmp_path):
    code, files, summary = run({1: [review(START + 10)]})
    assert code == 0
    assert files == ["date=2026-10-05.parquet", "date=2026-10-06.parquet", "date=2026-10-07.parquet"]
    assert len(pd.read_parquet(tmp_path / "reviews" / "date=2026-10-06.parquet")) == 0


def test_failed_game_still_lets_the_last_chance_day_be_written(run, tmp_path):
    code, files, summary = run({1: THREE_DAYS, 2: ValueError("filter broke"), 3: THREE_DAYS})
    assert code == 1
    assert files == ["date=2026-10-05.parquet"]  # the newer days wait for a clean run
    assert sorted(pd.read_parquet(tmp_path / "reviews")["appid"]) == [1, 3]
    assert summary["missing"] == [[2, "2026-10-05"]]


def test_capped_game_is_left_out_of_the_last_chance_day(run, tmp_path):
    code, files, summary = run({1: THREE_DAYS, 2: Capped(THREE_DAYS)})
    assert code == 1
    assert files == ["date=2026-10-05.parquet"]
    assert sorted(pd.read_parquet(tmp_path / "reviews")["appid"]) == [1]
    assert summary["missing"] == [[2, "2026-10-05"]]


def test_last_chance_day_already_written_means_nothing_missing(run, tmp_path):
    (tmp_path / "reviews").mkdir()
    (tmp_path / "reviews" / "date=2026-10-05.parquet").write_bytes(b"from a clean run")
    code, files, summary = run({1: THREE_DAYS, 2: ValueError("filter broke")})
    assert code == 1
    assert summary["missing"] == []


def test_tripped_breaker_writes_nothing_and_records_the_lost_day(run):
    down = steam_http.TransientHTTPError("503")
    code, files, summary = run({a: down for a in range(1, 7)})
    assert code == 1
    assert files == []
    assert summary["tripped"] is True
    assert summary["missing"] == [[a, "2026-10-05"] for a in range(1, 7)]
