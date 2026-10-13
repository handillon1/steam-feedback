import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from collector import news, steam_http
from collector.storage import write_parquet

NOW = int(datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc).timestamp())
DAY = 86400
DOWN = steam_http.TransientHTTPError("503")


def post(appid, date):
    return {"gid": f"{appid}-{date}", "date": date, "title": "t", "contents": "body", "url": "u", "author": "a",
            "feedname": "steam_community_announcements", "tags": ["patchnotes"]}


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(outcomes): main() at NOW over these games; outcomes[appid] is a list of post dates or an exception.

    Returns (exit code, rows written or None, run summary, params of each call).
    """
    def go(outcomes):
        games = tmp_path / "games.csv"
        games.write_text("appid\n" + "".join(f"{a}\n" for a in outcomes), encoding="utf-8")
        monkeypatch.setattr(news, "GAMES_CSV", games)
        monkeypatch.setattr(news, "NEWS_DIR", tmp_path / "news")
        monkeypatch.setattr(news.time, "time", lambda: NOW)
        calls = []

        def fake_get_json(url, params):
            calls.append(params)
            result = outcomes[params["appid"]]
            if isinstance(result, Exception):
                raise result
            # One page, then an empty one if fetch_news pages on.
            items = [] if "enddate" in params else [post(params["appid"], d) for d in result]
            return {"appnews": {"newsitems": items}}
        monkeypatch.setattr(news.steam_http, "get_json", fake_get_json)
        code = 0
        try:
            news.main()
        except SystemExit as e:
            code = e.code
        path = tmp_path / "news" / "fetched=2026-10-13.parquet"
        rows = pd.read_parquet(path) if path.exists() else None
        runs = sorted((tmp_path / "news" / "_runs").glob("*.json"))
        summary = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
        return code, rows, summary, calls
    return go


def test_keeps_posts_from_the_last_7_days(run):
    code, rows, summary, _ = run({1: [NOW - DAY, NOW - 6 * DAY, NOW - 8 * DAY], 2: [NOW - 2 * DAY]})
    assert code == 0
    assert set(rows["gid"]) == {f"1-{NOW - DAY}", f"1-{NOW - 6 * DAY}", f"2-{NOW - 2 * DAY}"}
    assert list(rows["tags"].iloc[0]) == ["patchnotes"]
    assert summary["since"] == NOW - 7 * DAY


def test_writes_what_succeeded_and_records_what_failed(run):
    code, rows, summary, _ = run({1: [NOW - DAY], 2: DOWN, 3: [NOW - DAY]})
    assert code == 0  # a minority failed: tomorrow's overlapping window refetches them
    assert sorted(rows["appid"].unique()) == [1, 3]
    assert summary["failed"] == [2]


def test_exits_1_when_most_games_failed(run):
    code, rows, summary, _ = run({1: [NOW - DAY], 2: DOWN, 3: ValueError("bad"), 4: DOWN, 5: []})
    assert code == 1
    assert list(rows["appid"]) == [1]  # still written
    assert summary["failed"] == [2, 3, 4]


def test_breaker_stops_the_run_and_counts_the_rest_as_failed(run):
    outcomes = {a: DOWN for a in range(1, news.BREAKER + 1)} | {99: [NOW - DAY]}
    code, rows, summary, calls = run(outcomes)
    assert code == 1
    assert 99 not in [c["appid"] for c in calls]
    assert summary["tripped"] and summary["failed"] == [*range(1, news.BREAKER + 1), 99]
    assert rows is None  # every game failed: no file, so a rerun today can retry


def test_permanent_error_is_skipped_not_failed(run):
    code, rows, summary, _ = run({1: steam_http.PermanentError(404, "url"), 2: [NOW - DAY]})
    assert code == 0
    assert summary["skipped"] == [1] and summary["failed"] == []


def test_a_quiet_week_still_writes_an_empty_file(run):
    code, rows, _, _ = run({1: [], 2: [NOW - 30 * DAY]})
    assert code == 0
    assert rows is not None and rows.empty


def test_skips_a_day_already_collected(run, tmp_path):
    write_parquet([], news.SCHEMA, tmp_path / "news" / "fetched=2026-10-13.parquet")
    code, _, summary, calls = run({1: [NOW - DAY]})
    assert code == 0 and calls == [] and summary is None
