from collector.load import files_to_load, find_files

FILES = ["backfill/reviews/appid=220.parquet", "backfill/reviews/appid=440.parquet",
         "reviews/date=2026-10-05.parquet", "players/hour=2026-10-08T18.parquet"]


def test_nothing_loaded_means_every_file_in_order():
    assert files_to_load(FILES, set()) == FILES


def test_loaded_files_are_left_out_and_order_is_kept():
    assert files_to_load(FILES, {FILES[0], FILES[2]}) == [FILES[1], FILES[3]]


def test_everything_loaded_means_nothing_to_do():
    assert files_to_load(FILES, set(FILES)) == []


def test_find_files_orders_backfill_then_daily_then_players_and_skips_the_rest(tmp_path):
    for rel in ["players/hour=2026-10-08T18.parquet", "reviews/date=2026-10-06.parquet",
                "reviews/date=2026-10-05.parquet", "backfill/reviews/appid=440.parquet",
                "backfill/reviews/_summary.json", "reviews/.date=2026-10-07.parquet.tmp", "players/_runs/x.json"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).touch()
    assert find_files(tmp_path) == ["backfill/reviews/appid=440.parquet", "reviews/date=2026-10-05.parquet",
                                    "reviews/date=2026-10-06.parquet", "players/hour=2026-10-08T18.parquet"]
