"""One-off review backfill: up to a year of history per game, ending where the daily files begin (D16).

Run locally with `python -m collector.backfill`; it's resumable, so after a crash just run it again.
"""
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from collector import steam_http
from collector.parse import SECONDS_PER_DAY
from collector.reviews import BREAKER, DATA, GAMES_CSV, REVIEW_PARAMS, REVIEWS, SCHEMA, day_name, fetch_window
from collector.storage import write_parquet

log = logging.getLogger(__name__)

BACKFILL_DIR = DATA / "backfill" / "reviews"
END = int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())  # the first daily file, date=2026-10-05 (R1)
DAYS_BACK = 365
START = END - DAYS_BACK * SECONDS_PER_DAY  # 2025-10-05 00:00 UTC, the same for every game and every rerun
MAX_PAGES = 50  # 5,000 reviews per game, counted from END, not from today
SKIP_PAGES = 100  # pages of reviews newer than END the skip-ahead may read; CS2 needs ~10 per day since END


def cursor_at(appid: int, end: int, max_pages: int = SKIP_PAGES) -> str:
    """The cursor of the first page that reaches back before end, paging from the newest review.

    fetch_window from this cursor spends its whole page cap on reviews older than end. From * it would first
    page through everything since end, which the daily files already have; CS2 would use ~60 pages on that alone.
    """
    cursor = "*"
    for _ in range(max_pages):
        page = steam_http.get_json(REVIEWS, {"appid": appid, "cursor": cursor, **REVIEW_PARAMS})["response"]
        raw = page.get("reviews")
        if not raw or page.get("cursor") == cursor or min(r["timestamp_created"] for r in raw) < end:
            return cursor
        cursor = page["cursor"]
    raise ValueError(f"more than {max_pages} pages of reviews newer than {day_name(end)}")


def game_summary(rows: list[dict], stats: dict, start: int) -> dict:
    """fetch_window's stats plus how far back the game's history goes.

    complete_from is the first day with every review. A capped game stopped partway through its earliest day,
    so its history is complete only from the day after.
    """
    earliest = min((r["timestamp_created"] for r in rows), default=None)
    if not stats["capped"]:
        complete_from = day_name(start)
    elif earliest is None:
        complete_from = None
    else:
        complete_from = day_name(earliest + SECONDS_PER_DAY)
    return {**stats, "earliest": day_name(earliest) if earliest is not None else None, "complete_from": complete_from}


def load_summary(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_summary(summary: dict, path: Path) -> None:
    """Atomically, like write_parquet, so a crash mid-write can't lose the earlier games' entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(dict(sorted(summary.items(), key=lambda kv: int(kv[0]))), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def backfill(appids: list[int], start: int, end: int, out_dir: Path) -> tuple[dict, list[int], bool]:
    """Fetch [start, end) for every game without a file, writing appid=<id>.parquet one game at a time.

    Returns (summary, failed, tripped), with the same meanings as reviews.collect. Unlike the daily run, a capped
    game is written: the cap is expected here, and the summary records how far back it got.
    The summary entry is saved before the parquet file, so a crash between the two leaves an entry for a game
    without a file, which the rerun fetches again and overwrites, never a file without an entry.
    """
    summary_path = out_dir / "_summary.json"
    summary = load_summary(summary_path)
    failed, in_a_row = [], 0
    todo = [a for a in appids if not (out_dir / f"appid={a}.parquet").exists()]
    log.info("%d of %d games already backfilled, %d to go", len(appids) - len(todo), len(appids), len(todo))
    for i, appid in enumerate(todo, 1):
        try:
            rows, stats = fetch_window(appid, start, end, MAX_PAGES, cursor_at(appid, end))
        except steam_http.PermanentError as e:
            log.warning("app %s: skipped: %s", appid, e)
            summary[str(appid)] = {"error": str(e)}
            save_summary(summary, summary_path)
            in_a_row = 0
            continue
        except (steam_http.TransientHTTPError, ValueError, KeyError) as e:
            log.error("app %s: %s: %s", appid, type(e).__name__, e)
            summary[str(appid)] = {"error": f"{type(e).__name__}: {e}"}
            save_summary(summary, summary_path)
            failed.append(appid)
            in_a_row = in_a_row + 1 if isinstance(e, steam_http.TransientHTTPError) else 0
            if in_a_row == BREAKER:
                log.error("%d transient failures in a row: Steam looks down, stopping", BREAKER)
                return summary, failed, True
            continue
        in_a_row = 0
        summary[str(appid)] = entry = game_summary(rows, stats, start)
        save_summary(summary, summary_path)
        write_parquet(rows, SCHEMA, out_dir / f"appid={appid}.parquet")
        log.info("[%d/%d] app %s: %d reviews in %d pages, back to %s%s", i, len(todo), appid, stats["rows"],
                 stats["pages"], entry["earliest"], " (capped)" if stats["capped"] else "")
    return summary, failed, False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]
    log.info("%d games, backfilling %s to %s (UTC, end excluded)", len(appids), day_name(START), day_name(END))

    summary, failed, tripped = backfill(appids, START, END, BACKFILL_DIR)

    done = [s for s in summary.values() if "error" not in s]
    capped = sum(s["capped"] for s in done)
    log.info("%d games backfilled (%d capped), %d reviews; summary in %s",
             len(done), capped, sum(s["rows"] for s in done), BACKFILL_DIR / "_summary.json")
    if tripped or failed:
        log.error("%d games failed (%s); rerun to retry them", len(failed), failed[:10])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
