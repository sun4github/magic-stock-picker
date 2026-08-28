"""Price snapshot for every open subject in the verdict ledger, run daily.

Intended to be scheduled externally via crontab, alongside magic-stock-tracker's
`run_daily_check.py` on the same Raspberry Pi:

    0 18 * * * /path/to/magic-stock-picker/.venv/bin/python \
        /path/to/magic-stock-picker/src/cron/run_outcome_snapshot.py

The cron fires every day, and `verdict_ledger.snapshot_interval_days` in
config.yaml is set to match (1 by default), so every open subject gets a fresh
price each day — that is what feeds a daily dashboard. The two are still
independent knobs: raise the interval (7 for weekly, say) if daily granularity
is not needed and cutting FMP call volume matters more than it does here (see
"WHY A ROLLING INTERVAL..." below). Either way it costs nothing extra to check
daily — a subject that is not due yet is skipped without an FMP call.

WHY THIS FILE IMPORTS NOTHING FROM THE REST OF THE REPO
-------------------------------------------------------
`main.py` and `mcp_server.py` pull in google-adk, pandas, edgartools and the
Gemini SDK. None of that is needed here — this job fetches quotes and subtracts —
and installing it on a Pi means building pandas from source for no benefit. So
this module talks to PostgreSQL and FMP directly and duplicates a ~15-line quote
helper rather than importing one. That duplication is deliberate; please do not
"fix" it by importing mcp_server.

Its full dependency set is `requests`, `psycopg2-binary`, `python-dotenv` and
`pyyaml` — see requirements-cron.txt. It needs exactly two environment
variables, DATABASE_URL and FMP_API_KEY, and sends no notifications: a price
being recorded is not news, and an alerting scoreboard would train its reader to
ignore it.

WHY A ROLLING INTERVAL RATHER THAN AT 30/90/180/365 DAYS
---------------------------------------------------------
Fixed horizons lose a datapoint permanently whenever a run is missed on the day
a horizon falls, and over a year a missed run is a certainty rather than a risk.
A rolling snapshot costs one quote call per due symbol, survives outages, and
lets the horizon marks be derived from `age_days` at read time instead of
captured. This is about `snapshot_interval_days`, the per-subject cadence — not
how often the cron itself fires, which is daily (see module docstring above).
"""
import json
import logging
import os
import sys
from datetime import date

import psycopg2
import requests
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        # cron discards a job's stdout unless the crontab line redirects it, so
        # the file handler is the only durable record of an unattended run.
        logging.FileHandler(os.path.join(LOG_DIR, "cron.log")),
    ],
)
logger = logging.getLogger("verdict_ledger")

# The repo root .env is the normal home for these, and is what exists on the
# machine that runs the agents. The Raspberry Pi this job is deployed to is a
# webapp-only checkout, where the ONLY .env is webapp/.env and it already holds a
# working DATABASE_URL. Reading that as a fallback means the Pi does not need a
# second copy of the same secret in a second file — two copies of one connection
# string is a drift waiting to happen, and the one that goes stale is always the
# one nobody looks at.
#
# `override=False` (the default) means the root file wins where both define a
# key, and neither ever overrides a variable already set in the environment.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, "webapp", ".env"))


def preflight() -> None:
    """Fail loudly, and with the fix in the message, before doing any work.

    This job runs unattended on a Pi. Without this, a missing DATABASE_URL
    surfaces as `psycopg2.connect(None)` raising something about a missing
    host, and a missing FMP_API_KEY as every quote silently failing — both at
    18:00, in a log nobody has a reason to open. The cost of a misconfiguration
    is therefore not one failed run, it is however long it takes before
    somebody notices the scoreboard stopped moving.
    """
    missing = [k for k in ("DATABASE_URL", "FMP_API_KEY") if not os.getenv(k)]
    if not missing:
        return
    logger.error(
        "Missing required environment variable(s): %s.\n"
        "  Set them in %s (or, on a webapp-only checkout, in %s).\n"
        "  DATABASE_URL is the same connection string the web viewer uses.\n"
        "  FMP_API_KEY is needed for the price quotes and is NOT required by the\n"
        "  viewer, so a Pi set up for the viewer alone will not have it yet.",
        ", ".join(missing),
        os.path.join(PROJECT_ROOT, ".env"),
        os.path.join(PROJECT_ROOT, "webapp", ".env"),
    )
    raise SystemExit(2)

FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"

