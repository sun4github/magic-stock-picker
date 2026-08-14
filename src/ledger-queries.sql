-- ===========================================================================
-- READING THE VERDICT LEDGER
--
-- The ledger is written by the pipeline and by cron/run_outcome_snapshot.py.
-- Nothing reads it automatically — these are the questions it was built to
-- answer, written out so they are asked the same way every time.
--
-- Run the whole file with:
--     psql "$DATABASE_URL" -f src/ledger-queries.sql
--
-- Individual queries can be pasted into any client. Run the FILE with psql
-- specifically, though: Q5 is guarded by `\gset` / `\if` so it skips itself on
-- an installation that has no position tracker, and those are psql meta-commands
-- that other clients will reject. Everything else here is plain SQL.
--
-- WHEN TO START TRUSTING THE ANSWERS
-- Each query reports its own sample size (`n`). Below roughly n=30 with 6+
-- months elapsed, treat every number here as a curiosity rather than a finding:
-- a 3-name "basket" beating a 4-name one over 8 weeks is noise wearing a
-- percentage sign. The queries are written now so the habit exists before the
-- data does.
--
-- Every "return" below is EXCESS return where a benchmark baseline exists —
-- how the position did against leaving the money in the market. Raw return
-- looks like skill in any rising market and like failure in any falling one.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- Q1. THE HEADLINE QUESTION: did filtering the screen beat not filtering it?
--
-- Compares the full screened basket against the subset the agents approved.
-- This is the one the ledger exists for. If "approved" does not beat "all",
-- the analysis layer is subtracting value from the Magic Formula and the honest
-- response is to buy the whole screen.
--
-- Uses the LATEST snapshot per subject, and only subjects old enough to mean
-- anything (>= 180 days).
-- ---------------------------------------------------------------------------
WITH latest_universe AS (
    SELECT DISTINCT ON (o.universe_id)
           o.universe_id, o.total_return_pct, o.age_days
      FROM verdict_outcome o
     WHERE o.universe_id IS NOT NULL AND o.age_days >= 180
     ORDER BY o.universe_id, o.as_of DESC
),
latest_verdict AS (
    SELECT DISTINCT ON (o.verdict_id)
           o.verdict_id, o.total_return_pct, o.excess_return_pct, o.age_days
      FROM verdict_outcome o
     WHERE o.verdict_id IS NOT NULL AND o.age_days >= 180
     ORDER BY o.verdict_id, o.as_of DESC
)
SELECT 'ALL screened names'            AS cohort,
       COUNT(*)                        AS n,
       ROUND(AVG(lu.total_return_pct), 2) AS avg_return_pct,
       ROUND(AVG(lu.age_days))            AS avg_age_days
  FROM latest_universe lu
UNION ALL
SELECT 'Names we called Buy or Watch',
       COUNT(*),
       ROUND(AVG(lv.total_return_pct), 2),
       ROUND(AVG(lv.age_days))
  FROM latest_verdict lv
  JOIN verdict_log v USING (verdict_id)
 WHERE v.verdict IN ('BUY', 'WATCH')
UNION ALL
SELECT 'Names we called Avoid',
       COUNT(*),
       ROUND(AVG(lv.total_return_pct), 2),
       ROUND(AVG(lv.age_days))
  FROM latest_verdict lv
  JOIN verdict_log v USING (verdict_id)
 WHERE v.verdict = 'AVOID';


-- ---------------------------------------------------------------------------
-- Q2. Does the verdict itself carry information?
--
-- If Buy, Watch and Avoid all return about the same thing, the labels are
-- decoration. Split by prompt_version, because mixing versions compares two
-- different systems and calls the difference a trend.
-- ---------------------------------------------------------------------------
SELECT v.verdict,
       v.prompt_version,
       COUNT(*)                                AS n,
       ROUND(AVG(o.total_return_pct), 2)       AS avg_return_pct,
       ROUND(AVG(o.excess_return_pct), 2)      AS avg_vs_market_pct,
       -- The hit rate matters as much as the average: one 200% winner can
       -- carry a cohort of losers and hide that most calls were wrong.
       ROUND(100.0 * COUNT(*) FILTER (WHERE o.excess_return_pct > 0)
             / NULLIF(COUNT(*) FILTER (WHERE o.excess_return_pct IS NOT NULL), 0), 1)
                                               AS pct_beating_market
  FROM verdict_log v
  JOIN LATERAL (
        SELECT * FROM verdict_outcome o2
         WHERE o2.verdict_id = v.verdict_id AND o2.age_days >= 180
         ORDER BY o2.as_of DESC LIMIT 1
       ) o ON TRUE
 GROUP BY v.verdict, v.prompt_version
 ORDER BY v.prompt_version, v.verdict;


-- ---------------------------------------------------------------------------
-- Q3. THE FACTOR SCORECARD: which things do we read well, and which badly?
--
-- The most useful query here long term, and the one that reaches a usable
-- sample first, because it aggregates across every ticker rather than needing
-- similar companies to recur.
--
-- Read it like this: a BEAR factor is "right" when the stock went on to trail
-- the market; a BULL factor is "right" when it beat it. A factor we cite often
-- and read wrong is a specific, fixable flaw in the analyst prompt.
-- ---------------------------------------------------------------------------
SELECT b.factor_key,
       b.stance,
       COUNT(*)                           AS n,
       ROUND(AVG(o.excess_return_pct), 2) AS avg_vs_market_pct,
       ROUND(100.0 * COUNT(*) FILTER (
                WHERE (b.stance = 'BEAR' AND o.excess_return_pct < 0)
                   OR (b.stance = 'BULL' AND o.excess_return_pct > 0))
             / NULLIF(COUNT(*), 0), 1)     AS pct_read_correctly
  FROM verdict_basis b
  JOIN verdict_log v USING (verdict_id)
  JOIN LATERAL (
        SELECT * FROM verdict_outcome o2
         WHERE o2.verdict_id = v.verdict_id AND o2.age_days >= 180
         ORDER BY o2.as_of DESC LIMIT 1
       ) o ON TRUE
 WHERE o.excess_return_pct IS NOT NULL
 GROUP BY b.factor_key, b.stance
