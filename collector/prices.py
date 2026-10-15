"""Daily US prices for every game in games.csv (D8, D19).

    python -m collector.prices

Batched appdetails with filters=price_overview: 100 appids per call, 3 calls for 300 games (every appid came back at
100, checked 2026-10-15). A game whose data comes back [] gets one full appdetails call to tell free (is_free) from
paid-but-no-single-price (Call of Duty sells only editions). Today's price can't be fetched later, so like players
(D14) this writes whatever succeeded; it exits 1 if any game failed. A non-USD price stops the run with nothing
written.

Output: data/prices/date=YYYY-MM-DD.parquet (the run's UTC day) and a run summary in data/prices/_runs/.
"""
import csv
import json
import logging
import time
from datetime import datetime, timezone

import pyarrow as pa

from collector import steam_http
from collector.parse import parse_app_type, parse_price_batch
from collector.reviews import DATA, GAMES_CSV, day_name
from collector.storage import write_parquet

log = logging.getLogger(__name__)

APPDETAILS = "https://store.steampowered.com/api/appdetails"
PRICES_DIR = DATA / "prices"
BATCH = 100

# status: priced | free (0 cents, D8) | no_price (not free, no single price) | not_found (success: false; no price, not 0)
SCHEMA = pa.schema([("appid", pa.int64()), ("snapshot_date", pa.string()), ("status", pa.string()),
                    ("is_free", pa.bool_()), ("initial_cents", pa.int64()), ("final_cents", pa.int64()),
                    ("discount_percent", pa.int64()), ("fetched_at", pa.int64())])


def row(appid: int, day: str, status: str, is_free: bool | None = None, initial: int | None = None,
        final: int | None = None, discount: int | None = None) -> dict:
    return {"appid": appid, "snapshot_date": day, "status": status, "is_free": is_free, "initial_cents": initial,
            "final_cents": final, "discount_percent": discount, "fetched_at": int(time.time())}


def is_free(appid: int) -> bool | None:
    """is_free from a full appdetails call, or None when Steam says success: false."""
    app = parse_app_type(steam_http.get_json(APPDETAILS, {"appids": appid, "cc": "us"}), appid)
    return None if app is None else app["is_free"]


def collect(appids: list[int], day: str) -> tuple[list[dict], list[int]]:
    """Returns (rows, failed). A failed batch fails all its games; an appid missing from its batch fails too."""
    rows, failed = [], []
    for i in range(0, len(appids), BATCH):
        batch = appids[i:i + BATCH]
        try:
            body = steam_http.get_json(APPDETAILS, {"appids": ",".join(map(str, batch)), "cc": "us",
                                                    "filters": "price_overview"})
        except (steam_http.TransientHTTPError, steam_http.PermanentError) as e:
            log.error("batch %d (%d games): %s: %s", i // BATCH, len(batch), type(e).__name__, e)
            failed += batch
            continue
        parsed = parse_price_batch(body, batch)  # WrongCurrency is not caught: the run stops
        missing = [a for a in batch if a not in parsed]
        if missing:
            log.error("batch %d: %d appids missing from the response; the batch is too big", i // BATCH, len(missing))
            failed += missing
        for appid, p in parsed.items():
            if p["status"] == "priced":
                rows.append(row(appid, day, "priced", False, p["initial_cents"], p["final_cents"], p["discount_percent"]))
            elif p["status"] == "not_found":
                rows.append(row(appid, day, "not_found"))
            else:
                try:
                    free = is_free(appid)
                except (steam_http.TransientHTTPError, steam_http.PermanentError, KeyError) as e:
                    log.error("app %s: is_free check: %s: %s", appid, type(e).__name__, e)
                    failed.append(appid)
                    continue
                if free is None:
                    rows.append(row(appid, day, "not_found"))
                elif free:
                    rows.append(row(appid, day, "free", True, 0, 0, 0))
                else:
                    rows.append(row(appid, day, "no_price", False))
    return rows, failed


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = int(time.time())
    day = day_name(now)
    path = PRICES_DIR / f"date={day}.parquet"
    if path.exists():
        log.info("%s already collected", day)
        return
    with open(GAMES_CSV, encoding="utf-8") as f:
        appids = [int(r["appid"]) for r in csv.DictReader(f)]

    rows, failed = collect(appids, day)
    if rows:
        write_parquet(rows, SCHEMA, path)

    by_status = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r["appid"])
    runs = PRICES_DIR / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"{stamp}.json").write_text(json.dumps(
        {"now": now, "day": day, "rows": len(rows), "counts": {s: len(a) for s, a in by_status.items()},
         "not_found": by_status.get("not_found", []), "no_price": by_status.get("no_price", []), "failed": failed},
        indent=1), encoding="utf-8")

    log.info("%s: %s, %d failed", day, ", ".join(f"{len(a)} {s}" for s, a in sorted(by_status.items())), len(failed))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
