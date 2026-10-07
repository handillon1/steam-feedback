"""Turn Steam responses into clean rows. No network here: everything is testable with saved fixtures."""
import re

from bs4 import BeautifulSoup

SECONDS_PER_DAY = 86400
MIN_RATE_SPAN = 3600  # an hour: stops a near-zero span blowing up the rate without capping busy games
# D4: over 10% non-English on a page of 10+ reviews means the language filter broke, not a stray review.
MAX_NON_ENGLISH_SHARE = 0.10
MIN_REVIEWS_TO_JUDGE = 10

# Kept by name, so a field Steam adds later (possibly personal) never reaches the database unseen (D7).
REVIEW_FIELDS = ["recommendationid", "language", "review", "timestamp_created", "timestamp_updated",
                 "voted_up", "votes_up", "votes_funny", "weighted_vote_score", "comment_count",
                 "steam_purchase", "received_for_free", "written_during_early_access",
                 "primarily_steam_deck", "refunded", "developer_response", "timestamp_dev_responded"]

# D9: patch/update/hotfix/bugfix in the title, or a version number like 1.4.5.7.
_PATCH_TITLE = re.compile(r"\b(?:patch|update|hotfix|hot fix|bug ?fix(?:es)?)\b|\bv?\d+\.\d+(?:\.\d+)+\b", re.I)


def parse_reviews(page: dict) -> tuple[list[dict], int]:
    """Rows from one GetAppReviews page: REVIEW_FIELDS plus playtime_at_review, nothing else (no steamid, D7).

    Every row has the same keys; an absent optional field (e.g. developer_response) is None.
    Returns (rows, skipped_non_english). A stray non-English review is skipped and counted (D4); raises
    ValueError only when they are over MAX_NON_ENGLISH_SHARE of a page of at least MIN_REVIEWS_TO_JUDGE.

    The last page has no "reviews" key at all (D5), which gives ([], 0).
    """
    reviews = page.get("reviews", [])
    rows = []
    for review in reviews:
        if review["language"] != "english":
            continue
        row = {field: review.get(field) for field in REVIEW_FIELDS}
        row["playtime_at_review"] = (review.get("author") or {}).get("playtime_at_review")
        rows.append(row)
    skipped = len(reviews) - len(rows)
    if len(reviews) >= MIN_REVIEWS_TO_JUDGE and skipped > MAX_NON_ENGLISH_SHARE * len(reviews):
        raise ValueError(f"{skipped} of {len(reviews)} reviews aren't English: language filter broke (D4)")
    return rows, skipped


def english_rate_per_day(reviews: list[dict], now: int) -> float:
    """Reviews per day, from the oldest review to now (D3), so a game that went quiet a month ago scores low.

    The span is at least MIN_RATE_SPAN. A full page from a busy game covers only hours (CS2: ~2.3h),
    so its rate is real but noisy; callers should record whether the page was full.
    """
    if not reviews:
        return 0.0
    oldest = min(r["timestamp_created"] for r in reviews)
    span = max(now - oldest, MIN_RATE_SPAN)
    return len(reviews) * SECONDS_PER_DAY / span


def parse_app_type(details: dict, appid: int) -> dict | None:
    """Selection fields from an appdetails response, or None when Steam says success: false (fake/removed app)."""
    entry = details[str(appid)]
    if not entry.get("success"):
        return None
    data = entry["data"]
    return {
        "appid": appid,
        "name": data["name"],
        "type": data["type"],
        "is_free": data.get("is_free", False),
        "coming_soon": (data.get("release_date") or {}).get("coming_soon", False),
        "genres": [g["description"] for g in data.get("genres", [])],
    }


def is_patch(post: dict) -> bool:
    """D9: tagged "patchnotes", or a patch-like title. Knowingly accepts some false positives.

    Assumes the post is from the developer's own feed (callers filter with feeds=steam_community_announcements);
    on press/blog posts the title rule would fire on any article that mentions an update.
    Known false positives, kept until week 2 measures the rule: "Community/Roadmap/Weekly Update" (the word
    "update") and dates like "10.05.2026" (the version-number pattern).
    """
    return "patchnotes" in (post.get("tags") or []) or bool(_PATCH_TITLE.search(post["title"]))


def parse_search_results(page: dict) -> tuple[list[int], int]:
    """App IDs from one store search page (infinite=1 JSON), in rank order, plus how many rows were skipped.

    Bundles and packages have no data-ds-appid or list several app IDs ("1,2,3"); they aren't one game, so they're skipped.
    """
    appids, skipped = [], 0
    for row in BeautifulSoup(page["results_html"], "html.parser").select("a.search_result_row"):
        appid = row.get("data-ds-appid", "")
        if appid.isdigit():
            appids.append(int(appid))
        else:
            skipped += 1
    return appids, skipped
