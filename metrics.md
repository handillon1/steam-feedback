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
