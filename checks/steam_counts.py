"""C1: our review counts vs Steam's own total_reviews, for games whose whole history should be in the database.

    python -m checks.steam_counts 2026-10-15

"Full-history" games: not capped in the backfill (D16) and released on or after 2025-10-05 (appdetails
release_date from data/selection_raw/). One-off, run locally after `python -m collector.load`.

Steam's total includes today's reviews, which aren't in the database yet, so each game is paged from cursor=* (D4
parameters) until a review older than today's UTC midnight, and those English reviews are added to ours.
Writes data/checks/steam_counts_<date>.csv.
"""
import csv
import json
import os
import sys
from datetime import date, datetime, timezone

import psycopg
from dotenv import load_dotenv

from collector import steam_http
from collector.reviews import REVIEW_PARAMS, REVIEWS

WINDOW_START = datetime(2025, 10, 5)


def full_history_games():
    summary = json.load(open("data/backfill/reviews/_summary.json"))
    for g in csv.DictReader(open("data/games.csv", encoding="utf-8")):
        a = g["appid"]
        raw = json.load(open(f"data/selection_raw/appdetails_{a}.json", encoding="utf-8"))[a]["data"]["release_date"]["date"]
        released = next(datetime.strptime(raw, f) for f in ("%d %b, %Y", "%b %d, %Y") if _parses(raw, f))
        if released >= WINDOW_START and not summary[a]["capped"]:
            yield int(a), g["name"], released.date().isoformat()


def _parses(s, fmt):
    try:
        datetime.strptime(s, fmt)
        return True
    except ValueError:
        return False


def main(today):
    load_dotenv(".env")
    midnight = datetime.combine(today, datetime.min.time(), timezone.utc)
    rows = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for appid, name, released in full_history_games():
            cursor, total, fresh_english = "*", None, 0
            while True:
                page = steam_http.get_json(REVIEWS, {"appid": appid, "cursor": cursor, **REVIEW_PARAMS})["response"]
                total = page["query_summary"]["total_reviews"] if total is None else total
                reviews = page.get("reviews") or []
                fresh = [r for r in reviews if r["timestamp_created"] >= midnight.timestamp()]
                fresh_english += sum(r["language"] == "english" for r in fresh)
                if len(fresh) < len(reviews) or not reviews or page.get("cursor") == cursor:
                    break
                cursor = page["cursor"]
            db, first = conn.execute("SELECT count(*), min(created_at)::date FROM reviews WHERE appid = %s AND created_at < %s",
                                     (appid, midnight)).fetchone()
            rows.append({"appid": appid, "name": name, "release": released, "first_review": first, "steam": total,
                         "db": db, "today": fresh_english, "ours": db + fresh_english})
            print(rows[-1], flush=True)
    os.makedirs("data/checks", exist_ok=True)
    with open(f"data/checks/steam_counts_{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]))
