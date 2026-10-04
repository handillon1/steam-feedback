# Decision log

Each entry records what was decided, why, and what was rejected. Evidence for the 2026-10-04 entries is in `notebooks/01_explore_apis.ipynb`.

## Setup (2026-10-03)

### D1. Postgres runs in Docker
- **Decision:** `pgvector/pgvector:pg17` container on port 5433, data in the `steam_pgdata` volume.
- **Why:** pgvector (needed in week 5) is painful to compile on Windows; the Docker image ships with it.
- **Rejected:** reusing the existing Windows Postgres install.

### D2. Python 3.14
- **Decision:** the project venv uses Python 3.14.
- **Why:** it's the installed version, and everything used so far installs and runs on it.
- **Fallback:** if a week 3–5 library lacks 3.14 support, recreate `.venv` with Python 3.12.

## Data collection (2026-10-04)

### D3. Game selection: chart-based candidate pool, filtered by English review rate
- **Problem:** GetAppList returns about 190k games. Checking each one at 1 call/sec would take more than 2 days, so don't scan the full list.
- **Decision:**
  1. **Candidates:** `ISteamChartsService/GetMostPlayedGames` (top 100 by players) plus the app IDs from `ISteamChartsService/GetTopReleasesPages` (monthly "top releases" lists). That gives a few hundred to ~1,000 apps.
  2. **Type check:** appdetails for each candidate; keep `type == "game"` and `release_date.coming_soon == false`; drop apps that return `success: false`.
  3. **English review rate:** one reviews call per candidate (`filter=1&languages[0]=english&purchase_type=1&num_per_page=100`). Rate = number of reviews ÷ days from the oldest of those 100 to now.
  4. **Threshold:** keep games with at least ~1 English review/day, then pick ~300 (see open questions O1–O3).
- **Why the review rate:** it measures exactly what the analysis needs. `total_reviews` doesn't, because it's all-time and all-language, so it can't tell an old hit from a game people are reviewing in English today.
- **Rejected:** scanning all 190k games (too slow); filtering on `total_reviews` (wrong measure).
- **Status:** draft until O1–O3 are decided.

### D4. Review request parameters
- **Decision:** `filter=1`, `languages[0]=english`, `purchase_type=1`.
- **Why `filter=1`:** the default sort is "most helpful in the last 30 days", and `filter=recent` is silently ignored. Verified: `timestamp_created` goes down across all reviews and across pages.
- **Why `languages[0]=english`:** GetAppReviews is a protobuf service, so `language=english` and `languages=english` are silently ignored. English only looked filtered because it's the default; `languages[0]=schinese` proves the real filter works. The parser asserts every review's `language == "english"`.
- **Why `purchase_type=1` (all):** the default `0` counts only reviews from Steam purchases. It hides 42% of CS2's English reviews and 100% of Marvel Rivals', so free-to-play games would be undercounted or missing. `steam_purchase` and `received_for_free` are stored so they can be filtered in the analysis.

### D5. Daily stopping rule
- **Decision:** store the newest `timestamp_created` already collected per game. Each day, page from `cursor=*` with the D4 parameters, passing back each response's `cursor`. Stop when any of these is true:
  - no `reviews` key comes back (end of stream; the key is missing, not an empty list)
  - the cursor comes back unchanged
  - a review's `timestamp_created` is older than the newest one already collected (keep only the newer reviews from that page)
- **Safety:** a maximum number of pages per game per run, so one review-bombed game can't stall the job.
- **Dedupe:** on `recommendationid` when saving, because reviews posted during a run can shift the page boundaries.

### D6. Retry policy (`collector/http.py`)
- **Decision:** retry only errors that can go away (timeouts, connection errors, 429, 5xx), with exponential backoff and at most 5 attempts.
- **Why:** a 404 / `result: 42` or a 400 is permanent. Retrying it wastes the 1 call/sec budget, so log it and move on.

### D7. Reviewer privacy
- **Decision:** `author.steamid` is dropped in the parser and never stored.
- **Test fixtures:** 3 reviews each, fake steamids (below the real SteamID64 range), text cut to 300 characters.

