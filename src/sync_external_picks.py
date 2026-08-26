"""Give externally-sourced purchases a place in the verdict ledger.

The pipeline writes its own verdicts as it reaches them. Purchases made on an
outside recommendation have no such moment — they exist only as lots in the
position tracker — so this script mirrors them into `verdict_log` as BUY rows
with `source = 'MANUAL'`, which is what lets the weekly snapshot job grade them
on exactly the same arithmetic as the pipeline's own picks.

    python src/sync_external_picks.py            # dry run, writes nothing
    python src/sync_external_picks.py --commit

Safe to run repeatedly, and MEANT to run before cron/run_outcome_snapshot.py on
the same schedule. Put both on one crontab line, in this order:

    0 18 * * * cd /path/to/magic-stock-picker && .venv/bin/python \
        src/sync_external_picks.py --commit && \
        .venv/bin/python src/cron/run_outcome_snapshot.py

Order matters. The sync is what tells the snapshot job which subjects exist and
which have finished, so running the snapshot first means a day's delay on both.

Runs DAILY, not weekly. A purchase recorded in the tracker is mirrored into the
ledger, and a sale settled, within a day instead of waiting up to a week for the
next scheduled run. This costs nothing extra: the script only calls FMP when
there is something new to mirror, settle, reprice or unmirror, and still fetches
the whole date range in ONE call regardless of how many purchases fall in it.

HOW A LOT IS MATCHED TO A VERDICT, AND WHY run_id WINS
-----------------------------------------------------
A lot carrying a `run_id` was bought off an analysis this system performed, and
that analysis already wrote its own verdict row. So the run_id link is checked
FIRST, and a lot it claims is never mirrored: doing so would record one decision
twice, once as the opinion and once as the purchase, and average them as two
independent picks.

The link is honoured whatever the LOT says its source is. A name analysed by a
one-off `main.py TICKER` is usually recorded in the tracker as MANUAL, because
the operator chose it — but its verdict is ONDEMAND, and matching on 'PIPELINE'
alone would miss it. That is exactly how a second MU row reached the ledger.

Most external lots carry no run_id at all, because nothing analysed them. Those
fall back to the natural key, (ticker, purchase_date), as they always have.

FOUR THINGS IT DOES, AND WHY EACH IS NEEDED
-------------------------------------------
1. Mirrors purchases that have no ledger row yet — externally-sourced only, and
   only those NOT already linked to an analysis by run_id, since any analysis
   writes its own verdict as it reaches one.
2. SETTLES rows whose position has since been sold — BOTH streams, pipeline
   picks included.
3. REPRICES rows whose lot no longer says what it said — see "corrections".
4. UNMIRRORS rows whose lot has been deleted from the tracker — mirrored rows
   only, and see "the third case" below.

The second is not a variation of the first. A purchase is mirrored when it is
made; the sale happens later, by which point a verdict_log row already exists
and step 1 skips it forever. Without step 2 a sold position would be re-priced
every week as though still held, its return drifting with a market it had left,
and the settled result silently replaced by a hypothetical one — because every
query here reads the LATEST snapshot. Marks taken after the sale date are
deleted for the same reason.

Step 2 covers the analysis streams even though step 1 does not, and the asymmetry
is the point: a closed position is closed whoever picked it. Only the join key
differs — see "HOW A LOT IS MATCHED" above. Closing a lot in the tracker is
therefore the ONLY action required to settle it, regardless of which stream chose
it.

CORRECTIONS: A LOT THAT CHANGED
-------------------------------
The tracker can correct a lot in place — a mistyped ticker or a misplaced
decimal is a typo, not a reason to delete a purchase and lose its history. What
happens here depends on WHICH field changed, because the mirror is keyed on
(ticker, purchase_date):

  * ticker or purchase date — the key itself moved, so the old row is orphaned
    and unmirrored, and a fresh one is written. Already handled by steps 1 and 4.
  * shares or buy price — the key is unchanged, so the row is neither orphaned
    nor re-created. `pending_purchases` only ever CREATES, and skips any lot that
    already has a row. Without step 3 the ledger would go on grading the position
    against the wrong cost forever, and silently: the one failure mode here with
    no visible symptom at all.

Step 3 also catches a case that is not an edit. Buy more of the same name on the
same day, in another account, and the blended cost changes while the key does
not — so the mirror keeps the price of whichever lots existed when it was first
written. That drift was present before this step existed.

Repricing RE-DERIVES the existing marks rather than deleting them. Each
verdict_outcome row already stores the market price on its as_of date and the
benchmark on the same day; only the baseline they were measured against has
moved. Recomputing from those keeps the whole history and makes it correct,
where deleting would throw away weeks of marks to fix a decimal point.

THE THIRD CASE: A DELETED LOT
-----------------------------
Deleting a lot in the tracker is not selling it. The tracker keeps the two
apart deliberately — CLOSED means "I sold this", delete means "this purchase
never happened" — and the ledger has to honour the distinction, because a
mirrored row exists ONLY because a lot existed.

Left alone an orphan is not inert. run_outcome_snapshot re-prices anything with
exit_date IS NULL and never joins lots, so it keeps being marked at market for
the whole external tracking window — years — and counts in Q7 and Q10 as a
purchase the tracker says was never made.

Settling it instead would be worse. Writing an exit_date and a closing price
records a SALE THAT DID NOT HAPPEN, and downstream nothing can tell that
fabricated round trip from a real one.

So the mirror is removed, and verdict_outcome cascades with it. If a position
was genuinely sold, restore the lot and record the sale on it: it then settles
through step 2 with its real exit price, which is the number worth having.

UNMIRRORING IS THE ONLY DESTRUCTIVE THING HERE
----------------------------------------------
It infers "deleted" from ABSENCE, and an empty or wrong database looks exactly
like a mass deletion — point DATABASE_URL somewhere fresh and every mirrored row
qualifies at once. So absence is believed only when the tracker is demonstrably
populated and the loss is small: no lots at all, or more than MASS_UNMIRROR_SHARE
of the ledger, and it refuses and says why. --allow-mass-unmirror lifts the share
ceiling; nothing lifts the empty-tracker check. A ledger is years of accumulated
marks that no re-run can rebuild.

So: record purchases and sales in the tracker whenever you like, and let this
run on its schedule. Nothing has to be run by hand at the moment of a trade.

WHAT A MIRRORED ROW MEANS
-------------------------
`verdict_date` is the PURCHASE date and `price` is the FILL price, not a
recommendation date and not the price when the recommendation was published.
That is a deliberate choice about which question gets answered: this grades the
holder's money, including whatever was lost between reading the idea and acting
on it. Grading the source's judgement instead would need the recommendation
date, which the position tracker does not know.

`prompt_version` and `git_sha` are NULL because no prompt produced these, and
there are no `verdict_basis` rows because no factors were articulated. Both are
expected. Nothing here is inferred by a model — the whole ledger grades by
arithmetic, and a mirrored purchase is arithmetic all the way down.

WHY THE BACKFILL IS WORTH DOING AT ONCE
---------------------------------------
`total_return_pct` is computed from the baseline stored on the row rather than
accumulated from snapshots, and the benchmark close on any past date is still
retrievable. So entering years of past purchases yields correct total AND
excess return immediately, instead of a year from now.

DEPENDENCIES, AND WHY THEY ARE THIN
-----------------------------------
Same reasoning as cron/run_outcome_snapshot.py: this talks to PostgreSQL and FMP
directly rather than importing mcp_server, so it can run on the Raspberry Pi
without pandas, google-adk or the Gemini SDK. Its whole dependency set is
`requests`, `psycopg2-binary`, `python-dotenv` and `pyyaml`. Please do not
"fix" the small amount of duplication by importing the rest of the repo.
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta

import psycopg2
import requests
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(os.path.join(LOG_DIR, "cron.log"))],
)
logger = logging.getLogger("external_sync")

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))

FMP_EOD_URL = "https://financialmodelingprep.com/stable/historical-price-eod/light"
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"

_DEFAULTS = {"benchmark_symbol": "SPY", "external_source": "MANUAL"}

# Above this share of the mirrored ledger, unmirroring refuses to run without
# --allow-mass-unmirror. See unmirror_guard: the operation infers "deleted" from
# absence, and a wrong or empty database looks identical to a mass deletion.
MASS_UNMIRROR_SHARE = 0.25

# Statuses whose lots are still held as far as the tracker knows.
HELD_STATUSES = ("OPEN", "TARGET_REACHED")

# Verdict sources that represent AN ANALYSIS THIS SYSTEM PERFORMED, as opposed to
# a purchase mirrored in from the tracker. Both are keyed to a lot by run_id, and
# neither is ever mirrored or unmirrored: the analysis happened whether or not the
# name was bought.
#
# ONDEMAND is the reason this is a tuple rather than the single literal 'PIPELINE'
# it used to be. A one-off `main.py TICKER` is still this system reasoning, so it
# already has a verdict row of its own — but the lot bought off it is usually
# recorded in the tracker as MANUAL, because the operator picked the name. Match
# only on 'PIPELINE' and that lot looks external, gets mirrored, and one purchase
# becomes two verdict rows: the analysis AND a mirror of the same decision.
ANALYSIS_SOURCES = ("PIPELINE", "ONDEMAND")

# THE TRACKER HAS NO 'ONDEMAND' LOT SOURCE. A lot is only ever MANUAL or
# PIPELINE, because the tracker records who chose the name, not which command
# analysed it. So a name analysed by a one-off run and then bought is a MANUAL
# lot pointing at an ONDEMAND verdict, and matching has to be relaxed on the
# verdict side to see it:
#
#   * with a run_id  -> match the analysis on the run. Exact, and preferred.
#   * without one    -> match on (ticker, purchase_date), accepting a MANUAL or
#                       an ONDEMAND verdict. That is how BAC — analysed and
#                       bought the same day, entered without a run_id — is
#                       recognised as one decision rather than two.
#
# The date must match EXACTLY. Relaxing to "same ticker" would marry a 2017 AMZN
# purchase to a 2026 AMZN analysis 3,431 days later and call them one decision.
MIRRORABLE_SOURCES = ("ONDEMAND",)  # + external_source, added at call time

# A CLOSED lot is mirrored too, but ONLY when it carries a sale_price. Measured
# to today's price a sold position reports a return nobody earned; measured to
# its exit it is a finished, gradeable pick. A lot closed without a recorded
# price is skipped and reported, because the alternative is to invent the
# number that decides whether the pick was good.
CLOSED_STATUS = "CLOSED"


def _config() -> dict:
    path = os.path.join(SRC_ROOT, "specs", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = (yaml.safe_load(fh) or {}).get("verdict_ledger", {}) or {}
        if cfg:
            return cfg
        logger.warning("No `verdict_ledger:` block in %s; using defaults %s", path, _DEFAULTS)
    except FileNotFoundError:
        logger.warning("%s not found; using defaults %s", path, _DEFAULTS)
    except yaml.YAMLError as e:
        logger.error("Could not parse %s (%s). Fix it or remove it.", path, e)
        raise SystemExit(2)
    return dict(_DEFAULTS)


def preflight(commit: bool) -> None:
    needed = ["DATABASE_URL"] + (["FMP_API_KEY"] if commit else [])
    missing = [k for k in needed if not os.getenv(k)]
    if missing:
        logger.error(
            "Missing required environment variable(s): %s.\n"
            "  Set them in %s (or webapp/.env on a viewer-only checkout).",
            ", ".join(missing), os.path.join(PROJECT_ROOT, ".env"))
        raise SystemExit(2)


def fetch_eod_series(symbol: str, start: date, end: date) -> dict:
    """{date: close} for `symbol` over the range, in ONE call.

    One request rather than one per purchase: the range is fetched whole and
    indexed by date, so a hundred backfilled buys still cost a single call.
    """
    api_key = os.getenv("FMP_API_KEY")
    try:
        resp = requests.get(
            FMP_EOD_URL,
            params={"symbol": symbol, "from": start.isoformat(),
                    "to": end.isoformat(), "apikey": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json() or []
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.error("Historical prices for %s failed (%s).", symbol, e)
        return {}
    out = {}
    for row in rows:
        day, price = row.get("date"), row.get("price")
        if day and isinstance(price, (int, float)) and price > 0:
            out[str(day)[:10]] = float(price)
    return out


def _pct(now: float, then: float):
    """Percentage move, or None when there is no baseline to move from.

    Same rounding as cron/run_outcome_snapshot.py, deliberately: a finished
    pick's final row and a held pick's weekly rows land in the same column and
    must be computed the same way, or the two are not comparable.
    """
    if not then:
        return None
    return round((now - then) / then * 100.0, 4)


def close_on_or_before(series: dict, day: date, lookback: int = 10):
    """The close on `day`, or the most recent one before it.

    Purchases land on non-trading days more often than seems plausible — a
    holiday, a weekend-dated statement line, an off-by-one in a broker export.
    Walking back finds the last real close instead of dropping the row.
    """
    for back in range(lookback + 1):
        hit = series.get((day - timedelta(days=back)).isoformat())
        if hit is not None:
            return hit
    return None


def fetch_profile(ticker: str) -> dict:
    """Company name / sector / industry, best effort. Never fatal.

    market_cap is deliberately NOT taken from here. The profile reports it as of
    today, while every other figure on a mirrored row is as of the purchase
    date, and a column mixing the two is worse than one left empty.
    """
    try:
        resp = requests.get(FMP_PROFILE_URL,
                            params={"symbol": ticker, "apikey": os.getenv("FMP_API_KEY")},
                            timeout=20)
        resp.raise_for_status()
        rows = resp.json() or []
        if rows:
            return {"company_name": rows[0].get("companyName"),
                    "sector": rows[0].get("sector"),
                    "industry": rows[0].get("industry")}
    except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.warning("Profile lookup for %s failed (%s); leaving it blank.", ticker, e)
    return {}


def pending_purchases(cur, external_source: str) -> list:
    """Externally-sourced purchases with no ledger row yet.

    Grouped by (ticker, purchase_date) rather than taken per lot: the same pick
    bought across three accounts on one day is ONE decision with one return, and
    entering it three times would let it outvote every other pick in the
    average. The fill price is share-weighted across those lots, which is the
    price the holder actually paid in aggregate.

    A LOT THAT CARRIES A run_id IS ALREADY IN THE LEDGER
    ----------------------------------------------------
    `run_id` on a lot means the purchase was made off an analysis this system
    performed, and that analysis wrote its own verdict row when it ran. Mirroring
    the lot as well would record ONE decision twice — once as the opinion, once as
    the purchase — and the two would then be averaged as though they were separate
    picks, and double-counted in the dollar queries.

    This is not hypothetical: it is what put a second MU row in the ledger. A
    one-off `main.py MU` wrote a verdict, the resulting buy was recorded in the
    tracker as MANUAL (the operator chose the name), and the old lot-level filter
    of `source <> 'PIPELINE'` saw an external purchase with no mirror yet.

    So the run_id link is checked FIRST and takes priority over the natural key.
    Most external lots carry no run_id at all — nothing linked them to an
    analysis, because there was none — and those still match on
    (ticker, purchase_date) exactly as before.
    """
    cur.execute(
        """
        SELECT l.ticker,
               l.purchase_date,
               SUM(l.shares)                            AS shares,
               SUM(l.shares * l.buy_price) / SUM(l.shares) AS buy_price,
               SUM(l.shares * l.buy_price)              AS cost_basis,
               COUNT(*)                                 AS lot_count,
               -- A purchase counts as exited only when EVERY lot of it left.
               -- One lot sold out of two is a trimmed position, not a finished
               -- pick, and the part still held is still being measured.
               BOOL_AND(l.status::text = 'CLOSED' AND l.sale_price IS NOT NULL) AS exited,
               MAX(l.closed_on)                         AS exit_date,
               SUM(l.shares * l.sale_price) / NULLIF(SUM(l.shares), 0) AS exit_price
          FROM stock_tracker.lots l
         WHERE l.source <> 'PIPELINE'
           -- ::text because status is a stock_tracker enum type, and this repo
           -- deliberately does not depend on that type existing.
           AND (l.status::text = ANY(%s)
                OR (l.status::text = %s AND l.sale_price IS NOT NULL))
           -- Priority match: the lot is linked to an analysis that already has a
           -- verdict row, so the decision is recorded and must not be mirrored.
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_log v
                  WHERE v.run_id::text = l.run_id
                    AND v.ticker = l.ticker
                    AND v.source = ANY(%s))
           -- Fallback match on the natural key, accepting an ONDEMAND verdict as
           -- well as an existing mirror. A one-off analysis bought the same day
           -- IS the ledger row for that purchase; writing a mirror beside it
           -- records the decision twice. The date must match exactly — see
           -- MIRRORABLE_SOURCES.
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_log v
                  WHERE v.source = ANY(%s)
                    AND v.ticker = l.ticker
                    AND v.verdict_date = l.purchase_date)
         GROUP BY l.ticker, l.purchase_date
         ORDER BY l.purchase_date, l.ticker
        """,
        (list(HELD_STATUSES), CLOSED_STATUS, list(ANALYSIS_SOURCES),
         [external_source, *MIRRORABLE_SOURCES]),
    )
    return cur.fetchall()


def newly_closed(cur, external_source: str) -> list:
    """Ledger rows still marked open whose position has since been SOLD —
    BOTH streams.

    This is the ongoing case, and it is not the same as the backfill. A purchase
    is mirrored once, when it is made; the sale happens later. By then a
    verdict_log row already exists, so `pending_purchases` skips it forever —
    which would leave a sold position being re-priced every week as though it
    were still held, and its return would drift with a market it had left.

    THE PIPELINE HALF IS NOT OPTIONAL. A closed position is closed whoever
    picked it, and until this covered both streams a sold pipeline pick kept
    being marked at market forever: it never gained an exit_date, so the
    snapshot job never stopped re-pricing it, Q10 kept counting it under "still
    held", and the settled side of that split — the side that cannot turn any
    more — never learned about it. That is the exact failure this function was
    written to prevent, applied to only one of the two streams it grades.

    The two halves differ only in how a lot is matched to its verdict:
      * run_id — the only link from a trade back to the analysis that justified
        it. A ticker alone will not do: the same name can be analysed many times
        and reach different verdicts. This half covers BOTH analysis sources
        (PIPELINE and ONDEMAND) and does not care what the LOT's own source says,
        which matters because a name bought off a one-off analysis is usually
        recorded in the tracker as MANUAL — the operator picked it. Keyed on
        `v.source = 'PIPELINE'` alone, as this was before ONDEMAND existed, such
        a position could never settle: it would be marked at market forever.
      * natural key — (ticker, purchase date), for the ordinary external
        purchase that has no run_id because no analysis preceded it.

    A lot the run_id half already claims is excluded from the natural-key half,
    so one sale is never settled twice down two different paths.

    POST_CRITIC rows are deliberately NOT excluded here, unlike in the cost
    queries. Both stages describe the same holding, so both must stop being
    re-priced when it is sold; each carries its own baseline and so grades to
    its own return. Excluding them would leave the refined verdict drifting.

    Either half requires EVERY lot of that position to be closed and priced: one
    lot sold out of two is a trimmed position, not a finished pick.
    """
    cur.execute(
        """
        SELECT 'external' AS stream,
               v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price,
               MAX(l.closed_on)                                        AS exit_date,
               SUM(l.shares * l.sale_price) / NULLIF(SUM(l.shares), 0) AS exit_price
          FROM verdict_log v
          JOIN stock_tracker.lots l
            ON l.ticker = v.ticker
           AND l.purchase_date = v.verdict_date
           AND l.source <> 'PIPELINE'
           -- Not the run_id half's business. Without this a lot linked to an
           -- analysis could settle twice, down both paths, on one sale.
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_log a
                  WHERE a.run_id::text = l.run_id
                    AND a.ticker = l.ticker
                    AND a.source = ANY(%s))
         -- ONDEMAND as well as the mirror source: a lot bought off a one-off
         -- analysis and entered WITHOUT a run_id is matched here, and if this
         -- half only saw mirrors that position could never gain an exit_date.
         WHERE v.source = ANY(%s)
           AND v.exit_date IS NULL
         GROUP BY v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price
        HAVING BOOL_AND(l.status::text = 'CLOSED' AND l.sale_price IS NOT NULL)

         UNION ALL

        SELECT 'analysis',
               v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price,
               MAX(l.closed_on),
               SUM(l.shares * l.sale_price) / NULLIF(SUM(l.shares), 0)
          FROM verdict_log v
          JOIN stock_tracker.lots l
            ON l.run_id = v.run_id::text
           AND l.ticker = v.ticker
         WHERE v.source = ANY(%s)
           AND v.exit_date IS NULL
         GROUP BY v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price
        HAVING BOOL_AND(l.status::text = 'CLOSED' AND l.sale_price IS NOT NULL)

         ORDER BY 7
        """,
        (list(ANALYSIS_SOURCES), [external_source, *MIRRORABLE_SOURCES],
         list(ANALYSIS_SOURCES)),
    )
    return cur.fetchall()


def orphaned_mirrors(cur, external_source: str) -> list:
    """Mirrored rows whose lot has been DELETED from the tracker.

    The third lifecycle event, after mirroring a purchase and settling a sale.
    A mirrored verdict exists only because a lot existed; deleting the lot in
    the tracker means "this purchase never happened" (as distinct from selling
    it, which is a status change and settles through newly_closed). The mirror
    is then a record of nothing.

    Left alone it does not merely sit there: run_outcome_snapshot re-prices any
    verdict with exit_date IS NULL and never joins lots, so an orphan is marked
    at market every week for the whole external tracking window — years — and
    counts in Q7 and Q10 as a purchase the tracker says was never made.

    Settling it instead would be worse: writing an exit_date and price records a
    SALE that did not happen, and a fabricated round trip is indistinguishable
    from a real one in every query downstream.

    PIPELINE and ONDEMAND rows are never orphans. Either records an analysis that
    genuinely happened whether or not the name was ever bought, and is graded as
    an opinion in Q2/Q3 regardless; with no lot it simply carries no cost basis
    and drops out of Q10 on its own. The query reaches only `external_source`
    rows, so both are excluded by construction rather than by a filter.
    """
    cur.execute(
        """
        SELECT v.verdict_id, v.ticker, v.verdict_date, v.exit_date IS NOT NULL AS was_settled,
               (SELECT COUNT(*) FROM verdict_outcome o WHERE o.verdict_id = v.verdict_id) AS marks
          FROM verdict_log v
         WHERE v.source = %s
           AND NOT EXISTS (
                 SELECT 1 FROM stock_tracker.lots l
                  WHERE l.ticker = v.ticker
                    AND l.purchase_date = v.verdict_date
                    AND l.source <> 'PIPELINE')
         ORDER BY v.verdict_date
        """,
        (external_source,),
    )
    return cur.fetchall()


def stale_mirrors(cur, external_source: str) -> list:
    """Mirrored rows whose lot still exists but no longer says what it said.

    The tracker can correct a lot's shares or buy price in place — a misplaced
    decimal is a typo, not a reason to delete a purchase and lose its history.
    Neither field is part of the mirror's natural key, so the row is not orphaned
    and not re-mirrored: `pending_purchases` only ever CREATES, and skips any lot
    that already has a row. Without this the ledger would go on grading the
    position against the price that was typed wrong, forever, and silently —
    the one failure mode with no visible symptom at all.

    (Correcting the ticker or the purchase date is a different matter: those ARE
    the key, so the old row orphans and a fresh one is written. That path is
    already covered by orphaned_mirrors + pending_purchases.)
    """
    cur.execute(
        """
        SELECT v.verdict_id, v.ticker, v.verdict_date, v.price AS ledger_price,
               SUM(l.shares * l.buy_price) / NULLIF(SUM(l.shares), 0) AS lot_price
          FROM verdict_log v
          JOIN stock_tracker.lots l
            ON l.ticker = v.ticker
           AND l.purchase_date = v.verdict_date
           AND l.source <> 'PIPELINE'
         WHERE v.source = %s
         GROUP BY v.verdict_id, v.ticker, v.verdict_date, v.price
        -- Rounded to the cent the ledger stores, so float noise in a weighted
        -- average does not present itself as a correction every single run.
        HAVING ROUND(SUM(l.shares * l.buy_price) / NULLIF(SUM(l.shares), 0), 2)
               IS DISTINCT FROM ROUND(v.price, 2)
         ORDER BY v.verdict_date
        """,
        (external_source,),
    )
    return cur.fetchall()


def mirrored_total(cur, external_source: str) -> int:
    cur.execute("SELECT COUNT(*) FROM verdict_log WHERE source = %s", (external_source,))
    return cur.fetchone()[0]


def lots_total(cur) -> int:
    cur.execute("SELECT COUNT(*) FROM stock_tracker.lots WHERE source <> 'PIPELINE'")
    return cur.fetchone()[0]


def unmirror_guard(orphans: int, mirrored: int, lots: int, allow_mass: bool):
    """Whether unmirroring is safe to perform, and why not when it is not.

    Unmirroring is the only destructive operation in this script, and it infers
    "deleted" from ABSENCE — which is exactly what an empty or wrong database
    also looks like. Point DATABASE_URL at a fresh installation, or run against
    a tracker whose lots table has not been restored yet, and every mirrored row
    in the ledger qualifies as an orphan at once.

    So absence is only believed when the tracker is demonstrably populated and
    the loss is small. Anything else stops and says so: a ledger is years of
    accumulated marks that no re-run can rebuild, and a sync job silently
    deleting all of it because a database was empty is not a risk worth
    carrying for a convenience.
    """
    if orphans == 0:
        return True, ""
    if lots == 0:
        return False, ("the tracker has no externally-sourced lots at all, which is "
                       "indistinguishable from every lot having been deleted")
    if allow_mass:
        return True, ""
    share = orphans / mirrored if mirrored else 1.0
    if share > MASS_UNMIRROR_SHARE:
        return False, (f"{orphans} of {mirrored} mirrored rows ({share:.0%}) have no lot, "
                       f"over the {MASS_UNMIRROR_SHARE:.0%} ceiling")
    return True, ""


def unpriced_exits(cur) -> list:
    """Closed lots with no sale price — reported, never guessed at.

    Both streams, for the same reason newly_closed covers both: a pipeline lot
    closed without a recorded sale price is just as ungradeable, and just as
    worth telling the holder about, as an external one."""
    cur.execute(
        """
        SELECT l.ticker, l.purchase_date, l.closed_on
          FROM stock_tracker.lots l
         WHERE l.status::text = 'CLOSED'
           AND l.sale_price IS NULL
         ORDER BY l.closed_on
        """)
    return cur.fetchall()


def has_position_tracker(cur) -> bool:
    cur.execute("SELECT to_regclass('stock_tracker.lots') IS NOT NULL")
    return bool(cur.fetchone()[0])


def run_sync(commit: bool, allow_mass: bool = False) -> dict:
    cfg = _config()
    preflight(commit)
    benchmark = str(cfg.get("benchmark_symbol", "SPY")).strip().upper()
    external_source = str(cfg.get("external_source", "MANUAL")).strip().upper()

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    # This repo tracks no positions of its own, so the tracker's schema may
    # simply not be here. That is a normal installation, not an error.
    if not has_position_tracker(cur):
        logger.info(
            "No stock_tracker.lots table in this database, so there are no external "
            "purchases to mirror. That is expected unless you keep positions in the "
            "companion tracker app.")
        cur.close(); conn.close()
        return {"pending": 0, "written": 0}

    rows = pending_purchases(cur, external_source)
    closures = newly_closed(cur, external_source)
    orphans = orphaned_mirrors(cur, external_source)
    stale = stale_mirrors(cur, external_source)
    unmirror_ok, unmirror_block = unmirror_guard(
        len(orphans), mirrored_total(cur, external_source), lots_total(cur), allow_mass)
    if not unmirror_ok:
        logger.warning(
            "%d mirrored row(s) have no lot behind them, but unmirroring is being SKIPPED: %s. "
            "Nothing was deleted. Re-run with --allow-mass-unmirror if that is genuinely what "
            "happened.", len(orphans), unmirror_block)
        orphans = []

    if not rows and not closures and not orphans and not stale:
        logger.info("Every externally-sourced purchase already has a ledger row, "
                    "no open one has been sold since, none has lost its lot, and "
                    "none has been corrected.")
        cur.close(); conn.close()
        return {"pending": 0, "written": 0, "closed": 0}

    dates = [r[1] for r in rows]
    if rows:
        logger.info("%d purchase(s) to mirror, %s to %s.", len(rows), min(dates), max(dates))
    if closures:
        logger.info("%d already-mirrored pick(s) have since been sold.", len(closures))

    unpriced = unpriced_exits(cur)
    exited = [r for r in rows if r[6]]

    if not commit:
        if rows:
            print(f"\n{len(rows)} purchase(s) would be mirrored into verdict_log "
                  f"as source={external_source}:\n")
        for (ticker, purchase_date, shares, buy_price, cost_basis,
             lot_count, is_exited, exit_date, exit_price) in rows:
            note = f"  ({lot_count} lots)" if lot_count > 1 else ""
            if is_exited:
                held = (exit_date - purchase_date).days
                pct = (float(exit_price) - float(buy_price)) / float(buy_price) * 100
                note += f"   EXITED {exit_date} @ {float(exit_price):.2f} ({pct:+.1f}%, {held}d)"
            print(f"    {purchase_date}  {ticker:<6} {float(shares):>10.4f} @ "
                  f"{float(buy_price):>9.2f}  = {float(cost_basis):>9.2f}{note}")
        if rows:
            print(f"\n  {len(exited)} of these are finished picks, graded buy-to-exit.")
        if closures:
            print(f"\n{len(closures)} pick(s) already in the ledger have since been sold "
                  f"and would be settled:\n")
            for stream, _vid, ticker, bought, buy_price, _bp, exit_date, exit_price in closures:
                pct = (float(exit_price) - float(buy_price)) / float(buy_price) * 100
                print(f"    [{stream:<8}] {ticker:<6} bought {bought}  exited {exit_date} @ "
                      f"{float(exit_price):>9.2f}  ({pct:+.1f}%, {(exit_date - bought).days}d)")
            print("\n  Their weekly marks stop here, and any mark taken AFTER the sale "
                  "date is removed:\n  it prices a position that was no longer held.")
        if orphans:
            print(f"\n{len(orphans)} mirrored row(s) have NO LOT behind them any more and "
                  f"would be DELETED from the ledger:\n")
            for _vid, ticker, bought, was_settled, marks in orphans:
                state = "settled" if was_settled else "still open"
                print(f"    {ticker:<6} bought {bought}  ({state}, {marks} snapshot"
                      f"{'' if marks == 1 else 's'} would go with it)")
            print("\n  These were mirrored from tracker lots that have since been deleted.")
            print("  Deleting a lot means the purchase never happened -- so the mirror is a")
            print("  record of nothing. It is removed rather than settled: writing an exit")
            print("  would put a sale that never occurred into Q7, Q8 and Q10.")
            print("  If one of these was actually SOLD, stop: restore the lot and record the")
            print("  sale on it instead, and it will settle normally on the next run.")
        if stale:
            print(f"\n{len(stale)} mirrored row(s) no longer match their lot and would be "
                  f"REPRICED:\n")
            for _vid, ticker, bought, ledger_price, lot_price in stale:
                print(f"    {ticker:<6} bought {bought}  ledger {float(ledger_price):>9.2f} "
                      f"->  lot {float(lot_price):>9.2f}")
            print("\n  The lot was corrected in the tracker. Existing marks are re-derived")
            print("  from the new cost: the market prices already recorded are kept, only")
            print("  the returns measured against them change.")
        print(f"\n  Benchmark baseline would be {benchmark} on each purchase date"
              + (" and each exit date." if exited or closures else "."))
        if unpriced:
            print(f"\n  {len(unpriced)} closed lot(s) have NO recorded sale price and are "
                  f"skipped:\n    " + ", ".join(f"{t} ({d})" for t, d, _ in unpriced[:10]))
            print("    Record what they sold for and they become gradeable.")
        print("\nDRY RUN - nothing written. Re-run with --commit.")
        cur.close(); conn.close()
        return {"pending": len(rows), "written": 0, "closed": len(closures),
                "would_unmirror": len(orphans), "would_reprice": len(stale)}

    # One call covers every purchase AND exit date, however far back they go.
    span = dates + [r[2] for r in closures]
    last = max(dates + [r[7] for r in exited if r[7]] + [r[5] for r in closures])
    series = fetch_eod_series(benchmark, min(span) - timedelta(days=10), last)
    if not series:
        logger.error("No %s history returned; excess return would be NULL for every "
                     "row. Refusing to write a scoreboard that cannot compare to the "
                     "market. Fix the API access and re-run.", benchmark)
        cur.close(); conn.close()
        raise SystemExit(1)

    profiles, written, finals, no_baseline = {}, 0, 0, []
    for (ticker, purchase_date, shares, buy_price, cost_basis,
         lot_count, is_exited, exit_date, exit_price) in rows:
        bench_price = close_on_or_before(series, purchase_date)
        if bench_price is None:
            # Recorded anyway: an absolute return is still worth having, and a
            # missing index close must not cost the whole row.
            no_baseline.append((ticker, purchase_date))
        if ticker not in profiles:
            profiles[ticker] = fetch_profile(ticker)
        profile = profiles[ticker]

        cur.execute(
            """
            INSERT INTO verdict_log (
                ticker, company_name, verdict_date, verdict, verdict_stage,
                price, sector, industry, benchmark_symbol, benchmark_price, source,
                exit_date, exit_price
            ) VALUES (%s, %s, %s, 'BUY', 'INDEPENDENT', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING verdict_id
            """,
            (ticker, profile.get("company_name"), purchase_date, buy_price,
             profile.get("sector"), profile.get("industry"),
             benchmark if bench_price else None, bench_price, external_source,
             exit_date if is_exited else None, exit_price if is_exited else None),
        )
        got = cur.fetchone()
        if not got:
            continue
        written += 1
        if not is_exited:
            continue

        # The finished pick's one and only outcome row, priced at the exit
        # rather than at today. The snapshot job will never revisit it, because
        # exit_date is set — so this is the number that stands.
        bench_exit = close_on_or_before(series, exit_date)
        total = _pct(float(exit_price), float(buy_price))
        excess = None
        if total is not None and bench_exit and bench_price:
            bench_move = _pct(bench_exit, float(bench_price))
            excess = round(total - bench_move, 4) if bench_move is not None else None
        cur.execute(
            """
            INSERT INTO verdict_outcome (
                verdict_id, ticker, as_of, age_days, price,
                benchmark_symbol, benchmark_price, total_return_pct, excess_return_pct
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (got[0], ticker, exit_date, (exit_date - purchase_date).days, exit_price,
             benchmark if bench_exit else None, bench_exit, total, excess),
        )
        finals += cur.rowcount

    # Settle picks — mirrored or pipeline-native — whose position has been sold.
    settled = 0
    for _stream, verdict_id, ticker, bought, buy_price, bench_then, exit_date, exit_price in closures:
        cur.execute("UPDATE verdict_log SET exit_date = %s, exit_price = %s "
                    "WHERE verdict_id = %s", (exit_date, exit_price, verdict_id))

        # Any weekly mark taken after the sale prices a position that was no
        # longer held. Left in place it would also be the LATEST snapshot, and
        # every query here reads the latest one — so the settled result would be
        # silently overridden by a hypothetical one.
        cur.execute("DELETE FROM verdict_outcome WHERE verdict_id = %s AND as_of > %s",
                    (verdict_id, exit_date))

        bench_exit = close_on_or_before(series, exit_date)
        total = _pct(float(exit_price), float(buy_price))
        excess = None
        if total is not None and bench_exit and bench_then:
            bench_move = _pct(bench_exit, float(bench_then))
            excess = round(total - bench_move, 4) if bench_move is not None else None
        cur.execute(
            """
            INSERT INTO verdict_outcome (
                verdict_id, ticker, as_of, age_days, price,
                benchmark_symbol, benchmark_price, total_return_pct, excess_return_pct
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (verdict_id, ticker, exit_date, (exit_date - bought).days, exit_price,
             benchmark if bench_exit else None, bench_exit, total, excess),
        )
        settled += 1

    # Reprice mirrors whose lot was corrected in the tracker.
    #
    # The already-recorded marks are RE-DERIVED, not deleted. Each verdict_outcome
    # row stores the market price on its as_of date and the benchmark on the same
    # day; only the baseline they were measured against has changed. Recomputing
    # from those preserves the whole history and simply makes it correct, where
    # deleting would throw away weeks of marks to fix a decimal point.
    repriced = 0
    for verdict_id, ticker, bought, ledger_price, lot_price in stale:
        cur.execute("UPDATE verdict_log SET price = %s WHERE verdict_id = %s",
                    (lot_price, verdict_id))
        cur.execute(
            """
            UPDATE verdict_outcome o
               SET total_return_pct = ROUND(100.0 * (o.price - v.price)
                                            / NULLIF(v.price, 0), 4),
                   excess_return_pct = CASE
                       WHEN v.benchmark_price > 0 AND o.benchmark_price IS NOT NULL
                       THEN ROUND(100.0 * (o.price - v.price) / NULLIF(v.price, 0)
                                  - 100.0 * (o.benchmark_price - v.benchmark_price)
                                    / NULLIF(v.benchmark_price, 0), 4)
                       ELSE NULL END
              FROM verdict_log v
             WHERE v.verdict_id = o.verdict_id
               AND o.verdict_id = %s
               AND o.price IS NOT NULL
            """,
            (verdict_id,),
        )
        logger.info("Repriced %s bought %s: %.2f -> %.2f, and re-derived %d mark(s).",
                    ticker, bought, float(ledger_price), float(lot_price), cur.rowcount)
        repriced += 1

    # Remove mirrors whose lot has been deleted. verdict_outcome and
    # verdict_basis both cascade off verdict_log, so the row's whole history
    # goes with it — which is the point: it was measuring a purchase that,
    # according to the tracker, never happened.
    unmirrored = 0
    for verdict_id, ticker, bought, was_settled, marks in orphans:
        cur.execute("DELETE FROM verdict_log WHERE verdict_id = %s", (verdict_id,))
        unmirrored += cur.rowcount
        logger.info("Unmirrored %s bought %s (%s, %d snapshot(s) removed): its lot is gone "
                    "from the tracker.", ticker, bought,
                    "was settled" if was_settled else "was open", marks)

    conn.commit()
    cur.close()
    conn.close()

    if repriced:
        logger.info("%d mirrored row(s) repriced after their lot was corrected.", repriced)
    if unmirrored:
        logger.info("%d mirrored row(s) removed because their lot was deleted.", unmirrored)
    if settled:
        logger.info("%d pick(s) settled at their exit and removed from weekly marking.",
                    settled)
    if unpriced:
        logger.warning("%d closed lot(s) have no recorded sale price and stay out of the "
                       "ledger: %s", len(unpriced),
                       ", ".join(f"{t} ({d})" for t, d, _ in unpriced[:8]))
    if finals:
        logger.info("%d finished pick(s) graded buy-to-exit.", finals)

    if no_baseline:
        logger.warning("No %s close found for %d purchase(s); their excess return "
                       "will stay NULL: %s", benchmark, len(no_baseline),
                       ", ".join(f"{t} {d}" for t, d in no_baseline[:5]))
    return {"pending": len(rows), "written": written, "graded_at_exit": finals,
            "settled_since_sold": settled, "unmirrored": unmirrored,
            "repriced": repriced,
            "unpriced_exits": len(unpriced), "benchmark": benchmark}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror externally-sourced purchases into the verdict ledger.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    parser.add_argument("--allow-mass-unmirror", action="store_true",
                        help="Permit removing mirrored rows even when most of the ledger "
                             "has lost its lots. Off by default: that pattern is what a "
                             "wrong or empty database looks like, and the deletion cannot "
                             "be undone. Only pass this if you know the lots really were "
                             "deleted.")
    args = parser.parse_args()

    result = run_sync(args.commit, args.allow_mass_unmirror)
    if args.commit:
        logger.info("External pick sync complete. %s", result)
        if result.get("written"):
            logger.info("Next: cron/run_outcome_snapshot.py will price these on its "
                        "weekly run, giving each one a return against the benchmark.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logger.exception("External pick sync failed with an unhandled exception.")
        sys.exit(1)
