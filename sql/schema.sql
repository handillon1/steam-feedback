-- sql/schema.sql: run once; safe to rerun (IF NOT EXISTS everywhere).
-- Postgres 17 + pgvector in Docker (D1). Raw Unix timestamps from Steam become timestamptz here.

CREATE TABLE IF NOT EXISTS games (
    appid                integer PRIMARY KEY,
    name                 text    NOT NULL,
    main_genre           text,
    size_group           text    NOT NULL CHECK (size_group IN ('1-10', '10-100', '100+')),
    rate_at_selection    double precision,   -- English reviews/day measured at selection (D13)
    patches_6mo          integer,            -- informational only (D12, L4)
    selection_seed       integer NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    recommendationid          bigint      PRIMARY KEY,   -- Steam's review ID; the dedupe key (D5)
    appid                     integer     NOT NULL REFERENCES games (appid),
    review                    text        NOT NULL,
    language                  text        NOT NULL CHECK (language = 'english'),   -- D4
    created_at                timestamptz NOT NULL,
    updated_at                timestamptz,
    voted_up                  boolean     NOT NULL,
    votes_up                  integer,
    votes_funny               integer,
    weighted_vote_score       double precision,
    comment_count             integer,
    steam_purchase            boolean,
    received_for_free         boolean,
    written_during_early_access boolean,
    primarily_steam_deck      boolean,
    refunded                  boolean,
    playtime_at_review_min    integer,       -- minutes, from author.playtime_at_review
    developer_response        text,
    dev_responded_at          timestamptz,
    source                    text        NOT NULL CHECK (source IN ('backfill', 'daily'))
);
CREATE INDEX IF NOT EXISTS reviews_appid_created ON reviews (appid, created_at);
-- Under 20 characters after trimming ("good", "10/10"): kept for rating metrics, skipped by text work (D18).
-- Generated, so it can't drift from review; ALTER, not in CREATE TABLE, so rerunning adds it to an existing table.
-- Trims tabs and newlines too: plain btrim() only trims spaces, and many reviews end in newlines.
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS is_short boolean
    GENERATED ALWAYS AS (length(btrim(review, E' \t\r\n')) < 20) STORED;
-- ADD COLUMN IF NOT EXISTS won't change an existing column. Databases built before 2026-10-13 got the old
-- spaces-only version; it was changed once with (rewrites the table, ~10 s):
--   ALTER TABLE reviews ALTER COLUMN is_short SET EXPRESSION AS (length(btrim(review, E' \t\r\n')) < 20);

CREATE TABLE IF NOT EXISTS player_snapshots (
    appid         integer     NOT NULL REFERENCES games (appid),
    fetched_at    timestamptz NOT NULL,      -- actual time measured; runs can be hours late (D15)
    player_count  integer     NOT NULL CHECK (player_count >= 0),
    PRIMARY KEY (appid, fetched_at)
);

-- Which Parquet files are already in the database, so the loader skips them.
CREATE TABLE IF NOT EXISTS loaded_files (
    path           text        PRIMARY KEY,   -- relative to data/, e.g. 'reviews/date=2026-10-05.parquet'
    rows_in_file   integer     NOT NULL,
    rows_inserted  integer     NOT NULL,      -- lower than rows_in_file means overlap, which is expected
    loaded_at      timestamptz NOT NULL DEFAULT now()
);

-- how far back each game's history is complete (from backfill/_summary.json).
CREATE TABLE IF NOT EXISTS review_coverage (
    appid          integer PRIMARY KEY REFERENCES games (appid),
    complete_from  date,      -- first day with every review; later than 2025-10-05 for capped games
    capped         boolean NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    gid        text        PRIMARY KEY,   -- Steam's post ID; text, not bigint (see below)
    appid      integer     NOT NULL REFERENCES games (appid),
    posted_at  timestamptz NOT NULL,
    title      text        NOT NULL,
    contents   text,
    url        text,
    author     text,
    feedname   text,
    tags       text[]      NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS news_appid_posted ON news (appid, posted_at);
-- One row per game per daily price snapshot (D8, D19). Prices are US cents. status says which response it was:
-- priced; free (0 cents, is_free); no_price (not free, no single price: NULL, not 0); not_found (success: false).
CREATE TABLE IF NOT EXISTS price_snapshots (
    appid             integer     NOT NULL REFERENCES games (appid),
    snapshot_date     date        NOT NULL,   -- the run's UTC day, from the file name
    fetched_at        timestamptz NOT NULL,   -- ~3 a.m. Pacific or later: before that day's 10 a.m. sale starts (D19)
    status            text        NOT NULL CHECK (status IN ('priced', 'free', 'no_price', 'not_found')),
    is_free           boolean,
    initial_cents     integer,
    final_cents       integer,
    discount_percent  integer,
    PRIMARY KEY (appid, snapshot_date)
);
