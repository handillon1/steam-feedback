import json
from datetime import datetime, timezone

from checks import players as player_checks
from checks import reviews as review_checks
from collector import players, reviews
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
