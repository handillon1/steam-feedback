# Metric definitions

Each metric has one definition sentence and one query. The queries are self-contained so they can be pasted into
psql or a Power BI native query; change the dates in the `params` line.

## Rules shared by every metric

- **Period:** a half-open range of UTC dates, `[start_date, end_date)`. `start_date = 2026-09-13, end_date =
  2026-10-13` is the 30 days from Sep 13 through Oct 12. A review's date is its `created_at` in UTC.
- **Don't run past the data:** `end_date` must be no later than the day after the newest `data/reviews/date=*.parquet`
  file. Today's reviews aren't collected yet, so a period ending tomorrow would look like a drop.
- **Coverage (L5):** a game's reviews count only from its `review_coverage.complete_from`. Each game's covered
  period is `[greatest(start_date, complete_from), end_date)`. A game with `complete_from >= end_date` has no covered
  days and gets no row. This applies to all metrics, not just volume, so every number about a game in a period comes
  from the same set of reviews.
- **Short reviews are included** in every metric here (D18). `is_short` is for text work only.

## Review volume

**The number of English reviews of a game created in its covered period.**

Volume depends on how many days a game was covered, so it compares games only when the period is fully covered for
both. Use reviews per day to compare games.

```sql
WITH params AS (SELECT date '2026-09-13' AS start_date, date '2026-10-13' AS end_date),
cov AS (
    SELECT c.appid, greatest(p.start_date, c.complete_from) AS from_date, p.end_date
    FROM review_coverage c CROSS JOIN params p
    WHERE c.complete_from < p.end_date
)
SELECT cov.appid, g.name, count(r.recommendationid) AS review_volume
FROM cov
JOIN games g USING (appid)
LEFT JOIN reviews r ON r.appid = cov.appid
    AND r.created_at >= cov.from_date::timestamp AT TIME ZONE 'UTC'
    AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
GROUP BY cov.appid, g.name
ORDER BY cov.appid;
```

## English reviews per day

**A game's review volume divided by the number of days in its covered period.**

The denominator is covered days, not calendar days in the period. Otherwise a capped game whose history starts
partway through the period would look quieter than it is.

```sql
WITH params AS (SELECT date '2026-09-13' AS start_date, date '2026-10-13' AS end_date),
cov AS (
    SELECT c.appid, greatest(p.start_date, c.complete_from) AS from_date, p.end_date
    FROM review_coverage c CROSS JOIN params p
    WHERE c.complete_from < p.end_date
)
SELECT cov.appid, g.name,
       cov.end_date - cov.from_date AS covered_days,
       count(r.recommendationid) AS review_volume,
       round(count(r.recommendationid)::numeric / (cov.end_date - cov.from_date), 2) AS reviews_per_day
FROM cov
JOIN games g USING (appid)
LEFT JOIN reviews r ON r.appid = cov.appid
    AND r.created_at >= cov.from_date::timestamp AT TIME ZONE 'UTC'
    AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
GROUP BY cov.appid, g.name, cov.from_date, cov.end_date
ORDER BY cov.appid;
```

## Share positive

**Thumbs-up reviews divided by all reviews of a game in its covered period, reported only when there are 30 or more
reviews; otherwise blank (NULL).**

With 3 reviews, the share can only be 0%, 33%, 67% or 100%, and a chart would show that noise as a dramatic swing.
At 30 reviews, the 95% margin of error on a share near 80% is about ±14 points. That's still wide, but it's enough to
separate a well-liked game from a disliked one. The `reviews` column is returned too, so a report can show how much
data sits behind each share.

```sql
WITH params AS (SELECT date '2026-09-13' AS start_date, date '2026-10-13' AS end_date),
cov AS (
    SELECT c.appid, greatest(p.start_date, c.complete_from) AS from_date, p.end_date
    FROM review_coverage c CROSS JOIN params p
    WHERE c.complete_from < p.end_date
)
SELECT cov.appid, g.name,
       count(r.recommendationid) AS reviews,
       CASE WHEN count(r.recommendationid) >= 30 THEN round(avg(r.voted_up::int), 3) END AS share_positive
FROM cov
JOIN games g USING (appid)
LEFT JOIN reviews r ON r.appid = cov.appid
    AND r.created_at >= cov.from_date::timestamp AT TIME ZONE 'UTC'
    AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
GROUP BY cov.appid, g.name
ORDER BY cov.appid;
```

