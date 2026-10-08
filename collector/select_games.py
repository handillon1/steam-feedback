"""Pick the frozen ~300-game list (D3, D11-D13).

    python -m collector.select_games

Candidates that pass the English review-rate bar are grouped by size × genre and drawn at random within groups
with a fixed seed. Patch counts are recorded for information only; they don't affect selection (L4).

Every raw response is cached under data/selection_raw/, and a call whose file exists is skipped,
so a crashed run resumes where it stopped and a finished run redraws with no network at all.
The run's "now" is cached too, so rates and patch windows don't shift between resumes, and so are the top-sellers
pages, so a resume reuses the same one-day snapshot.

Output: data/games.csv and data/candidates.csv (why each candidate was dropped), both committed.
games.csv is not written while any candidate hit a network error; rerun until it is.
"""
import argparse
import csv
import json
import logging
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

from collector import steam_http
from collector.parse import english_rate_per_day, is_patch, parse_app_type, parse_reviews, parse_search_results
from collector.reviews import REVIEW_PARAMS, REVIEWS  # D4 lives in one place

log = logging.getLogger(__name__)

CHARTS = "https://api.steampowered.com/ISteamChartsService/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
NEWS = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
SEARCH = "https://store.steampowered.com/search/results/"

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "selection_raw"
GAMES_CSV = DATA / "games.csv"
CANDIDATES_CSV = DATA / "candidates.csv"

SEED = 20261005
TARGET = 300
CAP_100_PLUS = 40
MIN_RATE = 1.0
PATCH_WINDOW_DAYS = 182
SEARCH_DEPTH = 1500  # top sellers to take; 100 per page is the endpoint's cap

# Store "genres" that describe a business model or status, not what the game is.
NOT_A_GENRE = {"Free To Play", "Early Access", "Indie", "Massively Multiplayer"}

GAMES_FIELDS = ["appid", "name", "main_genre", "size", "rate_per_day", "rate_page_full", "patches_6mo", "seed"]
CANDIDATE_FIELDS = ["appid", "source", "name", "type", "coming_soon", "genres", "rate_per_day", "rate_page_full", "patches_6mo", "dropped"]


# Pure helpers (tested in tests/test_select_games.py)

def main_genre(genres):
    """First store genre that says what the game is; falls back to the first genre, then "Unknown"."""
    for g in genres:
        if g not in NOT_A_GENRE:
            return g
    return genres[0] if genres else "Unknown"


def size_group(rate):
    if rate < 10:
        return "1-10"
    if rate < 100:
        return "10-100"
    return "100+"


def count_patches(posts, since):
    return sum(1 for p in posts if p["date"] >= since and is_patch(p))


def allocate(sizes, n):
    """Split n draws across strata in proportion to their sizes (largest remainder), never more than a stratum has."""
    total = sum(sizes.values())
    if n >= total:
        return dict(sizes)
    exact = {k: n * s / total for k, s in sizes.items()}
    out = {k: int(v) for k, v in exact.items()}
    by_remainder = sorted(sizes, key=lambda k: (exact[k] - out[k], k), reverse=True)
    for k in by_remainder[: n - sum(out.values())]:
        out[k] += 1
    return out


def draw(rows, seed=SEED, target=TARGET, cap_100_plus=CAP_100_PLUS):
    """Stratified random draw by size x main genre, with the 100+/day group capped (D13)."""
    rng = random.Random(seed)
    strata = defaultdict(list)
    for r in sorted(rows, key=lambda r: int(r["appid"])):  # sorted so the seed alone fixes the result
        strata[(r["size"], r["main_genre"])].append(r)

    big = {k: len(v) for k, v in strata.items() if k[0] == "100+"}
    rest = {k: len(v) for k, v in strata.items() if k[0] != "100+"}
    n_big = min(cap_100_plus, sum(big.values()))
    quota = {**allocate(big, n_big), **allocate(rest, target - n_big)}

    picked = []
    for key in sorted(quota):
        picked += rng.sample(strata[key], quota[key])
    return sorted(picked, key=lambda r: int(r["appid"]))


