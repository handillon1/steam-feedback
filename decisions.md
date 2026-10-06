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
- sentence-transformers and transformers installed cleanly on 3.14 (2026-10-03)

## Data collection (2026-10-04)

### D3. Candidate pool from charts and top sellers, filtered by English review rate
- **Problem:** GetAppList returns about 190k games. Checking each one at 1 call/sec would take more than 2 days, so don't scan the full list.
- **Decision:**
  1. **Candidates:** `ISteamChartsService/GetMostPlayedGames` (top 100 by players), the app IDs from `ISteamChartsService/GetTopReleasesPages` (monthly "top releases" lists), and the top 1,500 of the US store's top sellers (`store.steampowered.com/search/results/?filter=topsellers&category1=998&cc=us&infinite=1`, 15 pages of 100). The two charts alone gave only 183 apps, too few for 300. Snapshot taken 2026-10-05; pages are cached, so reruns reuse it. Bundle/package rows are skipped and counted.
     - **Why top sellers, US:** the store's "User Reviews" sort ranks by % positive weighted by volume, not by review count, so it would pull in small, well-liked games (checked 2026-10-05). Top sellers covers a broad range of sizes. `cc=us` matches D8, and global top sellers would add games sold mostly outside English-speaking markets that would fail the English review bar anyway.
  2. **Type check:** appdetails for each candidate; keep `type == "game"` and `release_date.coming_soon == false`; drop apps that return `success: false`.
  3. **English review rate:** one reviews call per candidate (`filter=1&languages[0]=english&purchase_type=1&num_per_page=100`). Rate = number of reviews ÷ days from the oldest of those 100 to now.
  4. **Threshold:** keep games with at least ~1 English review/day, then pick ~300 (see D11–D13).
- **Why the review rate:** it measures exactly what the analysis needs. `total_reviews` doesn't, because it's all-time and all-language, so it can't tell an old hit from a game people are reviewing in English today.
- **Rejected:** scanning all 190k games (too slow); filtering on `total_reviews` (wrong measure).
- top-releases chart only returned the Dec 2024–Feb 2025 lists, so it isn't contributing recent releases
- Action is 58% of survivors' main genre. That's fine, because the draw is proportional, and tags for the final 300 handle week 7's matching

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

### D6. Retry policy (`collector/steam_http.py`)
- **Decision:** retry only errors that can go away (timeouts, connection errors, 429, 5xx), with exponential backoff and at most 5 attempts.
- **Why:** a 404 / `result: 42` or a 400 is permanent. Retrying it wastes the 1 call/sec budget, so log it and move on.
- a cluster of 403s/429s means throttling; clear those cache files and rerun

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
- **Age gate:** send cookies `birthtime=0; lastagecheckage=1-0-1990; wants_mature_content=1`. Without them, mature games redirect (302) to `/agecheck/app/<id>/`, which returns status 200 and can contain tag-like elements that would be wrong data. Even with the cookies, the run fails for that game unless the final URL's path starts with `/app/<id>/`. (A substring check missed `/agecheck/app/<id>/`; caught by a test on 2026-10-05.)
- **Tags:** taken from the `InitAppTagModal(...)` JSON (with vote counts), not the visible tag links.
- **Fail loudly:** this depends on Steam's page code, so `parse_tags` raises on zero tags and a test parses the saved HTML fixture. A redesign breaks the run instead of silently producing empty tags.

### D11. The game list is frozen
- **Decision:** select ~300 games once, before the first scheduled run; never add games later. Drops (delisted, reviews disabled) are logged with date and reason, and their earlier data is kept.
- **Why:** week 7 compares the same games before and after patches. A monthly refresh would move games in and out mid-study.
- **Rejected:** monthly refresh.

### D12. Patch activity is not used in selection
- **Decision:** the draw is grouped by size and main genre only. `patches_6mo` is recorded in games.csv for information.
- **Why:** title/tag patch detection (D9) is unreliable: 553 of 1,370 survivors (40%) showed zero patches in 6 months, including Rust, Overwatch and No Man's Sky, which name their updates ("LIVESTOCK", "COSMO", "Season 4") without patch keywords or tags. Grouping on a measure this noisy would control nothing.
- **Changed from:** targeting ~75–80% patch-active games (2026-10-04).
- **Follow-up:** better patch detection in weeks 2–3 (full post text, Gemini classification, possibly Steam's event categories); the patch-active share of the final 300 is measured then.

### D13. English review rate is a threshold, not a ranking
- **Decision:** keep candidates with ≥1 English review/day; group by size and main genre; draw randomly within groups with a fixed, logged seed. Cap the 100+/day group at ~40 games.
- **Why:** the 100-review rate can span hours for big games, so ranking on it would select for current spikes (review bombs, patch surges). As a low bar, that noise doesn't change who passes.
- **Rejected:** taking the top 300 by rate.

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
The pool is built from most-played, top releases and US top sellers. All three lean toward games that are popular right now, so steady mid-size games that aren't trending, and free games with little revenue, are underrepresented. Top sellers ranks by current revenue, so the pool also favors paid games and games that were on discount on the snapshot day (2026-10-05).

### L4. Title/tag patch detection misses named content updates
Large live-service games often name updates without the word "patch" or a `patchnotes` tag. Until detection improves, patch counts undercount exactly the biggest updates.