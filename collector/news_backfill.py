"""One-off news backfill: every official announcement since 2025-10-05, with full post bodies (D9).

Run locally with `python -m collector.news_backfill`; it's resumable, so after a crash just run it again.

There's no is_patch column on purpose: the patch rule is still being measured, so it's applied later in SQL or
a notebook, where changing it doesn't mean refetching anything. Posts newer than the reviews backfill's END are
kept too, since the patch table also has to cover the daily collection.
"""
import csv
import logging
from pathlib import Path

import pyarrow as pa

from collector import steam_http
from collector.backfill import START
from collector.reviews import BREAKER, DATA, GAMES_CSV, day_name
from collector.select_games import NEWS
from collector.storage import write_parquet

log = logging.getLogger(__name__)

NEWS_DIR = DATA / "backfill" / "news"
NEWS_PARAMS = {"count": 100, "feeds": "steam_community_announcements"}  # no maxlength: full bodies
MAX_PAGES = 50  # 5,000 posts; a game that needs more is a broken pager, not a busy developer

SCHEMA = pa.schema([("gid", pa.string()), ("appid", pa.int64()), ("date", pa.int64()), ("title", pa.string()),
                    ("contents", pa.string()), ("url", pa.string()), ("author", pa.string()),
                    ("feedname", pa.string()), ("tags", pa.list_(pa.string()))])


def to_row(post: dict, appid: int) -> dict:
    """One newsitem as a SCHEMA row. tags is missing on untagged posts, so it becomes []."""
    return {"gid": str(post["gid"]), "appid": appid, "date": post["date"], "title": post["title"],
            "contents": post.get("contents"), "url": post.get("url"), "author": post.get("author"),
            "feedname": post.get("feedname"), "tags": post.get("tags") or []}


def fetch_news(appid: int, since: int, max_pages: int = MAX_PAGES) -> tuple[list[dict], int]:
    """Official announcements dated >= since, newest first, and the number of pages it took.

    Same loop as select_games.news_since: page backwards with enddate, which is inclusive, so dedupe on gid.
    """
    rows, seen, enddate = [], set(), None
    for pages in range(1, max_pages + 1):
        params = {"appid": appid, **NEWS_PARAMS}
        if enddate is not None:
            params["enddate"] = enddate
        page = [p for p in steam_http.get_json(NEWS, params)["appnews"]["newsitems"] if str(p["gid"]) not in seen]
        if not page:
            return rows, pages
        seen |= {str(p["gid"]) for p in page}
        rows += [to_row(p, appid) for p in page if p["date"] >= since]
        enddate = min(p["date"] for p in page)
        if enddate < since:
            return rows, pages
    raise ValueError(f"more than {max_pages} pages of news since {day_name(since)}")


def backfill(appids: list[int], since: int, out_dir: Path) -> tuple[list[int], bool]:
    """fetch_news for every game without a file, writing appid=<id>.parquet one game at a time.

    Returns (failed, tripped). A game with no posts still gets an (empty) file, so reruns skip it.
    A permanent error (removed game) is logged and skipped, not failed, and gets no file.
    """
    failed, in_a_row = [], 0
    todo = [a for a in appids if not (out_dir / f"appid={a}.parquet").exists()]
    log.info("%d of %d games already have news, %d to go", len(appids) - len(todo), len(appids), len(todo))
    for i, appid in enumerate(todo, 1):
        try:
            rows, pages = fetch_news(appid, since)
        except steam_http.PermanentError as e:
            log.warning("app %s: skipped: %s", appid, e)
            in_a_row = 0
            continue
        except (steam_http.TransientHTTPError, ValueError, KeyError) as e:
            log.error("app %s: %s: %s", appid, type(e).__name__, e)
            failed.append(appid)
            in_a_row = in_a_row + 1 if isinstance(e, steam_http.TransientHTTPError) else 0
            if in_a_row == BREAKER:
                log.error("%d transient failures in a row: Steam looks down, stopping", BREAKER)
                return failed, True
            continue
        in_a_row = 0
        write_parquet(rows, SCHEMA, out_dir / f"appid={appid}.parquet")
        log.info("[%d/%d] app %s: %d posts in %d pages", i, len(todo), appid, len(rows), pages)
    return failed, False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]
    log.info("%d games, news since %s (UTC)", len(appids), day_name(START))

    failed, tripped = backfill(appids, START, NEWS_DIR)

    if tripped or failed:
        log.error("%d games failed (%s); rerun to retry them", len(failed), failed[:10])
        raise SystemExit(1)
    log.info("done; files in %s", NEWS_DIR)


if __name__ == "__main__":
    main()
