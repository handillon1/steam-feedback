import json
from datetime import datetime, timezone

from checks import players as player_checks
from checks import prices as price_checks
from checks import reviews as review_checks
from collector import players, prices, reviews
from collector.storage import write_parquet

DAY = 86400
OCT5 = int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())


def write_run(run_dir, name, **summary):
    (run_dir / "_runs").mkdir(parents=True, exist_ok=True)
    (run_dir / "_runs" / f"{name}.json").write_text(json.dumps(summary), encoding="utf-8")


def write_day(out, day, rows):
    write_parquet([{"appid": 1, **r} for r in rows], reviews.SCHEMA, out / f"date={day}.parquet")


# reviews

def test_clean_review_run_passes(tmp_path):
    write_day(tmp_path, "2026-10-05", [{"recommendationid": "a", "timestamp_created": OCT5}])
    write_day(tmp_path, "2026-10-06", [{"recommendationid": "b", "timestamp_created": OCT5 + 2 * DAY - 1}])
    write_run(tmp_path, "20261008T060000Z", written=["date=2026-10-05.parquet", "date=2026-10-06.parquet"])
    assert review_checks.problems(tmp_path) == []


def test_duplicate_recommendationid_across_files_fails(tmp_path):
    write_day(tmp_path, "2026-10-05", [{"recommendationid": "a", "timestamp_created": OCT5}])
    write_day(tmp_path, "2026-10-06", [{"recommendationid": "a", "timestamp_created": OCT5 + DAY}])
    write_run(tmp_path, "20261008T060000Z", written=["date=2026-10-06.parquet"])
    assert any("duplicate" in p for p in review_checks.problems(tmp_path))


def test_null_timestamp_fails(tmp_path):
    write_day(tmp_path, "2026-10-05", [{"recommendationid": "a", "timestamp_created": None}])
    write_run(tmp_path, "20261008T060000Z", written=["date=2026-10-05.parquet"])
    assert any("null" in p for p in review_checks.problems(tmp_path))


def test_timestamp_outside_its_files_date_fails(tmp_path):
    write_day(tmp_path, "2026-10-05", [{"recommendationid": "a", "timestamp_created": OCT5 + DAY}])  # Oct 6 00:00
    write_run(tmp_path, "20261008T060000Z", written=["date=2026-10-05.parquet"])
    assert any("outside" in p for p in review_checks.problems(tmp_path))


def test_only_the_newest_run_is_checked(tmp_path):
    write_day(tmp_path, "2026-10-05", [{"recommendationid": "a", "timestamp_created": OCT5 + DAY}])  # bad
    write_day(tmp_path, "2026-10-06", [{"recommendationid": "b", "timestamp_created": OCT5 + DAY}])
    write_run(tmp_path, "20261007T060000Z", written=["date=2026-10-05.parquet"])
    write_run(tmp_path, "20261008T060000Z", written=["date=2026-10-06.parquet"])
    assert review_checks.problems(tmp_path) == []


def test_no_review_run_summary_fails(tmp_path):
    assert review_checks.problems(tmp_path) != []


# players

def write_hour(out, hour, counts):
    write_parquet([{"appid": a, "player_count": c, "fetched_at": OCT5} for a, c in enumerate(counts)],
                  players.SCHEMA, out / f"hour={hour}.parquet")


def test_clean_player_run_passes(tmp_path):
    write_hour(tmp_path, "2026-10-08T17", [10] * 8)
    write_run(tmp_path, "20261008T170000Z", hour="2026-10-08T17")
    assert player_checks.problems(tmp_path, n_games=10) == []


def test_negative_player_count_fails(tmp_path):
    write_hour(tmp_path, "2026-10-08T17", [10] * 9 + [-1])
    write_run(tmp_path, "20261008T170000Z", hour="2026-10-08T17")
    assert any("negative" in p for p in player_checks.problems(tmp_path, n_games=10))


def test_too_few_games_counted_fails(tmp_path):
    write_hour(tmp_path, "2026-10-08T17", [10] * 7)
    write_run(tmp_path, "20261008T170000Z", hour="2026-10-08T17")
    assert any("7 of 10" in p for p in player_checks.problems(tmp_path, n_games=10))


def test_newest_run_without_an_hour_file_fails(tmp_path):
    write_hour(tmp_path, "2026-10-08T16", [10] * 10)  # an older, fine hour must not hide the missing one
    write_run(tmp_path, "20261008T170000Z", hour="2026-10-08T17")
    assert any("no file" in p for p in player_checks.problems(tmp_path, n_games=10))


# prices

def price_row(appid, status="priced", initial=1000, final=1000, discount=0, is_free=False):
    return prices.row(appid, "2026-10-15", status, is_free, initial, final, discount)


def write_prices(out, rows):
    write_parquet(rows, prices.SCHEMA, out / "date=2026-10-15.parquet")
    write_run(out, "20261015T101700Z", day="2026-10-15")


def clean_prices():
    return ([price_row(a) for a in range(1, 8)] + [price_row(8, initial=999, final=249, discount=75)]
            + [price_row(9, "free", 0, 0, 0, True), price_row(10, "no_price", None, None, None)])


def test_clean_price_run_passes(tmp_path):
    write_prices(tmp_path, clean_prices())
    assert price_checks.problems(tmp_path, n_games=10) == []


def test_final_above_initial_fails(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, initial=1000, final=1200, discount=0)])
    assert any("above initial" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_discount_outside_0_100_fails(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, discount=120)])
    assert any("outside 0-100" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_discount_that_doesnt_match_the_prices_fails(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, initial=1000, final=500, discount=40)])
    assert any("off the prices" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_discount_within_1_point_of_the_prices_passes(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, initial=1999, final=1339, discount=34)])  # 33.0
    assert price_checks.problems(tmp_path, n_games=10) == []


def test_priced_row_with_missing_price_fails(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, final=None)])
    assert any("missing price" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_free_row_with_nonzero_price_fails(tmp_path):
    write_prices(tmp_path, clean_prices()[:-1] + [price_row(10, "free", 0, 499, 0, True)])
    assert any("free rows" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_too_few_price_rows_fails(tmp_path):
    write_prices(tmp_path, clean_prices())
    assert any("only 10 of 300" in p for p in price_checks.problems(tmp_path, n_games=300))
    assert price_checks.problems(tmp_path, n_games=15) == []  # 5 missing is allowed


def test_more_than_a_handful_of_not_found_fails(tmp_path):
    rows = clean_prices()[:4] + [price_row(a, "not_found", None, None, None, None) for a in range(5, 11)]
    write_prices(tmp_path, rows)
    assert any("not_found" in p for p in price_checks.problems(tmp_path, n_games=10))


def test_no_price_file_for_the_newest_run_fails(tmp_path):
    write_run(tmp_path, "20261015T101700Z", day="2026-10-15")
    assert any("no file" in p for p in price_checks.problems(tmp_path, n_games=10))
