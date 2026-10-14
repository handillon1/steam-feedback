-- sql/steam_sales.sql: Steam seasonal sales in the data window (2025-10-05 onward). Safe to rerun.
-- Data lives here, not in views: views join to this table to flag sale days.
-- Only sitewide seasonal sales. Next Fest (demos, not discounts), publisher sales and individual game discounts aren't
-- included. Game-level discounts come from daily prices once collected.
--
-- end_date is the LAST day of the sale (inclusive), as Valve announces it, unlike the half-open periods in metrics.md.
-- Test a day with `d BETWEEN start_date AND end_date`.
-- Approximation: sales start and end at 10 a.m. Pacific (17:00 or 18:00 UTC), mid-way through a UTC day. Flagging
-- whole UTC days is close enough for weekly analysis, but the first flagged day is only ~7 hours of sale.

CREATE TABLE IF NOT EXISTS steam_sales (
    name        text PRIMARY KEY,
    start_date  date NOT NULL,
    end_date    date NOT NULL CHECK (end_date >= start_date),
    source_url  text NOT NULL
);

INSERT INTO steam_sales (name, start_date, end_date, source_url) VALUES
    -- Only Oct 5-6 fall inside the data window.
    ('Autumn Sale 2025', '2025-09-29', '2025-10-06', 'https://www.pcgamer.com/games/steam-autumn-sale-2025-live/'),
    ('Winter Sale 2025', '2025-12-18', '2026-01-05', 'https://gameinformer.com/psa/2025/12/16/the-steam-winter-sale-starts-this-week-and-includes-discounts-on-hades-ii-dispatch'),
    ('Spring Sale 2026', '2026-03-19', '2026-03-26', 'https://steamdb.info/sales/history/'),
    ('Summer Sale 2026', '2026-06-25', '2026-07-09', 'https://steamdb.info/sales/history/'),
    -- The top-sellers snapshot (2026-10-05) was taken during this sale (L3).
    ('Autumn Sale 2026', '2026-10-01', '2026-10-08', 'https://steamdb.info/sales/history/')
ON CONFLICT (name) DO UPDATE
    SET start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date, source_url = EXCLUDED.source_url;
