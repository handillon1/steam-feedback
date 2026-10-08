"""Check the newest player-count run: exits 1 if anything is wrong.

    python -m checks.players

Reads the hour file named by the newest summary in data/players/_runs/, so a run that wrote nothing
fails here instead of an older hour passing in its place.
"""
import csv
import json
import sys
from pathlib import Path

import pandas as pd

from collector.players import GAMES_CSV, PLAYERS_DIR

MIN_SHARE = 0.8  # of the games in games.csv


def problems(players_dir: Path, n_games: int) -> list[str]:
    runs = sorted((players_dir / "_runs").glob("*.json"))
    if not runs:
        return [f"no run summary in {players_dir / '_runs'}"]
    hour = json.loads(runs[-1].read_text(encoding="utf-8"))["hour"]
    path = players_dir / f"hour={hour}.parquet"
    if not path.exists():
        return [f"no file for {hour}, the newest run's hour"]
    df = pd.read_parquet(path)
    found = []
    if (df["player_count"] < 0).any():
        found.append(f"{hour}: {(df['player_count'] < 0).sum()} negative player counts")
    if len(df) < MIN_SHARE * n_games:
        found.append(f"{hour}: only {len(df)} of {n_games} games counted (want {MIN_SHARE:.0%})")
    return found


def main():
    with open(GAMES_CSV, encoding="utf-8") as f:
        n_games = sum(1 for _ in csv.DictReader(f))
    found = problems(PLAYERS_DIR, n_games)
    for p in found:
        print(f"FAIL {p}")
    if found:
        sys.exit(1)
    print("players: ok")


if __name__ == "__main__":
    main()
