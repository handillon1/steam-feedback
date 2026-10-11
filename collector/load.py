"""Load games.csv, the Parquet files and the backfill summary into Postgres (sql/schema.sql).

    python -m collector.load

Connects with DATABASE_URL from .env. Each Parquet file is loaded in its own transaction and recorded in
loaded_files in that same transaction, so a crash leaves a file either fully loaded and recorded or not at all,
and rerunning loads only what's missing.
"""
import csv
import json
import logging
import os
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from collector.reviews import DATA, GAMES_CSV

log = logging.getLogger(__name__)

SUMMARY_JSON = DATA / "backfill" / "reviews" / "_summary.json"
# Load order: backfill before daily, so a review in both keeps source = 'backfill'.
GLOBS = ["backfill/reviews/appid=*.parquet", "reviews/date=*.parquet", "players/hour=*.parquet"]

_SQL_TYPES = {pa.int64(): "bigint", pa.string(): "text", pa.bool_(): "boolean", pa.float64(): "double precision"}

# Conversions happen here, from the stage table's raw Parquet columns.
_REVIEWS_INSERT = """
INSERT INTO reviews (recommendationid, appid, review, language, created_at, updated_at, voted_up, votes_up,
                     votes_funny, weighted_vote_score, comment_count, steam_purchase, received_for_free,
                     written_during_early_access, primarily_steam_deck, refunded, playtime_at_review_min,
                     developer_response, dev_responded_at, source)
SELECT recommendationid::bigint, appid, review, language, to_timestamp(timestamp_created),
       to_timestamp(timestamp_updated), voted_up, votes_up, votes_funny, weighted_vote_score, comment_count,
       steam_purchase, received_for_free, written_during_early_access, primarily_steam_deck, refunded,
       playtime_at_review, developer_response, to_timestamp(timestamp_dev_responded), %s
FROM stage
ON CONFLICT (recommendationid) DO NOTHING
"""
_PLAYERS_INSERT = """
INSERT INTO player_snapshots (appid, fetched_at, player_count)
SELECT appid, to_timestamp(fetched_at), player_count FROM stage
ON CONFLICT (appid, fetched_at) DO NOTHING
"""


def files_to_load(files: list[str], loaded: set[str]) -> list[str]:
    """The files not yet in loaded_files, in the order given."""
    return [f for f in files if f not in loaded]


def find_files(data: Path) -> list[str]:
    """Every Parquet file to load, relative to data/ with forward slashes (as loaded_files stores them), in GLOBS order."""
    return [p.relative_to(data).as_posix() for g in GLOBS for p in sorted(data.glob(g))]


def load_games(conn: psycopg.Connection, path: Path) -> None:
    with open(path, encoding="utf-8") as f:
        rows = [(int(r["appid"]), r["name"], r["main_genre"] or None, r["size"],
                 float(r["rate_per_day"]) if r["rate_per_day"] else None,
                 int(r["patches_6mo"]) if r["patches_6mo"] else None, int(r["seed"])) for r in csv.DictReader(f)]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("""INSERT INTO games (appid, name, main_genre, size_group, rate_at_selection, patches_6mo,
                                              selection_seed)
                           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (appid) DO NOTHING""", rows)
    log.info("%d games in games.csv", len(rows))


def load_file(conn: psycopg.Connection, data: Path, rel: str) -> tuple[int, int]:
    """Stage one Parquet file with COPY, insert it, and record it in loaded_files, all in one transaction."""
    table = pq.read_table(data / rel)
    cols = table.schema.names
    insert, params = (_PLAYERS_INSERT, ()) if rel.startswith("players/") else \
        (_REVIEWS_INSERT, ("backfill" if rel.startswith("backfill/") else "daily",))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE stage ({', '.join(f'{f.name} {_SQL_TYPES[f.type]}' for f in table.schema)})"
                    " ON COMMIT DROP")
        with cur.copy(f"COPY stage ({', '.join(cols)}) FROM STDIN") as cp:
            for row in table.to_pylist():
                cp.write_row([row[c] for c in cols])
        cur.execute(insert, params)
        inserted = cur.rowcount
        cur.execute("INSERT INTO loaded_files (path, rows_in_file, rows_inserted) VALUES (%s, %s, %s)",
                    (rel, table.num_rows, inserted))
    return table.num_rows, inserted


def load_coverage(conn: psycopg.Connection, path: Path) -> None:
    """One row per backfilled game; error entries (removed games, failures) have no coverage and are left out."""
    summary = json.loads(path.read_text(encoding="utf-8"))
    rows = [(int(a), s["complete_from"], s["capped"]) for a, s in summary.items() if "error" not in s]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("""INSERT INTO review_coverage (appid, complete_from, capped) VALUES (%s, %s, %s)
                           ON CONFLICT (appid) DO UPDATE
                           SET complete_from = EXCLUDED.complete_from, capped = EXCLUDED.capped""", rows)
    log.info("review_coverage: %d games (%d summary entries with errors left out)", len(rows), len(summary) - len(rows))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        load_games(conn, GAMES_CSV)  # first: reviews, player_snapshots and review_coverage reference games
        loaded = {r[0] for r in conn.execute("SELECT path FROM loaded_files")}
        todo = files_to_load(find_files(DATA), loaded)
        log.info("%d files already loaded, %d to go", len(loaded), len(todo))
        for i, rel in enumerate(todo, 1):
            rows, inserted = load_file(conn, DATA, rel)
            log.info("[%d/%d] %s: %d rows, %d inserted", i, len(todo), rel, rows, inserted)
        if SUMMARY_JSON.exists():
            load_coverage(conn, SUMMARY_JSON)


if __name__ == "__main__":
    main()
