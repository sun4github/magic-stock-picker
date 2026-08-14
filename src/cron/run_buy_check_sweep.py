"""Weekly sweep: has any open buy case's triggers actually fired?

Scheduled on the machine that runs the agents (Windows Task Scheduler — see
tools/setup_weekly_jobs.cmd), NOT on the Raspberry Pi. This one uses ADK and
Gemini, unlike cron/run_outcome_snapshot.py which is pure arithmetic.

    python src/cron/run_buy_check_sweep.py            # normal weekly run
    python src/cron/run_buy_check_sweep.py --dry-run  # pre-filter only, no LLM, no alerts

THE PRE-FILTER IS THE POINT
---------------------------
Running the buy-check agent over every open watchlist name every week costs
about $0.08 a ticker to ask a question whose answer is "not yet" almost every
time. Twelve names is ~$50 a year for a weekly notification that says the same
thing every week — and a notification that always says the same thing is one the
reader stops opening, which is the real cost.

So a free arithmetic pass runs first, using the price the buy case itself wrote
down, and only tickers where the answer could plausibly have changed reach the
model:

  * price at or below the buy case's own price trigger  -> ask
  * price within `near_trigger_pct` of it               -> ask
  * the company has reported since the last sweep       -> ask
  * otherwise                                           -> skip, cost nothing

The earnings condition matters more than it looks. Most event triggers in a buy
case are written against reported figures, so a fresh report is exactly when
their answer can change — and it is the one moment a price-only filter would
miss completely.

Nothing here decides anything. A fired trigger is a prompt to go and read the
buy case, which is what the alert says.
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The agent stack lives in src/ and imports its siblings by bare name.
sys.path.insert(0, SRC_ROOT)

# main.run_buy_check writes its report to a RELATIVE "reports/" path, as every
# other command in this repo does — they are all run by hand from the project
# root. A scheduled task is not: Task Scheduler starts a job in System32 unless
# told otherwise, so the check would write to C:\Windows\System32\reports\ or
# fail on a read-only directory, and the sweep would then read back nothing and
# report WAIT for a ticker that had just fired.
#
# Fixed here rather than in the scheduler entry so it holds however the script is
# launched — by the task, by hand from another directory, or from an IDE.
os.chdir(PROJECT_ROOT)

logger = logging.getLogger("buy_check_sweep")


# The price trigger, as the buy case actually writes it. Two shapes are in the
# stored corpus already and both must parse:
#
#   ### Trigger 1 — Price
#   - **Condition:** Stock price drops to $430.75 or lower.
#
#   *   **Trigger 1 — Price.** *(mandatory, and always first)*
#       *   **Condition:** Share price drops below **$28.00 per share**.
#
# Anchored on "Trigger 1" plus the word Price rather than on a heading level or
# a dash character: the heading depth varies between runs, and the em dash is
# not reliably the same codepoint. Deliberately tolerant, because the fallback
# when parsing fails is to escalate to the model — never to silently skip a
# ticker whose trigger may have fired.
_TRIGGER1_RE = re.compile(r"trigger\s*1\b[^\n]{0,40}price", re.IGNORECASE)
_CONDITION_RE = re.compile(r"condition:?\*{0,2}\s*(.{0,200})", re.IGNORECASE | re.DOTALL)
_DOLLAR_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def parse_trigger_price(buy_case: str):
    """The price threshold from the buy case's mandatory Trigger 1, or None.

    None means "could not read it", and every caller treats that as a reason to
    escalate rather than to skip. A watchlist name silently dropped because its
    formatting changed is the failure that would quietly empty this job of value.
    """
    if not buy_case:
        return None
    m = _TRIGGER1_RE.search(buy_case)
    if not m:
        return None
    cond = _CONDITION_RE.search(buy_case[m.end():m.end() + 800])
    if not cond:
        return None
    money = _DOLLAR_RE.search(cond.group(1))
    if not money:
        return None
    try:
        return float(money.group(1).replace(",", ""))
    except ValueError:
        return None


def _open_buy_cases(cur):
    """Tickers with a stored buy case whose LATEST verdict is still Watch.

    A name the pipeline has since moved to Buy or Avoid is dropped: its triggers
    are testing a case the system has already left behind, and paying an agent to
    evaluate them would be answering a question nobody is asking any more.
    """
    cur.execute('''
        SELECT DISTINCT ON (a.ticker)
               a.ticker, a.raw_content, a.run_id::text, a.created_at::date
          FROM agent_outputs a
         WHERE a.agent_type = 'BUY_CASE'
         ORDER BY a.ticker, a.created_at DESC
    ''')
    cases = cur.fetchall()
    if not cases:
        return []
    cur.execute('''
        SELECT DISTINCT ON (ticker) ticker, verdict
          FROM final_reports ORDER BY ticker, created_at DESC
    ''')
    latest = dict(cur.fetchall())
    out, stale = [], []
    for ticker, body, run_id, written_on in cases:
        if (latest.get(ticker) or "").upper() == "WATCH":
            out.append({"ticker": ticker, "buy_case": body, "run_id": run_id,
                        "written_on": written_on})
        else:
            stale.append(f"{ticker}={latest.get(ticker) or '?'}")
    if stale:
        logger.info(f"Skipping {len(stale)} buy case(s) whose latest verdict has moved "
                    f"off Watch: {', '.join(stale)}")
    return out


def _state(cur) -> dict:
    cur.execute("SELECT ticker, last_swept_on, last_earnings_seen, last_recommendation, "
                "last_alerted_on FROM buy_check_sweep_state")
    return {r[0]: {"last_swept_on": r[1], "last_earnings_seen": r[2],
                   "last_recommendation": r[3], "last_alerted_on": r[4]}
            for r in cur.fetchall()}


def _latest_earnings_date(ticker: str, fmp_earnings_calendar):
    """Most recent REPORTED earnings date for a ticker, or None."""
    try:
        data = json.loads(fmp_earnings_calendar(ticker))
    except Exception:
        return None
    dates = []
    for row in (data.get("recent") or []):
        raw = row.get("date") if isinstance(row, dict) else None
        if raw:
            try:
                dates.append(datetime.strptime(str(raw)[:10], "%Y-%m-%d").date())
            except ValueError:
                continue
    return max(dates) if dates else None


def _decide(case, price, trigger, prior, cfg, latest_earnings):
    """(escalate: bool, reason: str, urgency: float) for one ticker.

    `urgency` is how far below the trigger the price sits as a fraction — used
    only to order escalations when more of them arrive than the sweep's ceiling
    allows, so the ceiling drops the least urgent rather than an arbitrary tail.
    """
    near_pct = float(cfg.get("near_trigger_pct", 5.0))

    if trigger is None:
        # Unparseable trigger: ask the model. Cheaper to spend $0.08 than to
        # silently stop watching a name because its formatting drifted.
        return True, "could not read the price trigger from the buy case", 0.0

    gap_pct = (price - trigger) / trigger * 100.0
    if price <= trigger:
        return True, (f"price ${price:.2f} is at or below the ${trigger:.2f} trigger"), -gap_pct
    if gap_pct <= near_pct:
        return True, (f"price ${price:.2f} is within {gap_pct:.1f}% of the "
                      f"${trigger:.2f} trigger"), -gap_pct

    if cfg.get("escalate_on_new_earnings", True) and latest_earnings:
        seen = (prior or {}).get("last_earnings_seen")
        last_swept = (prior or {}).get("last_swept_on")
        # On a ticker never swept before, `seen` is NULL and every past report
        # would look new. Baseline against the buy case's own date instead: a
        # report published after the case was written genuinely is untested
        # against it, which is the thing worth escalating on.
        baseline = seen or last_swept or case["written_on"]
        if baseline and latest_earnings > baseline:
            return True, (f"reported on {latest_earnings}, after the buy case was "
                          f"last tested"), 0.0

    return False, f"price ${price:.2f} is {gap_pct:.1f}% above the ${trigger:.2f} trigger", gap_pct


def run_sweep(dry_run: bool = False) -> dict:
    import yaml
    import psycopg2
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    with open(os.path.join(SRC_ROOT, "specs", "config.yaml"), "r", encoding="utf-8") as fh:
        cfg = (yaml.safe_load(fh) or {}).get("buy_check_sweep", {}) or {}
    if not cfg.get("enabled", False):
        logger.info("buy_check_sweep.enabled is false in config.yaml; nothing to do.")
        return {"escalated": 0}

    # Imported here rather than at module scope so --dry-run and --help do not
    # pay the multi-second cost of loading ADK and the Gemini SDK.
    import main
    from mcp_server import fmp_earnings_calendar

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cases = _open_buy_cases(cur)
    if not cases:
        logger.info("No open buy cases on Watch names. Nothing to sweep.")
        cur.close()
        conn.close()
        return {"escalated": 0}

    prior_state = _state(cur)
    today = date.today()
    logger.info(f"Sweeping {len(cases)} open buy case(s).")

    candidates, skipped = [], 0
    for case in cases:
        ticker = case["ticker"]
        try:
            quote = json.loads(main.fmp_price_snapshot(ticker))
            price = float(quote.get("price") or 0)
        except Exception as e:
            logger.error(f"[{ticker}] Could not fetch a price ({e}); skipping.")
            continue
        if not price:
            logger.warning(f"[{ticker}] No live price; skipping.")
            continue

        trigger = parse_trigger_price(case["buy_case"])
        prior = prior_state.get(ticker)
        latest_earnings = (_latest_earnings_date(ticker, fmp_earnings_calendar)
                           if cfg.get("escalate_on_new_earnings", True) else None)
        escalate, reason, urgency = _decide(case, price, trigger, prior, cfg, latest_earnings)

        cur.execute('''
            INSERT INTO buy_check_sweep_state
                   (ticker, last_swept_on, last_price, trigger_price, last_earnings_seen, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (ticker) DO UPDATE SET
                last_swept_on = EXCLUDED.last_swept_on,
                last_price = EXCLUDED.last_price,
                trigger_price = EXCLUDED.trigger_price,
                last_earnings_seen = COALESCE(EXCLUDED.last_earnings_seen,
                                              buy_check_sweep_state.last_earnings_seen),
                updated_at = CURRENT_TIMESTAMP
        ''', (ticker, today, price, trigger, latest_earnings))

        if escalate:
            candidates.append({**case, "price": price, "trigger": trigger,
                               "reason": reason, "urgency": urgency})
            logger.info(f"[{ticker}] ESCALATE — {reason}")
        else:
            skipped += 1
            logger.info(f"[{ticker}] skip — {reason}")

    # A dry run must leave NO trace. Committing here would stamp
    # `last_earnings_seen`, and the next real sweep would then see those reports
    # as already tested and decline to escalate — so a harmless-looking preview
    # would silently suppress the very alert it was run to predict.
    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    ceiling = int(cfg.get("max_agent_checks_per_sweep", 8))
    candidates.sort(key=lambda c: c["urgency"], reverse=True)
    if len(candidates) > ceiling:
        # A market-wide drop would otherwise put every watchlist name over the
        # line at once and turn one scheduled job into an unbounded bill.
        logger.warning(f"{len(candidates)} tickers escalated but the per-sweep ceiling is "
                       f"{ceiling}; checking the {ceiling} closest to their triggers. The "
                       f"rest will be picked up next sweep: "
                       f"{', '.join(c['ticker'] for c in candidates[ceiling:])}")
        candidates = candidates[:ceiling]

    if dry_run:
        logger.info(f"DRY RUN — would run the buy-check agent on "
                    f"{len(candidates)} ticker(s): "
                    f"{', '.join(c['ticker'] for c in candidates) or 'none'}. "
                    f"No LLM calls made, no alerts sent.")
        cur.close()
        conn.close()
        return {"swept": len(cases), "escalated": len(candidates), "skipped": skipped,
                "checked": 0, "alerts": 0, "dry_run": True}

    alerts = 0
    for c in candidates:
        ticker = c["ticker"]
        try:
            # The existing on-demand command, unchanged: one code path for the
            # scheduled sweep and the manual `--buy-check`, so a fix to either
            # is a fix to both. It stores the check against the buy case's own
            # run and returns the recommendation, so nothing here has to read the
            # report file back off disk to learn the answer it just paid for.
            recommendation = main.run_buy_check(
                ticker, run_id=c["run_id"], origin="sweep",
                escalation_reason=c["reason"],
            )
            if not recommendation:
                # The check aborted somewhere inside — it has already logged why.
                # Treated as "no answer", not as WAIT: recording a WAIT the agent
                # never gave would put a false negative into the stored history.
                logger.warning(f"[{ticker}] Buy check produced no recommendation; "
                               f"leaving it for next sweep.")
                continue
        except Exception as e:
            logger.error(f"[{ticker}] Buy check failed ({e}); leaving it for next sweep.")
            continue

        prior = prior_state.get(ticker) or {}
        already = (cfg.get("suppress_repeat_alerts", True)
                   and prior.get("last_recommendation") == "BUY"
                   and recommendation == "BUY")
        should_alert = recommendation == "BUY" and not already

        cur.execute('''
            UPDATE buy_check_sweep_state
               SET last_recommendation = %s,
                   last_alerted_on = CASE WHEN %s THEN %s ELSE last_alerted_on END,
                   updated_at = CURRENT_TIMESTAMP
             WHERE ticker = %s
        ''', (recommendation, should_alert, today, ticker))

        if should_alert:
            import notifications
            notifications.send_buy_trigger_alert(
                ticker=ticker, company_name=ticker, price=c["price"],
                trigger_price=c["trigger"] or 0.0, reason=c["reason"],
                detail=f"Buy case from run {c['run_id']}, written {c['written_on']}.",
            )
            alerts += 1
            logger.info(f"[{ticker}] BUY — alert sent.")
        elif already:
            logger.info(f"[{ticker}] BUY, but it already read BUY at the last sweep; "
                        f"not re-alerting.")
        else:
            logger.info(f"[{ticker}] {recommendation} — no alert.")

    conn.commit()
    cur.close()
    conn.close()
    return {"swept": len(cases), "escalated": len(candidates), "skipped": skipped,
            "checked": len(candidates), "alerts": alerts}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the free pre-filter and report who WOULD be checked. "
                             "No LLM calls, no alerts, no cost.")
    args = parser.parse_args()

    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(log_dir, "buy_check_sweep.log"))],
    )
    try:
        logger.info(f"Buy-check sweep complete. {run_sweep(dry_run=args.dry_run)}")
    except Exception:
        logger.exception("Buy-check sweep failed with an unhandled exception.")
        sys.exit(1)


if __name__ == "__main__":
    main_cli()
