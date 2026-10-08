import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from collector import players, steam_http
from collector.parse import parse_player_count
from collector.storage import write_parquet

FIXTURES = Path(__file__).parent / "fixtures"
NOW = int(datetime(2026, 10, 8, 17, 59, 59, tzinfo=timezone.utc).timestamp())


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_player_count():
    assert parse_player_count(load("players_413150.json")) == 64873


def test_parse_player_count_of_unknown_app_is_none():
    assert parse_player_count({"response": {"result": 42}}) is None


def test_hour_name_is_utc():
    assert players.hour_name(NOW) == "2026-10-08T17"
    assert players.hour_name(NOW + 1) == "2026-10-08T18"


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(outcomes): main() at 17:59:59 UTC over games 1..n; outcomes[appid] is a count or an exception.

    Returns (exit code, rows written or None, run summary, appids fetched).
    """
    def go(outcomes):
        games = tmp_path / "games.csv"
        games.write_text("appid\n" + "".join(f"{a}\n" for a in outcomes), encoding="utf-8")
        monkeypatch.setattr(players, "GAMES_CSV", games)
        monkeypatch.setattr(players, "PLAYERS_DIR", tmp_path / "players")
        monkeypatch.setattr(players.time, "time", lambda: NOW)
        called = []

        def fake_get_json(url, params):
            called.append(params["appid"])
            result = outcomes[params["appid"]]
            if isinstance(result, Exception):
                raise result
            return {"response": {"player_count": result, "result": 1}}
        monkeypatch.setattr(players.steam_http, "get_json", fake_get_json)
        code = 0
        try:
            players.main()
        except SystemExit as e:
            code = e.code
        path = tmp_path / "players" / "hour=2026-10-08T17.parquet"
        rows = pd.read_parquet(path) if path.exists() else None
        runs = sorted((tmp_path / "players" / "_runs").glob("*.json"))
        summary = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
        return code, rows, summary, called
    return go


DOWN = steam_http.TransientHTTPError("503")


def test_writes_what_succeeded_and_records_what_failed(run):
    code, rows, summary, _ = run({1: 100, 2: DOWN, 3: 300})
    assert code == 0  # a minority failed: a missed snapshot can't be refetched, so keep the rest
    assert list(rows["appid"]) == [1, 3]
    assert list(rows["player_count"]) == [100, 300]
    assert (rows["fetched_at"] == NOW).all()
    assert summary["failed"] == [2]


def test_exits_1_when_most_games_failed(run):
    code, rows, summary, _ = run({1: 100, 2: DOWN, 3: ValueError("bad"), 4: DOWN, 5: 500})
    assert code == 1
    assert list(rows["appid"]) == [1, 5]


def test_removed_game_is_skipped_not_failed(run):
    code, rows, summary, _ = run({1: steam_http.PermanentError(404, "url"), 2: 200})
    assert code == 0
    assert summary["failed"] == []
    assert summary["skipped"] == [1]


def test_unknown_app_without_http_error_is_skipped(run):
    code, rows, summary, _ = run({1: None, 2: 200})
    assert summary["skipped"] == [1]
    assert list(rows["appid"]) == [2]


def test_breaker_stops_fetching_and_keeps_earlier_rows(run):
    code, rows, summary, called = run({1: 100, **{a: DOWN for a in range(2, 12)}})
    assert code == 1
    assert called == [1, 2, 3, 4, 5, 6]
    assert summary["tripped"] is True
    assert list(rows["appid"]) == [1]


def test_hour_already_collected_does_nothing(run, tmp_path):
    write_parquet([{"appid": 9, "player_count": 1, "fetched_at": NOW - 60}], players.SCHEMA,
                  tmp_path / "players" / "hour=2026-10-08T17.parquet")
    code, rows, _, called = run({1: 100})
    assert code == 0
    assert called == []
    assert list(rows["appid"]) == [9]