## Share positive by playtime group (prediction 1)

**Share positive for one game in its covered period, computed separately for reviews written after 2 hours of
playtime or less (`playtime_at_review_min <= 120`) and after 50 hours or more (`playtime_at_review_min >= 3000`).
Each group's share follows the same 30-review minimum.**

- `playtime_at_review_min` is in **minutes**. It's the reviewer's total playtime in that game when they wrote the
  review.
- Reviews between 2 and 50 hours, and reviews with no playtime recorded, are in neither group.
- **Compared within each game, not pooled across games.** Pooled, the 100+/day games would supply most of both
  groups, and the result would describe them. Prediction 1 is tested on the games where both groups have a share.
- **Use a long period.** Over 30 days (Sep 13 – Oct 12), only 61 games had 30+ reviews in both groups. Most games
  get too few reviews at 2 hours or less.
- **The finding describes those games, not all 300.** The 61 are mostly busy games: 35 of the 39 in the 100+/day
  group, 25 of 82 in 10–100, and 1 of 179 in 1–10. State this whenever prediction 1's result is reported.
- **Battlefield REDSEC (3028330) has no playtime** on any of its 5,000 reviews, so it's never in either group.

```sql
WITH params AS (SELECT date '2026-09-13' AS start_date, date '2026-10-13' AS end_date),
cov AS (
    SELECT c.appid, greatest(p.start_date, c.complete_from) AS from_date, p.end_date
    FROM review_coverage c CROSS JOIN params p
    WHERE c.complete_from < p.end_date
)
SELECT cov.appid, g.name,
       count(*) FILTER (WHERE r.playtime_at_review_min <= 120) AS reviews_2h_or_less,
       CASE WHEN count(*) FILTER (WHERE r.playtime_at_review_min <= 120) >= 30
            THEN round(avg(r.voted_up::int) FILTER (WHERE r.playtime_at_review_min <= 120), 3)
       END AS share_positive_2h_or_less,
       count(*) FILTER (WHERE r.playtime_at_review_min >= 3000) AS reviews_50h_or_more,
       CASE WHEN count(*) FILTER (WHERE r.playtime_at_review_min >= 3000) >= 30
            THEN round(avg(r.voted_up::int) FILTER (WHERE r.playtime_at_review_min >= 3000), 3)
       END AS share_positive_50h_or_more
FROM cov
JOIN games g USING (appid)
LEFT JOIN reviews r ON r.appid = cov.appid
    AND r.created_at >= cov.from_date::timestamp AT TIME ZONE 'UTC'
    AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
GROUP BY cov.appid, g.name
ORDER BY cov.appid;
```

## Very short reviews

**A review is short (`reviews.is_short`) when it has fewer than 20 characters after trimming leading and trailing
spaces, tabs and newlines (D18).**

Short reviews stay in every rating metric above, because their thumbs-up or thumbs-down is still a rating. Week 3's
text work filters them out with `WHERE NOT is_short`. `is_short` is a generated column in `sql/schema.sql`, so it
always matches `review`.

```sql
WITH params AS (SELECT date '2026-09-13' AS start_date, date '2026-10-13' AS end_date),
cov AS (
    SELECT c.appid, greatest(p.start_date, c.complete_from) AS from_date, p.end_date
    FROM review_coverage c CROSS JOIN params p
    WHERE c.complete_from < p.end_date
)
SELECT cov.appid, g.name,
       count(r.recommendationid) AS reviews,
       count(*) FILTER (WHERE r.is_short) AS short_reviews,
       round(avg(r.is_short::int), 3) AS share_short
FROM cov
JOIN games g USING (appid)
LEFT JOIN reviews r ON r.appid = cov.appid
    AND r.created_at >= cov.from_date::timestamp AT TIME ZONE 'UTC'
    AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
GROUP BY cov.appid, g.name
ORDER BY cov.appid;
```

## Negative review trend

