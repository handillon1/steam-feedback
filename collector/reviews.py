import csv
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from collector import steam_http
from collector.parse import REVIEW_FIELDS, SECONDS_PER_DAY, parse_reviews
from collector.storage import write_parquet

log = logging.getLogger(__name__)

REVIEWS = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
REVIEW_PARAMS = {"num_per_page": 100, "filter": 1, "languages[0]": "english", "purchase_type": 1}  # D4

DATA = Path(__file__).resolve().parent.parent / "data"
GAMES_CSV = DATA / "games.csv"
REVIEWS_DIR = DATA / "reviews"

WINDOW_DAYS = 3
MAX_PAGES = 100  # 10,000 reviews in 3 days; CS2, the busiest game, needs ~42 pages
BREAKER = 5  # transient failures in a row that mean Steam is down, not one bad game

COLUMNS = ["appid"] + REVIEW_FIELDS + ["playtime_at_review"]
# Explicit, so a day where every developer_response is None isn't written as a "null" column (D5).
# A KeyError here at import means a field was added to REVIEW_FIELDS without a type.
_TYPES = {"appid": pa.int64(), "recommendationid": pa.string(), "language": pa.string(), "review": pa.string(),
          "timestamp_created": pa.int64(), "timestamp_updated": pa.int64(), "voted_up": pa.bool_(),
          "votes_up": pa.int64(), "votes_funny": pa.int64(), "weighted_vote_score": pa.float64(),
          "comment_count": pa.int64(), "steam_purchase": pa.bool_(), "received_for_free": pa.bool_(),
          "written_during_early_access": pa.bool_(), "primarily_steam_deck": pa.bool_(), "refunded": pa.bool_(),
          "developer_response": pa.string(), "timestamp_dev_responded": pa.int64(), "playtime_at_review": pa.int64()}
SCHEMA = pa.schema([(c, _TYPES[c]) for c in COLUMNS])


def window(now: int) -> tuple[int, int]:
    """[today's UTC midnight - 3 days, today's UTC midnight): only complete days, never today.

    Starting at "now - 72h" would cut the first day in half, and since files are never rewritten,
    that half day would stay missing for good.
    """
    today = now - now % SECONDS_PER_DAY  # Unix time has no leap seconds, so this is UTC midnight
    return today - WINDOW_DAYS * SECONDS_PER_DAY, today


def fetch_window(appid: int, start: int, end: int, max_pages: int = MAX_PAGES,
                 cursor: str = "*") -> tuple[list[dict], dict]:
    """English reviews created in [start, end), newest first, each tagged with appid. Pages from cursor (D5);
    the default * is the newest review, and the backfill passes a later one (D16).

    Returns (rows, stats) with stats = {pages, rows, skipped, capped}. capped means it stopped at max_pages
    before reaching start, so the oldest days are incomplete.
    """
    rows, seen = [], set()
    stats = {"pages": 0, "rows": 0, "skipped": 0, "capped": False}
    while True:
        if stats["pages"] == max_pages:
            stats["capped"] = True
            log.warning("app %s: stopped at %d pages before reaching the start of the window", appid, max_pages)
            break
        page = steam_http.get_json(REVIEWS, {"appid": appid, "cursor": cursor, **REVIEW_PARAMS})["response"]
        stats["pages"] += 1
        page_rows, skipped = parse_reviews(page)
        stats["skipped"] += skipped
        for row in page_rows:
            # Dedupe: a review posted mid-run shifts the page boundaries, so a page can repeat one.
            if start <= row["timestamp_created"] < end and row["recommendationid"] not in seen:
                seen.add(row["recommendationid"])
                rows.append({"appid": appid, **row})
        raw = page.get("reviews")
        if not raw or page.get("cursor") == cursor or min(r["timestamp_created"] for r in raw) < start:
            break
        cursor = page["cursor"]
    stats["rows"] = len(rows)
    return rows, stats


