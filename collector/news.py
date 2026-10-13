"""Daily news: official announcements from the last 7 days for every game in games.csv.

    python -m collector.news

The 7-day window overlaps the previous runs on purpose, so a missed or late run (D15) never loses a post; the
loader dedupes on gid. Posts stay fetchable for days, so like players (not reviews, D5) this writes whatever
succeeded and exits 1 only if most games failed or Steam looks down.

Output: data/news/fetched=YYYY-MM-DD.parquet and a run summary in data/news/_runs/.
"""
import csv
import json
import logging
import time
from datetime import datetime, timezone

from collector import steam_http
from collector.news_backfill import SCHEMA, fetch_news
from collector.reviews import BREAKER, DATA, GAMES_CSV, day_name
from collector.storage import write_parquet

log = logging.getLogger(__name__)

NEWS_DIR = DATA / "news"
WINDOW = 7 * 86400


def collect(appids: list[int], since: int) -> tuple[list[dict], list[int], list[int], bool]:
    """fetch_news for every game. Returns (rows, failed, skipped, tripped), as players.collect does."""
    rows, failed, skipped, in_a_row = [], [], [], 0
    for i, appid in enumerate(appids):
        try:
            posts, _ = fetch_news(appid, since)
        except steam_http.PermanentError as e:
            log.warning("app %s: skipped: %s", appid, e)
            skipped.append(appid)
            in_a_row = 0
            continue
        except (steam_http.TransientHTTPError, ValueError, KeyError) as e:
            log.error("app %s: %s: %s", appid, type(e).__name__, e)
            failed.append(appid)
            in_a_row = in_a_row + 1 if isinstance(e, steam_http.TransientHTTPError) else 0
            if in_a_row == BREAKER:
                log.error("%d transient failures in a row: Steam looks down, stopping", BREAKER)
                return rows, failed + appids[i + 1:], skipped, True
            continue
        in_a_row = 0
        rows += posts
    return rows, failed, skipped, False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = int(time.time())
    day = day_name(now)
    path = NEWS_DIR / f"fetched={day}.parquet"
    if path.exists():
        log.info("%s already collected", day)
        return
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]

    rows, failed, skipped, tripped = collect(appids, now - WINDOW)
    # Unlike players, an empty but successful run still writes a file: no posts this week is an answer.
    if rows or len(failed) < len(appids):
        write_parquet(rows, SCHEMA, path)

    runs = NEWS_DIR / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"{stamp}.json").write_text(json.dumps(
        {"now": now, "day": day, "since": now - WINDOW, "rows": len(rows), "tripped": tripped, "failed": failed,
         "skipped": skipped}, indent=1), encoding="utf-8")

    log.info("%s: %d posts, %d games failed, %d skipped", day, len(rows), len(failed), len(skipped))
    if tripped or 2 * len(failed) > len(appids):
        log.error("most games failed; Steam may be down")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