**As of day D, a game has a negative review trend when its share positive over the last 30 days is at least 5
points below its share positive over the 90 days before that, and the drop is bigger than chance: the upper end of
the 95% Newcombe interval for (recent minus baseline) is below zero. Both windows need 30+ reviews; otherwise the
result is blank (NULL). It's a monitoring alert, not proof of decline.**

- **Windows, written out:** as of day D (`end_date` in the query), the recent window is `[D - 30, D)` and the
  baseline is `[D - 120, D - 30)`: UTC dates, half-open, like every metric here. With D = 2026-10-15 that's Sep 15 –
  Oct 14 vs Jun 17 – Sep 14. D is exclusive, so in Power BI it's the day *after* the last day selected in the date
  slicer, and the rule above applies: D can be at most the day after the newest review file.
- **Both bars are needed.** Without the 5-point floor, the interval alone flags tiny changes in the busiest games:
  CS2's ~30,000 reviews a month make a 0.5-point dip "significant". Without the interval, the floor alone flags
  noise in small games (±14 points at 30 reviews). A drop has to be big enough to matter *and* too big to be luck.
- **Interval: Newcombe's hybrid score interval** (method 10), built from each window's Wilson interval. With `r`, `b`
  the recent and baseline shares and `[l_r, u_r]`, `[l_b, u_b]` their Wilson 95% intervals, the upper end of
  (r − b) is `(r - b) + sqrt((u_r - r)^2 + (b - l_b)^2)`. Wilson's bound for a share `p` of `n` reviews, z = 1.96, is
  `(p + z²/2n ± z·sqrt(p(1-p)/n + z²/4n²)) / (1 + z²/n)`. **Why not the simpler normal approximation**,
  `(r - b) ± 1.96·sqrt(r(1-r)/n_r + b(1-b)/n_b)`: it fails near 100% with small samples, and many games sit above
  95%. At 30 reviews and 100% positive its standard error is zero, so a game going from 100% to 95% would be flagged
  on 30 reviews. Wilson keeps a sensible width at the edges. It's closed-form, so it still fits in DAX.
- **Why 30 and 90 days:** 30 matches the other metrics' default period. A 90-day baseline gives a 1–10/day game
  (median ~3/day) about 270 reviews, so the baseline is stable and the recent window carries most of the noise.
- **Coverage (L5):** each window starts no earlier than `complete_from`. A capped game whose history begins inside
  the baseline window gets a shorter baseline; one whose history begins inside the recent window has no baseline and
  gets NULL.
- **Launch month left out of the baseline:** the baseline starts no earlier than 30 days after the game's first
  covered review, as in Finding 2. A launch's first reviews differ from the steady state, and a game released 60 days
  ago would otherwise be judged against its launch. Games older than the data are unaffected, since their first
  covered day (2025-10-05) is long before any baseline.
- **Everything counts:** short reviews (D18), sale days and review bombs. A bomb is a real drop in what players
  say, so it should show up; whether it's on-topic is a separate question.
- **Sales don't cause false flags.** Finding 2: share positive on sale days is within 0.2 points of other days
  (CI −0.5 to +0.9), so a sale inside either window doesn't move the comparison. Sales change volume, which only
  narrows the interval.
- **Expected false alarms.** About 250 games get a result at any D. A game with no real change passes the one-sided
  2.5% test by chance, so ~6 would be flagged on the interval alone; the 5-point floor removes most of those. A flag
  is a lead to check, not a result. A time series repeats the test at each week's end, and a trend that persists
  over several consecutive weeks is stronger evidence than one flagged week.
- **Reviews aren't independent.** The interval treats every review as a separate draw. A review bomb clusters
  negative reviews on a few days, and players react together to the same patch or news, so the real uncertainty is
  larger and the interval is too narrow: more flags than the ~6 above. Week 7 deals with this properly (e.g.
  resampling days instead of reviews).
- **Regression to the mean.** A baseline that includes an unusually good stretch (a well-received big update, a
  free weekend of fans) makes a return to normal look like a drop. The flag can't tell "got worse" from "stopped
  being unusually good". Week 7 problem; check the baseline's own history before reading a flag as decline.
- **Leaving Early Access (L6)** in either window means the comparison spans the 1.0 release. That's often the
  biggest update a game ships and a real change, but the flag then has an obvious cause to check first.
