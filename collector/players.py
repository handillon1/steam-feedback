"""Player counts for every game in games.csv, one Parquet file per UTC hour of the run (D14; runs every 4 hours).

    python -m collector.players

Steam only reports the current count, so a missed snapshot can't be refetched. Unlike reviews (D5), this writes
whatever succeeded and records what failed; it exits 1 only if most games failed or Steam looks down.

Output: data/players/hour=YYYY-MM-DDTHH.parquet and a run summary in data/players/_runs/.
"""
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from collector import steam_http
from collector.parse import parse_player_count
from collector.storage import write_parquet

log = logging.getLogger(__name__)

PLAYERS = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"

DATA = Path(__file__).resolve().parent.parent / "data"
GAMES_CSV = DATA / "games.csv"
PLAYERS_DIR = DATA / "players"

BREAKER = 5  # transient failures in a row that mean Steam is down, not one bad game
SCHEMA = pa.schema([("appid", pa.int64()), ("player_count", pa.int64()), ("fetched_at", pa.int64())])


def hour_name(ts: int) -> str:
    """The UTC hour of a timestamp, as "YYYY-MM-DDTHH"."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H")


def collect(appids: list[int]) -> tuple[list[dict], list[int], list[int], bool]:
    """One call per game. Returns (rows, failed, skipped, tripped).

    skipped: removed or unknown apps (a permanent error, or result != 1). tripped: BREAKER transient failures
    in a row stopped the run early; the games not reached count as failed.
    """
    rows, failed, skipped, in_a_row = [], [], [], 0
    for i, appid in enumerate(appids):
        try:
            count = parse_player_count(steam_http.get_json(PLAYERS, {"appid": appid}))
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
        if count is None:
            log.warning("app %s: skipped: Steam doesn't know this app", appid)
            skipped.append(appid)
            continue
        rows.append({"appid": appid, "player_count": count, "fetched_at": int(time.time())})
    return rows, failed, skipped, False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = int(time.time())
    hour = hour_name(now)
    path = PLAYERS_DIR / f"hour={hour}.parquet"
    if path.exists():
        log.info("%s already collected", hour)
        return
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]

    rows, failed, skipped, tripped = collect(appids)
    if rows:
        write_parquet(rows, SCHEMA, path)

    runs = PLAYERS_DIR / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"{stamp}.json").write_text(json.dumps(
        {"now": now, "hour": hour, "rows": len(rows), "tripped": tripped, "failed": failed, "skipped": skipped},
        indent=1), encoding="utf-8")

    log.info("%s: %d games counted, %d failed, %d skipped", hour, len(rows), len(failed), len(skipped))
    if tripped or 2 * len(failed) > len(appids):
        log.error("most games failed; Steam may be down")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
