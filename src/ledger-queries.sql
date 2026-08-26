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
-- IF YOU ONLY WANT ONE ANSWER, IT IS Q10 at the bottom: every dollar you put in,
-- against the same dollars in SPY over the same holding periods. Q1-Q4 grade the
-- pipeline, Q7-Q9 compare the two streams; Q10 is the one that says how you did.
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
--
-- ONE RULE THAT IS NOT OPTIONAL: `source`
-- verdict_log holds THREE different things, and every query here must decide
-- which of them it is asking about:
--
--   PIPELINE — the SCREEN chose the name and the agents analysed it. Only these
--              have a `run_universe` control group standing behind them.
--   ONDEMAND — the agents analysed it, but the screen never chose it: a one-off
--              `main.py TICKER`. The judgement is the system's; the PICK is the
--              operator's.
--   MANUAL   — not an analysis at all. A purchase made on an outside
--              recommendation, mirrored in from the position tracker so it can
--              be measured on the same arithmetic (see sync_external_picks.py).
--
-- THE PIPELINE/ONDEMAND LINE IS THE EASY ONE TO GET WRONG, because both are this
-- system reasoning. The distinction is the SCREEN, not the reasoning:
--
--   * Q1 asks "did filtering the screen beat NOT filtering it", and measures
--     approvals against `run_universe`. A one-off run writes no universe rows,
--     so counting its verdict there compares approvals against a universe that
--     never contained the name — it inflates the approved side out of a
--     denominator it was never in. Q1 is PIPELINE ONLY.
--   * Q2/Q3/Q4 grade the JUDGEMENT — does the verdict carry information, which
--     factors do we read well, did the critic help. A one-off analysis is a real
--     analyst call with real factors behind it, so excluding it would throw away
--     signal for no reason. Those read PIPELINE + ONDEMAND.
--
-- Everything else is a purchase and must never earn the screen credit or blame
-- for a pick it never made — which does not merely add noise, it silently
-- answers a different question. Q7-Q10 are the queries that deliberately put the
-- streams side by side.
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
   AND v.source = 'PIPELINE'