HAVING COUNT(*) >= 5          -- below this the percentage is theatre
 ORDER BY pct_read_correctly ASC, n DESC;


-- ---------------------------------------------------------------------------
-- Q4. Did the critic improve anything, or only cost money?
--
-- Only meaningful for tickers refined through refine.py, which logs a
-- POST_CRITIC verdict alongside the INDEPENDENT one.
-- ---------------------------------------------------------------------------
SELECT v.verdict_stage,
       COUNT(*)                           AS n,
       ROUND(AVG(o.excess_return_pct), 2) AS avg_vs_market_pct
  FROM verdict_log v
  JOIN LATERAL (
        SELECT * FROM verdict_outcome o2
         WHERE o2.verdict_id = v.verdict_id AND o2.age_days >= 180
         ORDER BY o2.as_of DESC LIMIT 1
       ) o ON TRUE
 WHERE v.ticker IN (SELECT ticker FROM verdict_log
                     GROUP BY ticker HAVING COUNT(DISTINCT verdict_stage) > 1)
 GROUP BY v.verdict_stage;


-- ---------------------------------------------------------------------------
-- Q5. REAL MONEY: how did the positions actually bought do, against the
--     verdict that led to them?
--
-- ***********************************************************************
-- * OPTIONAL. This is the one query here that reads a table THIS PROJECT *
-- * DOES NOT CREATE, and most installations will not have it.            *
-- ***********************************************************************
--
-- `stock_tracker.lots` is a position ledger kept by a SEPARATE application —
-- one row per purchase: ticker, shares, buy price, purchase date. This project
-- deliberately tracks no positions of its own (see the README), so if you want
-- to grade the trades you actually made rather than the opinions the system
-- formed, that record has to come from wherever you already keep it.
--
-- The join key is the pipeline `run_id`, stored against each purchase. It is the
-- only link from a trade back to the analysis that justified it, and it cannot
-- be reconstructed afterwards — a ticker alone will not do it, because the same
-- name can be analysed many times and reach different verdicts.
--
-- TO USE THIS WITH YOUR OWN SETUP, change three things below:
--   * `stock_tracker.lots`  -> wherever your purchases live
--   * `l.run_id`            -> your column holding the pipeline run id
--   * `l.buy_price`, `l.purchase_date`, `l.ticker` -> your column names
--
-- The guard below skips the whole query when that table is absent, so this file
-- runs cleanly end to end on an installation that has no position tracker at
-- all. Nothing else in this file depends on it.
--
-- Two returns are reported, and the difference is the point: the VERDICT's
-- return grades the analysis, the LOT's return grades your execution. They
-- diverge whenever you bought at a different price or on a different day from
-- the verdict, which is the normal case.
--
-- Returns no rows (rather than skipping) when the table exists but no purchase
-- carries a run_id yet.
-- ---------------------------------------------------------------------------
SELECT to_regclass('stock_tracker.lots') IS NOT NULL AS has_lots \gset
\if :has_lots

SELECT l.ticker,
       l.purchase_date,
       l.buy_price,
       v.verdict,
       v.verdict_date,
       v.price                            AS price_at_verdict,
       o.as_of                            AS measured_on,
       o.total_return_pct                 AS verdict_return_pct,
       o.excess_return_pct                AS verdict_vs_market_pct,
       -- The lot's own return differs from the verdict's whenever the purchase
       -- happened at a different price or on a different day, which is the
       -- normal case. Both are worth seeing: one grades the analysis, the other
       -- grades the execution.
       ROUND(100.0 * (o.price - l.buy_price) / NULLIF(l.buy_price, 0), 2)
                                          AS lot_return_pct
  FROM stock_tracker.lots l
  JOIN verdict_log v ON v.run_id::text = l.run_id AND v.ticker = l.ticker
  JOIN LATERAL (
        SELECT * FROM verdict_outcome o2
         WHERE o2.verdict_id = v.verdict_id
         ORDER BY o2.as_of DESC LIMIT 1
       ) o ON TRUE
 ORDER BY l.purchase_date DESC;

\else
\echo '-- Q5 skipped: no stock_tracker.lots table in this database.'
\echo '-- That is expected unless you keep purchases in one. See the notes above Q5.'
\endif


-- ---------------------------------------------------------------------------
-- Q6. Housekeeping: what is the ledger actually holding right now?
-- ---------------------------------------------------------------------------
SELECT 'verdicts logged'      AS item, COUNT(*)::text AS value FROM verdict_log
UNION ALL
SELECT 'factors logged',      COUNT(*)::text FROM verdict_basis
UNION ALL
SELECT 'screened names',      COUNT(*)::text FROM run_universe
UNION ALL
SELECT 'snapshots taken',     COUNT(*)::text FROM verdict_outcome
UNION ALL
SELECT 'oldest verdict',      COALESCE(MIN(verdict_date)::text, 'none') FROM verdict_log
UNION ALL
SELECT 'oldest tracked (days)',
       COALESCE(MAX(age_days)::text, '0') FROM verdict_outcome
UNION ALL
SELECT 'prompt versions seen',
       COUNT(DISTINCT prompt_version)::text FROM verdict_log;