### D8. Prices
- **Decision:** `cc=us` everywhere, because prices differ by region. Daily prices use appdetails with many appids plus `filters=price_overview`, so 300 games take a handful of calls.
- **Free games:** in that mode they come back as `data: []`. Recorded as 0 cents with `is_free = true`, so "free" can't be confused with "price missing because the call failed".

### D9. Patch detection
- **Decision:** official posts only (`feeds=steam_community_announcements`). A post is a patch if `"patchnotes" in tags`, OR the title contains patch/update/hotfix/bugfix, OR the title has a version number like 1.4.5.7.
- **Why not the tag alone:** it misses patches. Terraria tags none of its patches, and Valheim's "Patch 0.221.10" was untagged.
- **Known false positives:** the version pattern catches non-patches ("Terraria 1.4.5 - the Launch Date Revealed").
- **Week 2 follow-up:** measure each rule on 20 hand-checked posts; flag any game with zero tagged patches in a year.
- **Backfill:** page backwards with `enddate` until past 12 months. `enddate` is inclusive, so dedupe on `gid`.

### D10. Store page scraping
- **Age gate:** send cookies `birthtime=0; lastagecheckage=1-0-1990; wants_mature_content=1`. Without them, mature games redirect (302) to `/agecheck/app/<id>/`, which returns status 200 and can contain tag-like elements that would be wrong data. Even with the cookies, the run fails for that game unless the final URL is `/app/<id>/`.
- **Tags:** taken from the `InitAppTagModal(...)` JSON (with vote counts), not the visible tag links.
- **Fail loudly:** this depends on Steam's page code, so `parse_tags` raises on zero tags and a test parses the saved HTML fixture. A redesign breaks the run instead of silently producing empty tags.

## Estimates (2026-10-04)

### E1. English reviews per day for a typical game
- **Estimate:** about 30–40 per day, so 1 page per game on most days.
- **Evidence:** across the top 100 most-played games, using the page-1 rate (newest 100 English reviews, all purchase types): median 77/day; ranks 51–100 median ~39/day; max ~1,170/day. A 300-game list goes deeper than the top 100, so the typical game will be at or below the ranks 51–100 figure. Stardew Valley gets ~96/day; CS2 ~1,050/day (about 11 pages).
- **Daily run cost:** ~300 games × 1–2 pages plus a few heavy hitters ≈ 500–700 calls ≈ 10–12 minutes at 1 call/sec.

## Known limitations

### L1. Games whose reviews the API doesn't return
With `purchase_type=1`, only Deadlock (1422450) in the top 100 returned nothing; it's unreleased/invite-only, and its store page says "No user reviews". Before fixing `purchase_type`, big free-to-play live-service games (Marvel Rivals, Limbus Company) looked empty. Any game that still returns nothing is excluded, so the sample under-represents the biggest live-service games.

### L2. English only
Findings describe English-speaking reviewers. CS2 has 1.5M English reviews out of 5.3M total.

### L3. Candidate pool comes from charts
The pool is built from most-played and top-release charts, so it leans toward very large and recently released games. Older mid-size games that aren't on any chart can't enter the sample.

### D11. The game list is frozen
- **Decision:** select ~300 games once, before the first scheduled run; never add games later. Drops (delisted, reviews disabled) are logged with date and reason, and their earlier data is kept.
- **Why:** week 7 compares the same games before and after patches. A monthly refresh would move games in and out mid-study.
- **Rejected:** monthly refresh.

### D12. Low-patch games are kept on purpose
- **Decision:** don't filter on patching. Target ~75–80% patch-active and ~20–25% low-patch games.
- **Why:** dropping non-patchers selects the sample on the treatment. The main week 7 comparison is active games that didn't patch a given topic at the time; low-patch games keep contrast but may be declining, so they aren't the primary comparison.
- **Rejected:** requiring a patch in the last 12 months.

### D13. English review rate is a threshold, not a ranking
- **Decision:** keep candidates with ≥1 English review/day; group by size (1–10, 10–100, 100+/day), main genre tag and patch activity; draw randomly within groups with a fixed, logged seed. Cap the 100+/day group at ~40 games.
- **Why:** the 100-review rate can span hours for big games, so ranking on it would select for current spikes (review bombs, patch surges). As a low bar, that noise doesn't change who passes.
- **Rejected:** taking the top 300 by rate.