UNION ALL
SELECT 'Names we called Avoid',
       COUNT(*),
       ROUND(AVG(lv.total_return_pct), 2),
       ROUND(AVG(lv.age_days))
  FROM latest_verdict lv
  JOIN verdict_log v USING (verdict_id)
 WHERE v.verdict = 'AVOID'
   AND v.source = 'PIPELINE';


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
 -- PIPELINE + ONDEMAND: both are analyst calls this system made and stands
 -- behind. Only Q1 needs the screened-universe denominator that ONDEMAND lacks.
 WHERE v.source IN ('PIPELINE', 'ONDEMAND')
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
   -- A factor articulated in a one-off analysis was read just as right or wrong
   -- as one from a screened candidate. Dropping those would shrink the sample
   -- this query already waits longest to fill.
   AND v.source IN ('PIPELINE', 'ONDEMAND')
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
 -- refine.py inherits the reviewed verdict's source, so a refined one-off keeps
 -- its INDEPENDENT and POST_CRITIC rows in the same population — which is the
 -- only way this pairing means anything.
 WHERE v.source IN ('PIPELINE', 'ONDEMAND')
   AND v.ticker IN (SELECT ticker FROM verdict_log
                     WHERE source IN ('PIPELINE', 'ONDEMAND')
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
       COUNT(DISTINCT prompt_version)::text FROM verdict_log
UNION ALL
SELECT 'external picks logged',
       COUNT(*)::text FROM verdict_log WHERE source <> 'PIPELINE';


-- ---------------------------------------------------------------------------
-- Q7. HEAD TO HEAD: the pipeline's picks, the picks made elsewhere, and the
--     market.
--
-- The question this ledger was extended to answer. Most of the money in this
-- household follows externally-sourced recommendations, and until those
-- purchases were recorded there was nothing to compare the pipeline against
-- except the index — which flatters or damns both streams together depending
-- only on what the market did.
--
-- Every cohort is measured the same way, on its LATEST snapshot, so nothing
-- here depends on the two streams being the same age or the same size.
--
-- READ THE `n` COLUMN BEFORE THE RETURN COLUMN. These two cohorts will not
-- reach a usable sample at the same time, and the smaller one will look
-- dramatic long before it means anything. A three-pick stream "beating" a
-- thirty-pick stream is a sentence about arithmetic, not about skill.
--
-- `min_age_days` is reported because a cohort bought mostly last month and one
-- held for three years are not comparable however similar their averages look.
--
-- KNOWN COVERAGE LIMIT ON THE EXTERNAL STREAM (as of 2026-08-14)
-- Twenty positions in the Roth IRA were sold inside the recorded history but
-- BOUGHT before it starts (2022-10-26), which is as far back as the broker
-- provides: ADM, AIA, ARKK, AXON, BSX, BX, CEVA, DIS, FAF, FOX, GIC, LEVI,
-- LMND, META, MTCH, NYT, OPEN, PINS, TSN, XLB. With no purchase price their
-- return cannot be computed, so they are absent from every number below and
-- cannot be recovered.
--
-- Which way that biases things is unknown, but it is not random: they are all
-- names that were sold, and sold names have trailed the market by roughly 7%
-- in the part of the record we CAN see. Read the external stream as "the picks
-- we can measure", not "every pick that was made". The other accounts have no
-- such gap — Ind-TOD-3920 and Jo-WROS-4473 are covered from their first trade.
-- ---------------------------------------------------------------------------
SELECT CASE v.source
            WHEN 'PIPELINE' THEN 'Pipeline (screened + analysed)'
            WHEN 'ONDEMAND' THEN 'On-demand (you picked, we analysed)'
            ELSE 'External (chosen elsewhere)' END AS stream,
       COUNT(*)                                    AS n,
       ROUND(AVG(o.total_return_pct), 2)           AS avg_return_pct,
       ROUND(AVG(o.excess_return_pct), 2)          AS avg_vs_market_pct,
       -- The average says how the stream did; the hit rate says whether that
       -- average is the stream or is one lucky name carrying it.
       ROUND(100.0 * COUNT(*) FILTER (WHERE o.excess_return_pct > 0)
             / NULLIF(COUNT(*) FILTER (WHERE o.excess_return_pct IS NOT NULL), 0), 1)
                                                   AS pct_beating_market,
       MIN(o.age_days)                             AS min_age_days,
       ROUND(AVG(o.age_days))                      AS avg_age_days
  FROM verdict_log v
  JOIN LATERAL (
        SELECT * FROM verdict_outcome o2
         WHERE o2.verdict_id = v.verdict_id
         ORDER BY o2.as_of DESC LIMIT 1
       ) o ON TRUE
 -- BUY only. An external row is always a BUY (money actually changed hands),
 -- so comparing it against the pipeline's Watch and Avoid calls would be
 -- comparing purchases against opinions.
 WHERE v.verdict = 'BUY'
 GROUP BY 1
 ORDER BY 1;


-- ---------------------------------------------------------------------------
-- Q8. The same question, weighted by what was actually invested.
--
-- ***********************************************************************
-- * OPTIONAL — reads stock_tracker.lots, which this project does not     *
-- * create. Skipped automatically when it is absent. See Q5.             *
-- ***********************************************************************
--
-- Q7 treats every pick as one vote. That answers "were the picks good". It does
-- NOT answer "did the money do well", and the two come apart as soon as
-- position sizes differ — which they do here by 2-3x. A $300 position tripling
-- and a $1,000 position sagging is a good record and a bad year at the same
-- time, and only this query can tell you that.
--
-- Cost basis is summed across every lot of the same purchase, because one pick
-- bought in three accounts on one day is one decision.
-- ---------------------------------------------------------------------------
SELECT to_regclass('stock_tracker.lots') IS NOT NULL AS has_lots \gset
\if :has_lots

WITH lot_cost AS (
    SELECT l.ticker, l.purchase_date, SUM(l.shares * l.buy_price) AS cost_basis
      FROM stock_tracker.lots l
     WHERE l.source <> 'PIPELINE'
     GROUP BY l.ticker, l.purchase_date
),
priced AS (
    SELECT lc.cost_basis,
           o.total_return_pct,
           o.excess_return_pct
      FROM verdict_log v
      JOIN lot_cost lc ON lc.ticker = v.ticker AND lc.purchase_date = v.verdict_date
      JOIN LATERAL (
            SELECT * FROM verdict_outcome o2
             WHERE o2.verdict_id = v.verdict_id
             ORDER BY o2.as_of DESC LIMIT 1
           ) o ON TRUE
     WHERE v.source <> 'PIPELINE'
)
SELECT COUNT(*)                                AS n,
       ROUND(SUM(cost_basis), 2)               AS invested,
       -- Equal-weighted: were the picks good?
       ROUND(AVG(excess_return_pct), 2)        AS avg_vs_market_pct,
       -- Dollar-weighted: did the money do well? Where these two disagree, the
       -- sizing decisions did the damage (or the earning), not the picking.
       ROUND(SUM(excess_return_pct * cost_basis) / NULLIF(SUM(cost_basis), 0), 2)
                                               AS dollar_weighted_vs_market_pct,
       ROUND(SUM(total_return_pct * cost_basis) / NULLIF(SUM(cost_basis), 0), 2)
                                               AS dollar_weighted_return_pct,
       -- What the same money would have gained or lost in aggregate, in dollars.
       ROUND(SUM(total_return_pct * cost_basis) / 100.0, 2)
                                               AS approx_gain_usd
  FROM priced;

\else
\echo '-- Q8 skipped: no stock_tracker.lots table in this database.'
\endif


-- ---------------------------------------------------------------------------
-- Q9. THE OVERLAP: picks that BOTH streams landed on.
--
-- The streams are not disjoint. An outside recommendation is sometimes also run
-- through this pipeline before being bought, which leaves two rows for one
-- company: a PIPELINE verdict grading the analysis, and an external row
-- grading the purchase. That is not double counting — they are different
-- claims, made on different dates, at different prices — but it does mean a
-- name can appear in both cohorts of Q7, and anyone reading Q7 should know
-- how much of it is shared.
--
-- Read this two ways:
--   * as a caveat on Q7 — if the overlap is most of both streams, Q7 is
--     largely comparing a set against itself and the "difference" is mostly
--     execution timing, not judgement;
--   * as a finding in its own right — agreement between an outside
--     recommendation and this system is a stronger signal than either alone,
--     IF the overlap column here beats both streams in Q7. That is the most
--     directly actionable thing this ledger can eventually tell you.
--
-- `verdict_gap_days` is the distance between the two, and is the closest thing
-- available to a cost of deliberation: positive means the purchase came after
-- the analysis.
-- ---------------------------------------------------------------------------
SELECT ext.ticker,
       pipe.verdict                          AS pipeline_said,
       pipe.verdict_date                     AS analysed_on,
       ext.verdict_date                      AS bought_on,
       (ext.verdict_date - pipe.verdict_date) AS verdict_gap_days,
       pipe.price                            AS price_at_analysis,
       ext.price                             AS price_paid,
       -- Positive means the purchase happened at a worse price than the
       -- analysis saw. Over many picks this is the drag from acting late.
       ROUND(100.0 * (ext.price - pipe.price) / NULLIF(pipe.price, 0), 2)
                                             AS price_drift_pct,
       po.excess_return_pct                  AS analysis_vs_market_pct,
       eo.excess_return_pct                  AS purchase_vs_market_pct
  FROM verdict_log ext
  JOIN verdict_log pipe
    ON pipe.ticker = ext.ticker
   -- ONDEMAND counts as an analysis here: "we looked at this and then bought it"
   -- is the same question whether the screen or the operator surfaced the name,
   -- and the price drift between the two dates is the point of the query.
   AND pipe.source IN ('PIPELINE', 'ONDEMAND')
   AND pipe.verdict_stage <> 'POST_CRITIC'   -- one row per analysis, not two
  LEFT JOIN LATERAL (
        SELECT * FROM verdict_outcome o WHERE o.verdict_id = pipe.verdict_id
         ORDER BY o.as_of DESC LIMIT 1) po ON TRUE
  LEFT JOIN LATERAL (
        SELECT * FROM verdict_outcome o WHERE o.verdict_id = ext.verdict_id
         ORDER BY o.as_of DESC LIMIT 1) eo ON TRUE
 WHERE ext.source <> 'PIPELINE'
 ORDER BY ext.verdict_date DESC;


-- ---------------------------------------------------------------------------
-- Q10. THE BOTTOM LINE: did the money beat putting the same money in SPY?
--
-- ***********************************************************************
-- * OPTIONAL — reads stock_tracker.lots. Skipped when absent. See Q5.    *
-- ***********************************************************************
--
-- RUN THIS ONE when the question is "how am I doing". Everything above
-- averages PICKS; this one follows DOLLARS, and the two disagree whenever
-- position sizes differ — which they always do.
--
-- The counterfactual is exact, not a market-wide index return: for every
-- purchase it takes the money actually spent, on the day it was actually spent,
-- and grows it by what SPY did over that same holding period. Sum both sides
-- and the difference is what the picking was worth in dollars.
--
-- WHAT THIS NUMBER IS, AND IS NOT
-- It is a verdict on DECISIONS, not a portfolio valuation. Sold positions
-- contribute their exit proceeds, and that money was presumably reinvested into
-- something else that also appears here — so `worth` is not what the account is
-- worth today. Read it as: "every buy I made, against the same dollars in SPY
-- for the same period."
--
-- The `sold` and `still held` rows are the split that matters most. Open
-- positions are marked at today's price and can still turn; sold ones are
-- settled and cannot. A strong total resting entirely on the open rows is a
-- position, not a result.
--
-- AS OF AN EARLIER DATE: add `AND o2.as_of <= DATE '2026-01-01'` inside the
-- LATERAL below. Snapshots are weekly, so it will find the nearest mark at or
-- before that date.
--
-- Bear the Q7 coverage note in mind: twenty sold Roth positions are missing
-- from this entirely, and every one of them was a sale.
-- ---------------------------------------------------------------------------
SELECT to_regclass('stock_tracker.lots') IS NOT NULL AS has_lots \gset
\if :has_lots

WITH lot_pick AS (
    -- A lot is matched to its verdict by run_id FIRST, and only by the natural
    -- key when it has no run_id. The two halves below are keyed that way rather
    -- than by what the LOT calls itself, which is the trap: a name bought off a
    -- one-off analysis is normally recorded in the tracker as MANUAL (the
    -- operator picked it) while its verdict is ONDEMAND, and a lot bought off
    -- the screened pipeline may be recorded either way. Keying on l.source
    -- alone silently drops whichever combination does not line up.
    --
    -- POST_CRITIC is excluded throughout so a refined ticker's cost is not
    -- counted twice.

    -- 1. Linked to an analysis by run_id — PIPELINE or ONDEMAND alike. A
    --    verdict's DATE is when the analysis ran, which is not necessarily when
    --    the buy happened, so the run is the only sound key here.
    SELECT v.verdict_id, SUM(l.shares * l.buy_price) AS cost
      FROM stock_tracker.lots l
      JOIN verdict_log v
        ON v.run_id::text = l.run_id
       AND v.ticker = l.ticker
       AND v.source IN ('PIPELINE', 'ONDEMAND')
       AND v.verdict_stage <> 'POST_CRITIC'
     GROUP BY v.verdict_id
    UNION ALL
    -- 2. No run_id to go on: joined on the natural key instead, accepting a
    --    MANUAL mirror OR an ONDEMAND analysis. The tracker has no 'ONDEMAND'
    --    lot source — a lot is only ever MANUAL or PIPELINE — so a name analysed
    --    on demand and then bought is a MANUAL lot pointing at an ONDEMAND
    --    verdict, and this half has to be able to see it.
    --
    --    The date must match EXACTLY. Relaxed to "same ticker", this would marry
    --    a 2017 AMZN purchase to a 2026 AMZN analysis 3,431 days later.
    --
    --    Lots claimed by half 1 are excluded so one purchase is never counted
    --    down both paths.
    SELECT v.verdict_id, SUM(l.shares * l.buy_price)
      FROM stock_tracker.lots l
      JOIN verdict_log v
        ON v.source <> 'PIPELINE'
       AND v.ticker = l.ticker
       AND v.verdict_date = l.purchase_date
       AND v.verdict_stage <> 'POST_CRITIC'
     WHERE l.source <> 'PIPELINE'
       AND NOT EXISTS (
             SELECT 1 FROM verdict_log a
              WHERE a.run_id::text = l.run_id
                AND a.ticker = l.ticker
                AND a.source IN ('PIPELINE', 'ONDEMAND'))
       -- Should a mirror and a one-off analysis both sit on this date, the
       -- ANALYSIS wins: it carries the factors and the prompt version, and
       -- counting both would attach the same dollars twice.
       AND (v.source = 'ONDEMAND' OR NOT EXISTS (
             SELECT 1 FROM verdict_log a2
              WHERE a2.ticker = l.ticker
                AND a2.verdict_date = l.purchase_date
                AND a2.source = 'ONDEMAND'))
     GROUP BY v.verdict_id
),
marked AS (
    SELECT v.source,
           CASE WHEN v.exit_date IS NULL THEN 'still held' ELSE 'sold' END AS state,
           lp.cost,
           lp.cost * (1 + o.total_return_pct / 100.0)                   AS worth,
           -- The same dollars, in the index, over the same days.
           lp.cost * (o.benchmark_price / NULLIF(v.benchmark_price, 0)) AS worth_if_spy,
           o.age_days
      FROM lot_pick lp
      JOIN verdict_log v USING (verdict_id)
      JOIN LATERAL (
            SELECT * FROM verdict_outcome o2
             WHERE o2.verdict_id = v.verdict_id
             ORDER BY o2.as_of DESC LIMIT 1
           ) o ON TRUE
     -- Both baselines must exist or the comparison is not like-for-like.
     WHERE v.benchmark_price > 0 AND o.benchmark_price > 0
),
rolled AS (
    -- Three streams, not two: 'On-demand' is money put into names the operator
    -- brought to the system and the system then vetted. Folding it into
    -- 'Pipeline' would credit the screen with picks it never surfaced; folding
    -- it into 'External' would hide that the agents did analyse them.
    SELECT CASE source WHEN 'PIPELINE' THEN 'Pipeline'
                       WHEN 'ONDEMAND' THEN 'On-demand'
                       ELSE 'External' END AS stream,
           state, cost, worth, worth_if_spy, age_days FROM marked
    UNION ALL
    SELECT CASE source WHEN 'PIPELINE' THEN 'Pipeline'
                       WHEN 'ONDEMAND' THEN 'On-demand'
                       ELSE 'External' END, 'ALL',
           cost, worth, worth_if_spy, age_days FROM marked
    UNION ALL
    SELECT 'EVERYTHING', 'ALL', cost, worth, worth_if_spy, age_days FROM marked
)
SELECT stream,
       state,
       COUNT(*)                                  AS n,
       ROUND(SUM(cost))                          AS invested,
       ROUND(SUM(worth))                         AS worth,
       ROUND(SUM(worth_if_spy))                  AS if_spy,
       ROUND(SUM(worth) - SUM(cost))             AS gain,
       -- The whole point of the query, in one column.
       ROUND(SUM(worth) - SUM(worth_if_spy))     AS vs_spy_dollars,
       ROUND(100.0 * (SUM(worth) - SUM(cost)) / NULLIF(SUM(cost), 0), 1)        AS gain_pct,
       ROUND(100.0 * (SUM(worth_if_spy) - SUM(cost)) / NULLIF(SUM(cost), 0), 1) AS spy_pct,
       ROUND(AVG(age_days))                      AS avg_days
  FROM rolled
 GROUP BY stream, state
 ORDER BY stream, state;

\else
\echo '-- Q10 skipped: no stock_tracker.lots table in this database.'
\endif
