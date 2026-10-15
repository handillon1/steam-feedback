import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from collector.load import _NEWS_INSERT, _PLAYERS_INSERT, _PRICES_INSERT, _REVIEWS_INSERT, files_to_load, find_files, insert_for, sql_type

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


def test_find_files_puts_news_after_players_backfill_first(tmp_path):
    for rel in ["news/fetched=2026-10-13.parquet", "backfill/news/appid=440.parquet", "players/hour=2026-10-08T18.parquet"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).touch()
    assert find_files(tmp_path) == ["players/hour=2026-10-08T18.parquet", "backfill/news/appid=440.parquet",
                                    "news/fetched=2026-10-13.parquet"]


def test_insert_for_picks_the_statement_by_path_prefix():
    assert insert_for("backfill/reviews/appid=440.parquet") == (_REVIEWS_INSERT, ("backfill",))
    assert insert_for("reviews/date=2026-10-05.parquet") == (_REVIEWS_INSERT, ("daily",))
    assert insert_for("players/hour=2026-10-08T18.parquet") == (_PLAYERS_INSERT, ())
    assert insert_for("backfill/news/appid=440.parquet") == (_NEWS_INSERT, ())
    assert insert_for("news/fetched=2026-10-13.parquet") == (_NEWS_INSERT, ())
    assert insert_for("prices/date=2026-10-15.parquet") == (_PRICES_INSERT, ())
    with pytest.raises(ValueError):
        insert_for("selection_raw/x.parquet")


def test_sql_type_reads_tags_back_from_parquet_as_text_array(tmp_path):
    schema = pa.schema([("gid", pa.string()), ("tags", pa.list_(pa.string()))])
    pq.write_table(pa.Table.from_pylist([{"gid": "1", "tags": ["patchnotes"]}], schema=schema), tmp_path / "x.parquet")
    assert [sql_type(f.type) for f in pq.read_schema(tmp_path / "x.parquet")] == ["text", "text[]"]


def test_find_files_puts_prices_last_and_skips_their_run_summaries(tmp_path):
    for rel in ["prices/date=2026-10-15.parquet", "prices/_runs/x.json", "news/fetched=2026-10-13.parquet"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).touch()
    assert find_files(tmp_path) == ["news/fetched=2026-10-13.parquet", "prices/date=2026-10-15.parquet"]