- **History:** first written 2026-10-15 with the normal approximation, and run once (19 of 235 flagged at
  D = 2026-10-15). Switched to Newcombe the same day, for the reason above, before anything was reported from it.
  Newcombe flags 21 of 235 at the same D: the same 19 plus StarRupture and Five Nights at Freddy's: Secret of the
  Mimic, both with upper ends just below zero (−0.004, −0.003).

```sql
WITH params AS (SELECT date '2026-10-15' AS end_date),   -- D
first_covered AS (
    SELECT r.appid, min(r.created_at AT TIME ZONE 'UTC')::date AS first_day
    FROM reviews r JOIN review_coverage c USING (appid)
    WHERE r.created_at >= c.complete_from::timestamp AT TIME ZONE 'UTC'
    GROUP BY r.appid
),
cov AS (
    SELECT c.appid, p.end_date,
           greatest(p.end_date - 30, c.complete_from)                      AS recent_from,
           greatest(p.end_date - 120, c.complete_from, f.first_day + 30)   AS base_from,
           p.end_date - 30                                                 AS base_to
    FROM review_coverage c CROSS JOIN params p
    JOIN first_covered f USING (appid)
    WHERE c.complete_from < p.end_date
),
agg AS (
    SELECT cov.appid,
           count(r.recommendationid) FILTER (WHERE r.created_at < cov.base_to::timestamp AT TIME ZONE 'UTC'
                                               AND r.created_at >= cov.base_from::timestamp AT TIME ZONE 'UTC')   AS base_reviews,
           avg(r.voted_up::int)      FILTER (WHERE r.created_at < cov.base_to::timestamp AT TIME ZONE 'UTC'
                                               AND r.created_at >= cov.base_from::timestamp AT TIME ZONE 'UTC')   AS base_share,
           count(r.recommendationid) FILTER (WHERE r.created_at >= cov.recent_from::timestamp AT TIME ZONE 'UTC') AS recent_reviews,
           avg(r.voted_up::int)      FILTER (WHERE r.created_at >= cov.recent_from::timestamp AT TIME ZONE 'UTC') AS recent_share
    FROM cov
    LEFT JOIN reviews r ON r.appid = cov.appid
        AND r.created_at >= least(cov.base_from, cov.recent_from)::timestamp AT TIME ZONE 'UTC'
        AND r.created_at <  cov.end_date::timestamp AT TIME ZONE 'UTC'
    GROUP BY cov.appid
),
wilson AS (   -- Wilson bounds (z = 1.96, z² = 3.8416): upper for recent, lower for baseline
    SELECT *,
           (recent_share + 1.9208 / nullif(recent_reviews, 0)
            + 1.96 * sqrt(recent_share * (1 - recent_share) / nullif(recent_reviews, 0) + 0.9604 / nullif(recent_reviews, 0) ^ 2))
           / (1 + 3.8416 / nullif(recent_reviews, 0))                                                        AS recent_upper,
           (base_share + 1.9208 / nullif(base_reviews, 0)
            - 1.96 * sqrt(base_share * (1 - base_share) / nullif(base_reviews, 0) + 0.9604 / nullif(base_reviews, 0) ^ 2))
           / (1 + 3.8416 / nullif(base_reviews, 0))                                                          AS base_lower
    FROM agg
),
scored AS (   -- Newcombe: upper end of (recent - baseline)
    SELECT *,
           recent_share - base_share AS change,
           recent_share - base_share + sqrt((recent_upper - recent_share) ^ 2 + (base_share - base_lower) ^ 2) AS change_ci_upper
    FROM wilson
)
SELECT s.appid, g.name, s.base_reviews, round(s.base_share, 3) AS base_share,
       s.recent_reviews, round(s.recent_share, 3) AS recent_share,
       round(s.change, 3) AS change, round(s.change_ci_upper::numeric, 3) AS change_ci_upper,
       CASE WHEN s.base_reviews >= 30 AND s.recent_reviews >= 30
            THEN s.change <= -0.05 AND s.change_ci_upper < 0
       END AS negative_trend
FROM scored s
JOIN games g USING (appid)
ORDER BY s.appid;
```
