-- sql/patch_events.sql: the D9 patch rule as views, so a changed rule (week 3) needs no table rebuild. Safe to rerun.
-- Run after sql/schema.sql. Must stay in step with is_patch in collector/parse.py
-- (parity check in notebooks/04_patch_events.ipynb).
-- Postgres regexes use \y for a word boundary; \b is backspace there.

-- One row per official post, with each rule as its own column so each can be measured.
-- rule_version uses ~* like the rest: parse.py compiles the whole pattern with re.I, so "V1.2.3" counts.
CREATE OR REPLACE VIEW news_patch_flags AS
SELECT gid, appid, posted_at, (posted_at AT TIME ZONE 'UTC')::date AS post_date, title,
       'patchnotes' = ANY(tags)                                                  AS rule_tag,
       title ~* '\y(patch|update|hotfix|hot fix|bug ?fix(es)?)\y'                AS rule_keyword,
       title ~* '\yv?\d+\.\d+(\.\d+)+\y'                                         AS rule_version
FROM news;

-- Posts any rule flags. No contents: join news for the text (week 3's Gemini step); Power BI doesn't need it.
CREATE OR REPLACE VIEW patch_events AS
SELECT appid, gid, post_date, posted_at, title, rule_tag, rule_keyword, rule_version
FROM news_patch_flags
WHERE rule_tag OR rule_keyword OR rule_version;
