-- sql/views.sql: star schema for Power BI on top of the storage tables. Safe to rerun.
-- Run after sql/schema.sql and sql/steam_sales.sql. All dates are UTC days.
-- CREATE OR REPLACE can add columns at the end but not drop, rename or reorder them; for that, DROP VIEW first.

-- One row per day of the data window, with Steam sale flags (approximate: sales start mid-UTC-day, see steam_sales).
CREATE OR REPLACE VIEW dim_date AS
SELECT d::date                                   AS date,
       date_trunc('week', d)::date               AS week_start,   -- weeks start Monday
       date_trunc('month', d)::date              AS month,
       s.name IS NOT NULL                        AS is_sale_day,
       s.name                                    AS sale_name
FROM generate_series('2025-10-05'::date, '2026-12-31'::date, interval '1 day') AS d
LEFT JOIN steam_sales s ON d::date BETWEEN s.start_date AND s.end_date;

-- One row per game. news_posts counts official announcements since 2025-10-05; 0 means none (L4), not "no updates".
CREATE OR REPLACE VIEW dim_game AS
SELECT g.appid, g.name, g.main_genre, g.size_group, g.rate_at_selection, g.patches_6mo,
       c.complete_from, c.capped,
       coalesce(n.news_posts, 0)                 AS news_posts,
       n.news_posts IS NOT NULL                  AS has_announcements
FROM games g
LEFT JOIN review_coverage c USING (appid)
LEFT JOIN (SELECT appid, count(*) AS news_posts FROM news GROUP BY appid) n USING (appid);

-- One row per review from each game's complete_from on (L5). No review text: Power BI doesn't need it.
-- Playtime groups match prediction 1 in metrics.md; NULL when no playtime was recorded.
-- review_chars is the text length only, for Finding 3 (are longer reviews more negative?).
CREATE OR REPLACE VIEW fact_reviews AS
SELECT r.recommendationid,
       r.appid,
       (r.created_at AT TIME ZONE 'UTC')::date   AS review_date,
       r.voted_up,
       round(r.playtime_at_review_min / 60.0, 1) AS playtime_hours,
       CASE WHEN r.playtime_at_review_min <= 120  THEN '2h or less'
            WHEN r.playtime_at_review_min >= 3000 THEN '50h or more'
            WHEN r.playtime_at_review_min IS NOT NULL THEN '2-50h'
       END                                       AS playtime_group,
       r.is_short,
       r.source,
       length(btrim(r.review, E' \t\r\n'))       AS review_chars  -- same trim as is_short, so is_short = review_chars < 20
FROM reviews r
JOIN review_coverage c USING (appid)
WHERE r.created_at >= c.complete_from::timestamp AT TIME ZONE 'UTC';

-- One row per player-count measurement. fetched_at is when it was actually measured (D15).
CREATE OR REPLACE VIEW fact_player_snapshots AS
SELECT appid,
       fetched_at,
       (fetched_at AT TIME ZONE 'UTC')::date     AS snapshot_date,
       player_count
FROM player_snapshots;