# Cached fetching

def cached_json(name, url, params):
    """get_json, but read from RAW/<name>.json if it exists. Permanent errors are cached too, so resumes skip them."""
    path = RAW / f"{name}.json"
    if path.exists():
        body = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            body = steam_http.get_json(url, params)
        except steam_http.PermanentError as e:
            body = {"_permanent_error": e.status_code}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # never leave a half-written file that a resume would trust
    if "_permanent_error" in body:
        raise steam_http.PermanentError(body["_permanent_error"], url)
    return body


def run_now():
    """The timestamp this selection is measured at, fixed on the first run."""
    path = RAW / "run.json"
    if not path.exists():
        path.write_text(json.dumps({"now": int(time.time())}), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["now"]


def top_sellers(depth):
    """App IDs from store search sorted by top sellers, US store, games only (D3). Bundles/packages are skipped."""
    appids, skipped = [], 0
    for start in range(0, depth, 100):
        params = {"query": "", "start": start, "count": 100, "filter": "topsellers", "category1": 998,
                  "infinite": 1, "cc": "us", "l": "english"}
        page_ids, page_skipped = parse_search_results(cached_json(f"search_topsellers_{start}", SEARCH, params))
        appids += page_ids
        skipped += page_skipped
        if not page_ids and not page_skipped:
            break
    log.info("top sellers: %d app IDs, %d bundle/package rows skipped", len(appids), skipped)
    return appids


def candidate_pool(search_depth=SEARCH_DEPTH):
    """App IDs from the most-played and top-releases charts and the top-sellers search, deduped, first source wins."""
    pool = {}
    for r in cached_json("charts_most_played", CHARTS + "GetMostPlayedGames/v1/", {})["response"]["ranks"]:
        pool.setdefault(r["appid"], "most_played")
    for page in cached_json("charts_top_releases", CHARTS + "GetTopReleasesPages/v1/", {})["response"]["pages"]:
        for item in page["item_ids"]:
            pool.setdefault(item["appid"], page["url_path"])
    for appid in top_sellers(search_depth):
        pool.setdefault(appid, "top_sellers")
    return pool


def news_since(appid, since):
    """Official announcements back to `since`, paging with enddate (inclusive, so dedupe on gid; D9)."""
    posts, seen, enddate = [], set(), None
    while True:
        params = {"appid": appid, "count": 100, "maxlength": 1, "feeds": "steam_community_announcements"}
        if enddate is not None:
            params["enddate"] = enddate
        body = cached_json(f"news_{appid}_{enddate or 'latest'}", NEWS, params)
        page = [p for p in body["appnews"]["newsitems"] if p["gid"] not in seen]
        if not page:
            return posts
        posts += page
        seen |= {p["gid"] for p in page}
        enddate = min(p["date"] for p in page)
        if enddate < since:
            return posts


def step(rows, label, check):
    """Run `check` on every row still in play; it returns a drop reason or "". Logs the count that survives.

    A network error that outlasted the retries marks the row "retry: ..." rather than dropping it for good:
    transient errors aren't cached, so the next run tries just those rows again.
    """
    for i, row in enumerate(r for r in rows if not r["dropped"]):
        try:
            row["dropped"] = check(row)
        except steam_http.TransientHTTPError as e:
            log.warning("app %s: %s, will need a rerun", row["appid"], e)
            row["dropped"] = f"retry: {label}: {e}"
        except steam_http.PermanentError as e:
            log.warning("app %s: %s", row["appid"], e)
            row["dropped"] = f"{label}: PermanentError: {e}"
            row["error_status"] = e.status_code
        except (ValueError, KeyError) as e:
            log.warning("app %s: %s: %s", row["appid"], type(e).__name__, e)
            row["dropped"] = f"{label}: {type(e).__name__}: {e}"
        if (i + 1) % 50 == 0:
            log.info("  %s: %d checked", label, i + 1)
    kept = sum(1 for r in rows if not r["dropped"])
    log.info("%s: %d", label, kept)
    return kept


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--search-depth", type=int, default=SEARCH_DEPTH, help="how many top sellers to take")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    RAW.mkdir(parents=True, exist_ok=True)
    now = run_now()
    since = now - PATCH_WINDOW_DAYS * 86400

    rows = [{"appid": appid, "source": source, "dropped": ""} for appid, source in candidate_pool(args.search_depth).items()]
    log.info("candidates: %d", len(rows))

    def released(row):
        app = parse_app_type(cached_json(f"appdetails_{row['appid']}", APPDETAILS, {"appids": row["appid"], "cc": "us"}), row["appid"])
        if app is None:
            return "appdetails success: false"
        row.update(name=app["name"], type=app["type"], coming_soon=app["coming_soon"],
                   genres="|".join(app["genres"]), main_genre=main_genre(app["genres"]))
        return "" if app["type"] == "game" and not app["coming_soon"] else "not a released game"

    def over_rate_bar(row):
        page = cached_json(f"reviews_{row['appid']}", REVIEWS, {"appid": row["appid"], **REVIEW_PARAMS})["response"]
        reviews, skipped = parse_reviews(page)
        if skipped:
            log.info("app %s: skipped %d non-English review(s)", row["appid"], skipped)
        if not reviews:
            return "no reviews returned (L1)"
        rate = english_rate_per_day(reviews, now)
        # A full page means the rate comes from a short window: real, but noisier.
        row.update(rate_per_day=round(rate, 3), size=size_group(rate),
                   rate_page_full=len(reviews) + skipped >= REVIEW_PARAMS["num_per_page"])
        return "" if rate >= MIN_RATE else f"under {MIN_RATE} English review/day"

    def patch_count(row):
        """Recorded for information only (L4): a failed news call leaves it blank instead of dropping the game."""
        try:
            row["patches_6mo"] = count_patches(news_since(row["appid"], since), since)
        except (steam_http.PermanentError, KeyError) as e:
            log.warning("app %s: no patch count: %s: %s", row["appid"], type(e).__name__, e)
            row["patches_6mo"] = None
        return ""

    step(rows, "released games", released)
    step(rows, "over the rate bar", over_rate_bar)
    step(rows, "with patch counts", patch_count)

    with open(CANDIDATES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # A cluster of 403s/429s here means throttling, not missing apps: delete those cache files and rerun.
    permanent = Counter(r["error_status"] for r in rows if "error_status" in r)
    if permanent:
        log.warning("permanent errors by status: %s", dict(sorted(permanent.items())))
    retry = sum(1 for r in rows if r["dropped"].startswith("retry:"))
    if retry:
        log.error("%d candidates hit network errors; games.csv not written. Rerun to retry just those.", retry)
        raise SystemExit(1)

    survivors = [r for r in rows if not r["dropped"]]
    selected = draw(survivors, seed=args.seed)
    with open(GAMES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GAMES_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({**r, "seed": args.seed} for r in selected)

    counted = [r for r in selected if r["patches_6mo"] is not None]
    patched = sum(1 for r in counted if r["patches_6mo"] > 0)
    log.info("selected: %d (seed %d) | sizes %s", len(selected), args.seed,
             dict(sorted(Counter(r["size"] for r in selected).items())))
    log.info("detected patches in 6 months (unreliable, L4): %d of %d counted (%.0f%%)", patched, len(counted),
             100 * patched / len(counted) if counted else 0)
    if len(selected) < TARGET:
        log.warning("only %d games selected, short of the %d target", len(selected), TARGET)
    log.info("wrote %s", GAMES_CSV)


if __name__ == "__main__":
    main()