# ONE SYMBOL PER CALL, deliberately.
#
# `/stable/quote` does not batch: passing a comma-separated list returns HTTP
# 200 with an empty array — success-shaped, and it would write a whole week of
# nothing while logging no error at all. The real batch endpoints
# (`/stable/batch-quote`, `/stable/batch-quote-short`) answer 402 Restricted on
# the Starter plan this project runs on.
#
# So do not "optimise" this loop into a comma list. Forty single calls once a
# week is nothing against the plan's quota; a silently empty scoreboard is the
# one failure this job cannot afford, because nobody is watching it.


# Mirrors `verdict_ledger:` in specs/config.yaml, and is used only when that file
# is not present beside this one.
#
# This job is deployed by copying files onto a Pi rather than by checking the
# repo out there, so "the config file did not come along" is a likely accident
# rather than a hypothetical. Falling back keeps a one-file copy working; saying
# so at WARNING keeps it from being mistaken for the configured behaviour, which
# is the failure that would otherwise hide for months — a Pi silently
# benchmarking against a different symbol than the config says.
_DEFAULTS = {
    "enabled": True,
    "benchmark_symbol": "SPY",
    "snapshot_interval_days": 1,
    "track_for_days": 400,
}


def _config() -> dict:
    path = os.path.join(SRC_ROOT, "specs", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = (yaml.safe_load(fh) or {}).get("verdict_ledger", {}) or {}
        if cfg:
            return cfg
        logger.warning("No `verdict_ledger:` block in %s; using built-in defaults: %s",
                       path, _DEFAULTS)
    except FileNotFoundError:
        logger.warning(
            "%s not found; using built-in defaults: %s. Copy specs/config.yaml "
            "beside this script to control these from one place.", path, _DEFAULTS)
    except yaml.YAMLError as e:
        # A corrupt config is NOT quietly replaced with defaults: that would run
        # the job against settings nobody chose while the file that was supposed
        # to govern it sat there broken.
        logger.error("Could not parse %s (%s). Fix it or remove it to fall back "
                     "to defaults.", path, e)
        raise SystemExit(2)
    return dict(_DEFAULTS)


def fetch_quotes(symbols: list) -> dict:
    """Return {SYMBOL: price} for the given symbols, skipping any that fail.

    A symbol that no longer quotes — delisted, acquired, renamed — is dropped
    with a warning rather than raising. Over a year of tracking, some subjects
    WILL stop trading, and that must not stop the rest of the week's snapshot.
    """
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        raise ValueError("FMP_API_KEY not set")
    out = {}
    for symbol in symbols:
        try:
            resp = requests.get(
                FMP_QUOTE_URL,
                params={"symbol": symbol, "apikey": api_key},
                timeout=20,
            )
            resp.raise_for_status()
            for row in resp.json() or []:
                sym, price = row.get("symbol"), row.get("price")
                if sym and isinstance(price, (int, float)) and price > 0:
                    out[sym.upper()] = float(price)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Quote for {symbol} failed ({e}); skipping it.")
    missing = [s for s in symbols if s not in out]
    if missing:
        logger.warning(f"No quote for {len(missing)} symbol(s): {', '.join(missing[:10])}"
                       + (" ..." if len(missing) > 10 else ""))
    return out


def _due(cur, cfg: dict) -> tuple:
    """Rows needing a snapshot today: (verdict subjects, universe subjects).

    A verdict is due when it is still OPEN (exit_date is NULL) and has no
    snapshot within `snapshot_interval_days` — open verdicts are priced forever,
    with no age cutoff. A universe row is due when it is inside its tracking
    window and has no recent snapshot. Expressed against the LAST snapshot
    rather than against a schedule, so a job that misses a run catches up on its
    next one instead of silently leaving a gap.
    """
    interval = int(cfg.get("snapshot_interval_days", 1))
    track_for = int(cfg.get("track_for_days", 400))

    # Every OPEN verdict is tracked forever, regardless of how old it is or how
    # it entered the ledger (PIPELINE, ONDEMAND or mirrored MANUAL). The ledger
    # is meant to be a comprehensive picture of a portfolio's picks, and a
    # position still being held years after the verdict is exactly one that
    # must keep being priced — under-tracking it would quietly drop a held
    # position's price history (and Q10 would mark it at whatever the last
    # snapshot caught). The only thing that ends a verdict's snapshotting is
    # the position being SOLD (exit_date set), which settles the result.
    #
    # There is deliberately no verdict_age window here anymore. The old
    # per-source cutoff (400 days for PIPELINE, 2200 for everything else)
    # silently excluded old-but-still-open picks — a 2017 buy backfilled years
    # later fell outside both windows and was never snapshotted once, so it
    # never reached verdict_outcome and never appeared on the MST scoreboard.
    # Cost is not a reason to keep such a window: a quote on the FMP Starter
    # plan is free within the subscription, so a daily re-quote of every open
    # ticker is negligible even measured over years.
    cur.execute('''
        SELECT v.verdict_id, v.ticker, v.verdict_date, v.price, v.benchmark_price
          FROM verdict_log v
         WHERE v.price > 0
           -- A sold position is finished. Re-pricing it every week would keep
           -- crediting or blaming the pick for a market it was no longer in,
           -- and would overwrite a settled result with a hypothetical one.
           -- This is the ONLY thing that stops an open verdict being priced:
           -- age is irrelevant, because an open position is priced forever.
           AND v.exit_date IS NULL
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_outcome o
                  WHERE o.verdict_id = v.verdict_id
                    AND o.as_of > CURRENT_DATE - %s)
    ''', (interval,))
    verdicts = cur.fetchall()

    # Universe rows are the control group: names the screen surfaced, tracked
    # whether or not they ever reached a verdict or were bought. Without them
    # "did our filtering beat not filtering" has no denominator.
    cur.execute('''
        SELECT u.universe_id, u.ticker, u.screened_on, u.price
          FROM run_universe u
         WHERE u.price > 0
           AND u.screened_on > CURRENT_DATE - %s
           AND NOT EXISTS (
                 SELECT 1 FROM verdict_outcome o
                  WHERE o.universe_id = u.universe_id
                    AND o.as_of > CURRENT_DATE - %s)
    ''', (track_for, interval))
    universe = cur.fetchall()
    return verdicts, universe


def _pct(now: float, then: float):
    if not then:
        return None
    return round((now - then) / then * 100.0, 4)


def run_snapshot() -> dict:
    cfg = _config()
    if not cfg.get("enabled", False):
        logger.info("verdict_ledger.enabled is false in config.yaml; nothing to do.")
        return {"snapshots": 0}
    preflight()

    benchmark = str(cfg.get("benchmark_symbol", "SPY")).strip().upper()
    today = date.today()

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    verdicts, universe = _due(cur, cfg)

    if not verdicts and not universe:
        logger.info("No ledger subjects due for a snapshot. No API calls made.")
        cur.close()
        conn.close()
        return {"snapshots": 0}

    symbols = sorted({r[1] for r in verdicts} | {r[1] for r in universe} | {benchmark})
    logger.info(f"{len(verdicts)} verdict(s) and {len(universe)} universe row(s) due; "
                f"quoting {len(symbols)} symbol(s).")
    quotes = fetch_quotes(symbols)
    bench_now = quotes.get(benchmark)
    if bench_now is None:
        # Absolute returns are still worth recording, so the run continues with
        # excess return left NULL rather than being abandoned.
        logger.warning(f"No quote for the benchmark {benchmark}; excess returns "
                       f"will be NULL for this snapshot.")

    written = 0
    for parent, rows in (("verdict_id", verdicts), ("universe_id", universe)):
        for row in rows:
            subject_id, ticker, baseline_date, baseline_price = row[0], row[1], row[2], row[3]
            price_now = quotes.get(ticker)
            if price_now is None:
                continue
            total = _pct(price_now, float(baseline_price))
            # The benchmark baseline lives on the verdict row. Universe rows have
            # none — they exist to be compared against the verdicts of the SAME
            # run, and that comparison is like-for-like without an index. So
            # excess return is computed only where a baseline was actually
            # captured, rather than back-filled from whatever SPY costs today.
            bench_then = float(row[4]) if parent == "verdict_id" and row[4] else None
            excess = None
            if total is not None and bench_now and bench_then:
                bench_move = _pct(bench_now, bench_then)
                excess = round(total - bench_move, 4) if bench_move is not None else None

            cur.execute(f'''
                INSERT INTO verdict_outcome (
                    {parent}, ticker, as_of, age_days, price,
                    benchmark_symbol, benchmark_price, total_return_pct, excess_return_pct
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            ''', (subject_id, ticker, today, (today - baseline_date).days, price_now,
                  benchmark if bench_now else None, bench_now, total, excess))
            written += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    return {"snapshots": written, "quoted": len(quotes), "due": len(verdicts) + len(universe)}


if __name__ == "__main__":
    try:
        result = run_snapshot()
        logger.info(f"Ledger snapshot complete. {result}")
    except Exception:
        logger.exception("Ledger snapshot failed with an unhandled exception.")
        sys.exit(1)
