"""Check the newest price run: exits 1 if anything is wrong.

    python -m checks.prices

Reads the day file named by the newest summary in data/prices/_runs/, so a run that wrote nothing fails here
instead of an older day passing in its place. A run stopped by a non-USD price writes no summary; that step has
already failed the job.
"""
import csv
import json
import sys
from pathlib import Path

import pandas as pd

from collector.prices import GAMES_CSV, PRICES_DIR

MAX_MISSING_ROWS = 5   # of the games in games.csv: ~295 of 300
MAX_NOT_FOUND = 5      # a handful of delisted or region-locked games; more means the call is broken
DISCOUNT_SLACK = 1     # points between discount_percent and the one implied by the prices


def problems(prices_dir: Path, n_games: int) -> list[str]:
    runs = sorted((prices_dir / "_runs").glob("*.json"))
    if not runs:
        return [f"no run summary in {prices_dir / '_runs'}"]
    day = json.loads(runs[-1].read_text(encoding="utf-8"))["day"]
    path = prices_dir / f"date={day}.parquet"
    if not path.exists():
        return [f"no file for {day}, the newest run's day"]
    df = pd.read_parquet(path)
    found = []

    def bad(mask, what):
        if mask.any():
            found.append(f"{day}: {mask.sum()} {what} (appids {sorted(df.appid[mask])[:5]})")

    if len(df) < n_games - MAX_MISSING_ROWS:
        found.append(f"{day}: only {len(df)} of {n_games} games have a row (want {n_games - MAX_MISSING_ROWS}+)")
    bad(df.appid.duplicated(), "duplicate appids")
    if (df.status == "not_found").sum() > MAX_NOT_FOUND:
        bad(df.status == "not_found", f"not_found rows (want at most {MAX_NOT_FOUND})")

    priced = df.status == "priced"
    price_cols = ["initial_cents", "final_cents", "discount_percent"]
    bad(priced & df[price_cols].isna().any(axis=1), "priced rows with a missing price")
    bad(priced & (df.initial_cents <= 0), "priced rows with initial price 0")
    bad(priced & (df.final_cents > df.initial_cents), "rows with final price above initial")
    bad(priced & ~df.discount_percent.between(0, 100), "discounts outside 0-100")
    implied = (100 * (1 - df.final_cents / df.initial_cents.where(df.initial_cents > 0))).round()
    bad(priced & ((implied - df.discount_percent).abs() > DISCOUNT_SLACK),
        f"discounts more than {DISCOUNT_SLACK} point off the prices")

    free = df.status == "free"
    bad(free & ((df[price_cols].fillna(-1) != 0).any(axis=1) | (df.is_free != True)), "free rows with a nonzero price")
    unpriced = df.status.isin(["no_price", "not_found"])
    bad(unpriced & df[price_cols].notna().any(axis=1), "no_price/not_found rows with a price (should be NULL)")
    return found


def main():
    with open(GAMES_CSV, encoding="utf-8") as f:
        n_games = sum(1 for _ in csv.DictReader(f))
    found = problems(PRICES_DIR, n_games)
    for p in found:
        print(f"FAIL {p}")
    if found:
        sys.exit(1)
    print("prices: ok")


if __name__ == "__main__":
    main()
