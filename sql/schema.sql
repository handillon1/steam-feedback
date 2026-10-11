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

-- Optional but useful in week 2: how far back each game's history is complete (from backfill/_summary.json).
CREATE TABLE IF NOT EXISTS review_coverage (
    appid          integer PRIMARY KEY REFERENCES games (appid),
    complete_from  date,      -- first day with every review; later than 2025-10-05 for capped games
    capped         boolean NOT NULL
);