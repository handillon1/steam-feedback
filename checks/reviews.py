"""Check the newest review run: exits 1 if anything is wrong.

    python -m checks.reviews

Reads the newest summary in data/reviews/_runs/ and the day files that run wrote. Duplicate recommendationids
are checked across every day file, since a review landing in two days is exactly what skip-existing could hide.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from collector.parse import SECONDS_PER_DAY
from collector.reviews import REVIEWS_DIR


def problems(reviews_dir: Path) -> list[str]:
    runs = sorted((reviews_dir / "_runs").glob("*.json"))
    if not runs:
        return [f"no run summary in {reviews_dir / '_runs'}"]
    written = json.loads(runs[-1].read_text(encoding="utf-8"))["written"]
    found = []
    for name in written:
        df = pd.read_parquet(reviews_dir / name, columns=["timestamp_created"])
        if df["timestamp_created"].isna().any():
            found.append(f"{name}: {df['timestamp_created'].isna().sum()} null timestamp_created")
        day = name.removeprefix("date=").removesuffix(".parquet")
        start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        ts = df["timestamp_created"].dropna()
        outside = ((ts < start) | (ts >= start + SECONDS_PER_DAY)).sum()
        if outside:
            found.append(f"{name}: {outside} reviews created outside {day} (UTC)")
    if written:
        ids = pd.read_parquet(reviews_dir, columns=["recommendationid"])["recommendationid"]
        if ids.duplicated().any():
            found.append(f"{ids.duplicated().sum()} duplicate recommendationid across day files")
    return found


def main():
    found = problems(REVIEWS_DIR)
    for p in found:
        print(f"FAIL {p}")
    if found:
        sys.exit(1)
    print("reviews: ok")


if __name__ == "__main__":
    main()
