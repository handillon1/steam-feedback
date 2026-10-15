import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from collector import prices, steam_http
from collector.parse import WrongCurrency, parse_price_batch
from collector.storage import write_parquet

FIXTURES = Path(__file__).parent / "fixtures"
NOW = int(datetime(2026, 10, 15, 10, 17, tzinfo=timezone.utc).timestamp())
# 205100 discounted paid game, 730 free ([] + is_free), 1938090 Call of Duty ([] but not free), 99999999 success: false
GAMES = [205100, 730, 1938090, 99999999]


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_price_batch_shapes():
    parsed = parse_price_batch(load("appdetails_prices_batch.json"), GAMES)
    assert parsed[205100] == {"status": "priced", "initial_cents": 999, "final_cents": 249, "discount_percent": 75}
    assert parsed[730] == {"status": "unlisted"}
    assert parsed[1938090] == {"status": "unlisted"}
    assert parsed[99999999] == {"status": "not_found"}


def test_parse_price_batch_leaves_out_missing_appids():
    assert 4242 not in parse_price_batch(load("appdetails_prices_batch.json"), GAMES + [4242])


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(batch_body, games): main() at 10:17 UTC. batch_body is what every batched call returns (or raises).

    Returns (exit code, rows written or None, run summary, the params of every call).
    """
    def go(batch_body=None, games=GAMES):
        csv_path = tmp_path / "games.csv"
        csv_path.write_text("appid\n" + "".join(f"{a}\n" for a in games), encoding="utf-8")
        monkeypatch.setattr(prices, "GAMES_CSV", csv_path)
        monkeypatch.setattr(prices, "PRICES_DIR", tmp_path / "prices")
        monkeypatch.setattr(prices.time, "time", lambda: NOW)
        body = load("appdetails_prices_batch.json") if batch_body is None else batch_body
        full = {730: load("appdetails_730.json"), 1938090: load("appdetails_1938090.json")}
        calls = []

        def fake_get_json(url, params):
            calls.append(params)
            if "filters" not in params:  # the is_free check for a [] game
                return full[params["appids"]]
            if isinstance(body, Exception):
                raise body
            return body
        monkeypatch.setattr(prices.steam_http, "get_json", fake_get_json)
        code = 0
        try:
            prices.main()
        except SystemExit as e:
            code = e.code
        path = tmp_path / "prices" / "date=2026-10-15.parquet"
        rows = pd.read_parquet(path).set_index("appid") if path.exists() else None
        runs = sorted((tmp_path / "prices" / "_runs").glob("*.json"))
        summary = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
        return code, rows, summary, calls
    return go


def test_each_response_shape_becomes_the_right_row(run):
    code, rows, summary, _ = run()
    assert code == 0
    paid, free, no_price, missing = (rows.loc[a] for a in GAMES)
    assert (paid.status, paid.is_free, paid.initial_cents, paid.final_cents, paid.discount_percent) == \
        ("priced", False, 999, 249, 75)
    assert (free.status, free.is_free, free.initial_cents, free.final_cents) == ("free", True, 0, 0)  # D8
    # Not free, no single price: its own value, never 0 or "free"
    assert no_price.status == "no_price" and no_price.is_free == False
    assert pd.isna(no_price.final_cents)
    # success: false: a row with no price (not 0), counted in the run summary
    assert missing.status == "not_found" and pd.isna(missing.is_free) and pd.isna(missing.final_cents)
    assert summary["not_found"] == [99999999]
    assert summary["no_price"] == [1938090]
    assert (rows.snapshot_date == "2026-10-15").all() and (rows.fetched_at == NOW).all()


def test_non_usd_currency_stops_the_run_and_writes_nothing(run, tmp_path):
    body = load("appdetails_prices_batch.json")
    body["205100"]["data"]["price_overview"]["currency"] = "EUR"
    with pytest.raises(WrongCurrency):
        run(body)
    assert not (tmp_path / "prices" / "date=2026-10-15.parquet").exists()


def test_batches_of_100(run):
    body = {str(a): {"success": True, "data": {"price_overview": {"currency": "USD", "initial": 100, "final": 100,
                                                                   "discount_percent": 0}}} for a in range(1, 251)}
    code, rows, _, calls = run(body, games=list(range(1, 251)))
    assert code == 0
    assert [len(c["appids"].split(",")) for c in calls] == [100, 100, 50]
    assert all(c["cc"] == "us" and c["filters"] == "price_overview" for c in calls)
    assert len(rows) == 250


def test_appid_missing_from_the_batch_fails_and_the_rest_is_written(run):
    body = load("appdetails_prices_batch.json")
    del body["205100"]
    code, rows, summary, _ = run(body)
    assert code == 1
    assert summary["failed"] == [205100]
    assert sorted(rows.index) == [730, 1938090, 99999999]


def test_failed_batch_fails_its_games(run):
    code, rows, summary, _ = run(steam_http.TransientHTTPError("503"))
    assert code == 1
    assert rows is None
    assert summary["failed"] == GAMES


def test_day_already_collected_does_nothing(run, tmp_path):
    write_parquet([prices.row(1, "2026-10-15", "free", True, 0, 0, 0)], prices.SCHEMA,
                  tmp_path / "prices" / "date=2026-10-15.parquet")
    code, rows, _, calls = run()
    assert code == 0
    assert calls == []
    assert list(rows.index) == [1]