def day_name(ts: int) -> str:
    """The UTC date of a timestamp, as "YYYY-MM-DD"."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def split_by_day(rows: list[dict]) -> dict[str, list[dict]]:
    """Rows grouped by the UTC date they were created."""
    days = defaultdict(list)
    for row in rows:
        days[day_name(row["timestamp_created"])].append(row)
    return dict(days)


def days_to_write(rows: list[dict], failed: list[int], start: int, end: int) -> dict[str, list[dict]]:
    """Every day in the window after a clean run. Otherwise only the oldest day, without the failed games (D5).

    The oldest day leaves the window after this run, so it's now or never; the newer days wait for a clean run.
    A day with no reviews still gets a (empty) file, so a file existing always means the day was collected.
    """
    days = {day_name(d): [] for d in range(start, end, SECONDS_PER_DAY)}
    for day, day_rows in split_by_day(rows).items():
        days[day] = day_rows
    if not failed:
        return days
    oldest, bad = day_name(start), set(failed)
    return {oldest: [r for r in days[oldest] if r["appid"] not in bad]}


def write_day_files(days: dict[str, list[dict]], out_dir: Path) -> list[Path]:
    """Write date=<day>.parquet for each day that has no file yet. Returns the paths written."""
    written = []
    for day in sorted(days):
        path = out_dir / f"date={day}.parquet"
        if path.exists():
            continue
        write_parquet(days[day], SCHEMA, path)
        written.append(path)
    return written


def collect(appids: list[int], start: int, end: int) -> tuple[list[dict], dict, list[int], bool]:
    """fetch_window for every game. Returns (rows, summary, failed, tripped).

    failed: games whose window is incomplete (network error after retries, broken page, or capped).
    A permanent error (removed game) is logged and skipped, not failed. tripped: BREAKER transient failures
    in a row stopped the run early, because Steam is down.
    """
    rows, summary, failed, in_a_row = [], {}, [], 0
    for appid in appids:
        try:
            game_rows, stats = fetch_window(appid, start, end)
        except steam_http.PermanentError as e:
            log.warning("app %s: skipped: %s", appid, e)
            summary[appid] = {"error": str(e)}
            in_a_row = 0
            continue
        except (steam_http.TransientHTTPError, ValueError, KeyError) as e:
            log.error("app %s: %s: %s", appid, type(e).__name__, e)
            summary[appid] = {"error": f"{type(e).__name__}: {e}"}
            failed.append(appid)
            in_a_row = in_a_row + 1 if isinstance(e, steam_http.TransientHTTPError) else 0
            if in_a_row == BREAKER:
                log.error("%d transient failures in a row: Steam looks down, stopping", BREAKER)
                return rows, summary, failed, True
            continue
        in_a_row = 0
        summary[appid] = stats
        if stats["capped"]:
            failed.append(appid)
        rows += game_rows
    return rows, summary, failed, False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = int(time.time())
    start, end = window(now)
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]
    oldest = day_name(start)
    log.info("%d games, window %s to %s (UTC, end excluded)", len(appids), oldest, day_name(end))

    rows, summary, failed, tripped = collect(appids, start, end)
    written = [] if tripped else write_day_files(days_to_write(rows, failed, start, end), REVIEWS_DIR)

    # (appid, day) pairs that left the window without being collected, for a later backfill (D5).
    oldest_path = REVIEWS_DIR / f"date={oldest}.parquet"
    if not oldest_path.exists():  # breaker tripped
        missing = [[a, oldest] for a in appids]
    elif oldest_path in written:
        missing = [[a, oldest] for a in failed]
    else:
        missing = []  # written complete by an earlier run

    runs = REVIEWS_DIR / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"{stamp}.json").write_text(json.dumps(
        {"now": now, "start": start, "end": end, "tripped": tripped, "failed": failed, "missing": missing,
         "written": [p.name for p in written], "games": summary}, indent=1), encoding="utf-8")

    log.info("%d reviews from %d games; wrote %s", len(rows), len(summary), [p.name for p in written] or "nothing")
    if missing:
        log.error("%d (game, day) pairs missing, recorded in %s for backfill", len(missing), stamp)
    if tripped or failed:
        log.error("%d games incomplete (%s); newer days wait for a clean run", len(failed), failed[:10])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
