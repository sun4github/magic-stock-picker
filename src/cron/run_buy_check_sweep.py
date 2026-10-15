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

THE SECOND PASS: BUYS TOLD TO WAIT
----------------------------------
A Buy has no buy case, so the pass above never sees one. A Buy whose entry gate
(momentum.py) said "wait for a pullback" or "wait for the trend to turn" is still
a name someone is waiting to buy. This pass re-reads its chart weekly, for free,
and alerts ONCE when the gate clears. Escalated Watch names get the same chart
reading in their alert: a Watch that reaches its price trigger usually got there
by falling, and the reader should know whether it is still falling.

THE WEEKLY SUMMARY
------------------
Sent every sweep, by email and Pushover, whether anything fired or not, and also
when the sweep fails. A quiet week used to look exactly like a broken job.

    python src/cron/run_buy_check_sweep.py --test-summary   # dry run + send it, marked [TEST]
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
               a.ticker, a.raw_content, a.run_id::text, a.created_at::date, a.created_at
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
    for ticker, body, run_id, written_on, written_at in cases:
        if (latest.get(ticker) or "").upper() == "WATCH":
            out.append({"ticker": ticker, "buy_case": body, "run_id": run_id,
                        "written_on": written_on, "written_at": written_at})
        else:
            stale.append(f"{ticker}={latest.get(ticker) or '?'}")
    if stale:
        logger.info(f"Skipping {len(stale)} buy case(s) whose latest verdict has moved "
                    f"off Watch: {', '.join(stale)}")
    return out


def _state(cur) -> dict:
    cur.execute("SELECT ticker, last_swept_on, last_earnings_seen, last_recommendation, "
                "last_alerted_on, unreadable_streak, unreadable_alerted_on "
                "FROM buy_check_sweep_state")
    return {r[0]: {"last_swept_on": r[1], "last_earnings_seen": r[2],
                   "last_recommendation": r[3], "last_alerted_on": r[4],
                   "unreadable_streak": r[5] or 0, "unreadable_alerted_on": r[6]}
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


def next_unreadable_streak(trigger, prior: dict) -> int:
    """How many sweeps in a row this buy case's price trigger has been unreadable.

    Reset to zero the moment one parses, rather than decayed: the question the
    counter answers is "has this been unreadable CONTINUOUSLY", and a buy case
    regenerated with a dollar figure has settled it. Without the reset, a name
    briefly unreadable would carry the count for ever and eventually warn about a
    document that has been fine for months.

    A module-level function rather than an expression inline in the sweep so the
    tests exercise the rule the job actually applies, instead of a copy of it
    that would keep passing after the real one broke.
    """
    if trigger is not None:
        return 0
    return (prior or {}).get("unreadable_streak", 0) + 1


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


# --- The second pass: Buy verdicts whose entry gate said to wait ---------------
#
# A Buy has no buy case, so the first pass never sees one. But a Buy whose entry
# gate said "wait for a pullback" or "wait for the trend to turn" (momentum.py)
# is exactly a name someone is waiting to buy, and the wait has an answer that
# changes week to week. This pass re-reads the chart — free, no model — and says
# once when the wait is over.
WAITING_CALLS = ("WAIT_PULLBACK", "WAIT_TREND", "CHECK_CHART")


def _waiting_buys(cur, max_age_days: int, today: date):
    """The latest analysis verdict per ticker, kept when it is a Buy that was told
    to wait and is young enough that the business verdict still stands.

    Latest across stages, so a refined (POST_CRITIC) call supersedes the one it
    reviewed. MANUAL rows are never this system's judgement and are excluded.
    """
    cur.execute('''
        SELECT DISTINCT ON (ticker)
               verdict_id::text, ticker, verdict, entry_call, verdict_date
          FROM verdict_log
         WHERE source IN ('PIPELINE', 'ONDEMAND')
         ORDER BY ticker, created_at DESC
    ''')
    out = []
    for verdict_id, ticker, verdict, call, vdate in cur.fetchall():
        if (verdict or "").upper() != "BUY" or call not in WAITING_CALLS:
            continue
        age = (today - vdate).days if vdate else None
        if age is not None and age > max_age_days:
            logger.info(f"[{ticker}] Buy from {vdate} told to wait is {age} days old "
                        f"(limit {max_age_days}); no longer watched. Re-run the "
                        f"analysis before acting on it.")
            continue
        out.append({"verdict_id": verdict_id, "ticker": ticker, "entry_call": call,
                    "verdict_date": vdate})
    return out


def _gate_state(cur) -> dict:
    cur.execute("SELECT verdict_id::text, cleared_on FROM entry_gate_watch_state")
    return {r[0]: {"cleared_on": r[1]} for r in cur.fetchall()}


def gate_should_alert(state: str, prior: dict) -> bool:
    """Announce a clearance once per verdict: CLEAR now, and not announced before.

    UNKNOWN is never a clearance — a chart that could not be read is not a clear
    one. Module-level so the tests exercise the rule the sweep applies.
    """
    return state == "CLEAR" and not (prior or {}).get("cleared_on")


def _chart(ticker: str, quote: dict, main, momentum, mcfg) -> dict:
    """The entry gate's reading for one ticker from a quote already fetched. {} on failure."""
    try:
        closes = main.fmp_price_history(ticker, momentum.sessions_needed(mcfg))
    except Exception as e:
        logger.warning(f"[{ticker}] Price history failed ({e}); reading the chart "
                       f"without the 200-day slope.")
        closes = []
    try:
        return momentum.momentum_state(quote, closes, mcfg)
    except Exception as e:
        logger.warning(f"[{ticker}] Could not read the chart ({e}).")
        return {}


def _chart_phrase(m: dict) -> str:
    """'falling knife (25.0% below ...)' — the state and why, for a summary line."""
    if not m:
        return "not read"
    label = {"CLEAR": "clear", "EXTENDED": "extended", "FALLING_KNIFE": "falling knife",
             "UNKNOWN": "could not be read"}.get(m.get("state"), str(m.get("state")))
    why = "; ".join(m.get("reasons") or [])
    return f"{label} ({why})" if why else label


# --- Buy cases the agent keeps calling out of date ---------------------------
#
# The buy-check prompt ends with "a note on the buy case itself": when a trigger
# is unusable or overtaken by events, the agent is told to say so and to
# recommend `python buy_case.py TICKER`. Nothing acted on that. FICO was checked
# six weeks running, at ~$0.08 each, after the agent had called its $950 trigger
# obsolete — the regulator had broken the monopoly the target was priced on.
#
# The signal is the literal command, not the word "regenerate": the agent also
# writes "no buy case regeneration is required", and that must not count.
def stale_case_streak(ticker: str, checks_newest_first: list) -> int:
    """How many of the most recent buy checks IN A ROW recommend regenerating."""
    needle = f"buy_case.py {ticker}".lower()
    n = 0
    for text in checks_newest_first:
        if needle not in (text or "").lower():
            break
        n += 1
    return n


def _stale_case_flags(cur, cases: list, flag_after: int) -> list:
    """'TICKER: ...' lines for buy cases the agent has called out of date
    `flag_after` checks in a row. Only checks of the CURRENT buy case count: those
    on its run AND written after it. The run alone is not enough — `buy_case.py`
    regenerates a case onto the SAME run, so the old case's checks sit beside the
    new one and would keep flagging a document that has just been fixed."""
    if not cases or flag_after <= 0:
        return []
    cur.execute('''
        SELECT ticker, run_id::text, created_at, raw_content
          FROM agent_outputs
         WHERE agent_type = 'BUY_CHECK' AND run_id::text = ANY(%s)
         ORDER BY created_at DESC
    ''', ([c["run_id"] for c in cases],))
    by_case = {}
    written = {(c["ticker"], c["run_id"]): c.get("written_at") for c in cases}
    for ticker, run_id, created_at, text in cur.fetchall():
        since = written.get((ticker, run_id))
        if since is not None and created_at <= since:
            continue
        by_case.setdefault((ticker, run_id), []).append(text)
    out = []
    for c in cases:
        streak = stale_case_streak(c["ticker"], by_case.get((c["ticker"], c["run_id"]), []))
        if streak >= flag_after:
            out.append(
                f"{c['ticker']}: the buy-check agent has said this buy case is out of "
                f"date in its last {streak} checks. Regenerate it (python buy_case.py "
                f"{c['ticker']}), or re-run the analysis (python main.py {c['ticker']}) "
                f"if the business itself has changed.")
    return out


# --- The weekly summary -------------------------------------------------------
SUMMARY_TITLE = "Magic Stock Picker - Weekly Buy Sweep"


def format_summary(result: dict, today: date, test: bool = False):
    """(title, email body, push body, pushover priority) for one sweep's result.

    Pure — no I/O — so the tests can pin what the reader is told. The email is the
    full record; the push is a few lines that fit Pushover's limit.
    """
    stamp = today.strftime("%a %d %b %Y")
    prefix = "[TEST] " if test else ""
    dry = result.get("dry_run")

    if result.get("failed"):
        title = f"{prefix}{SUMMARY_TITLE}, {today}: FAILED"
        body = "\n".join([
            f"The weekly buy sweep for {stamp} failed before it finished.",
            "",
            f"Error: {result['failed']}",
            "",
            "Nothing was alerted this week, so a trigger may have fired unseen. "
            "The full traceback is in logs/buy_check_sweep.log. To re-run it by hand:",
            "    python src/cron/run_buy_check_sweep.py",
        ])
        return title, body, body, 0

    watch = result.get("watch") or []
    waiting = result.get("waiting") or []
    buys = [w for w in watch if w.get("recommendation") == "BUY" and w.get("alerted")]
    cleared = [w for w in waiting if w.get("alerted")]
    problems = result.get("problems") or []

    actions = len(buys) + len(cleared)
    if actions:
        parts = ([f"{len(buys)} BUY signal{'s' if len(buys) != 1 else ''}"] if buys else []) + \
                ([f"{len(cleared)} entry gate{'s' if len(cleared) != 1 else ''} cleared"]
                 if cleared else [])
        headline = ", ".join(parts)
    else:
        headline = "nothing to act on"
    title = f"{prefix}{SUMMARY_TITLE}, {today}: {headline}"

    lines = [f"Weekly buy sweep for {stamp}."]
    if test:
        lines += ["", "TEST: this is a preview built from a dry run. No buy-check agent "
                      "was run, nothing was recorded, and no BUY alert was sent."]
    elif dry:
        lines += ["", "DRY RUN: no buy-check agent was run and nothing was recorded."]

    lines += ["", "ACTION"]
    if not actions:
        lines.append("- Nothing to act on this week.")
    for w in buys:
        lines.append(f"- BUY signal: {w['ticker']} at ${w['price']:.2f}"
                     + (f" (trigger ${w['trigger']:.2f})" if w.get("trigger") else "")
                     + f". Chart: {_chart_phrase(w.get('chart'))}."
                     + f" Read reports/{w['ticker']}_Buy_Check.md")
    for w in cleared:
        lines.append(f"- Entry gate cleared: {w['ticker']} at ${w['price']:.2f} "
                     f"(BUY from {w['verdict_date']}).")

    lines += ["", f"WATCHLIST ({len(watch)} Watch name{'s' if len(watch) != 1 else ''} "
                  f"with a buy case)"]
    escalated = [w for w in watch if w.get("escalated")]
    if escalated:
        lines.append(f"Escalated to the buy-check agent ({len(escalated)}):")
        for w in escalated:
            trig = f" vs trigger ${w['trigger']:.2f}" if w.get("trigger") else ""
            gap = (f" ({w['gap_pct']:+.1f}%)" if w.get("gap_pct") is not None else "")
            answer = (w.get("recommendation") or
                      ("not run (dry run)" if dry else
                       "not checked this week (over the per-sweep ceiling)" if w.get("dropped")
                       else "no answer"))
            lines.append(f"- {w['ticker']} ${w['price']:.2f}{trig}{gap}: {answer}. "
                         f"Why: {w['reason']}. Chart: {_chart_phrase(w.get('chart'))}.")
    else:
        lines.append("No name was close enough to its trigger, or had reported, to "
                     "need the agent this week.")
    near = sorted((w for w in watch if not w.get("escalated") and w.get("gap_pct") is not None),
                  key=lambda w: w["gap_pct"])[:5]
    if near:
        band = result.get("near_trigger_pct", 5.0)
        lines.append(f"Next closest (more than {band:g}% above their trigger, so not sent "
                     f"to the agent this week):")
        for w in near:
            lines.append(f"- {w['ticker']} ${w['price']:.2f}, {w['gap_pct']:.1f}% above "
                         f"${w['trigger']:.2f}")
    if watch:
        lines.append(f"Checked by the agent: {result.get('checked', 0)} "
                     f"(about ${0.08 * result.get('checked', 0):.2f}).")

    lines += ["", f"WAITING BUYS ({len(waiting)} Buy verdict"
                  f"{'s' if len(waiting) != 1 else ''} the entry gate said to wait on)"]
    if not waiting:
        lines.append("- None.")
    for w in waiting:
        told = w["entry_call"].replace("_", " ").lower()
        if w.get("chart"):
            m = w["chart"]
            now = _chart_phrase(m)
            target = (f"; clears at or below ${m['clear_below']:,.2f}" if m.get("clear_below")
                      else f"; clears at or above ${m['clear_above']:,.2f}" if m.get("clear_above")
                      else "")
            done = (f" Clearance announced on {w['cleared_on']}." if w.get("cleared_on")
                    and not w.get("alerted") else "")
            lines.append(f"- {w['ticker']} BUY from {w['verdict_date']}, told {told}. "
                         f"Now ${w['price']:.2f}, chart {now}{target}.{done}")
        else:
            lines.append(f"- {w['ticker']} BUY from {w['verdict_date']}, told {told}. "
                         f"Chart could not be read this week.")

    if problems:
        lines += ["", "PROBLEMS"] + [f"- {p}" for p in problems]
    tidy = result.get("tidy") or []
    if tidy:
        # Standing chores, not failures: listed every week until fixed, but they do
        # not raise the push priority, or a single stale document would make every
        # summary buzz until it is regenerated.
        lines += ["", "TO TIDY UP"] + [f"- {t}" for t in tidy]

    body = "\n".join(lines)
    push = "\n".join(
        [f"Action: {headline}."]
        + [f"BUY {w['ticker']} ${w['price']:.2f}" for w in buys]
        + [f"Entry cleared {w['ticker']} ${w['price']:.2f}" for w in cleared]
        + [f"Watchlist: {len(watch)} swept, {len(escalated)} escalated, "
           f"{result.get('checked', 0)} checked.",
           f"Waiting buys: {len(waiting)}.",
           f"Problems: {len(problems) or 'none'}."]
        + ([f"To tidy up: {len(tidy)}."] if tidy else [])
        + [
           "Full summary by email."])
    return title, body, push, (0 if actions or problems else -1)


def run_sweep(dry_run: bool = False) -> dict:
    import yaml
    import psycopg2
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    with open(os.path.join(SRC_ROOT, "specs", "config.yaml"), "r", encoding="utf-8") as fh:
        full_cfg = yaml.safe_load(fh) or {}
    cfg = full_cfg.get("buy_check_sweep", {}) or {}
    mcfg = full_cfg.get("momentum_gate", {}) or {}
    if not cfg.get("enabled", False):
        logger.info("buy_check_sweep.enabled is false in config.yaml; nothing to do.")
        return {"disabled": True, "escalated": 0}

    # Imported here rather than at module scope so --help does not pay the
    # multi-second cost of loading ADK and the Gemini SDK.
    import main
    import momentum
    from mcp_server import fmp_earnings_calendar

    result = {"dry_run": dry_run, "near_trigger_pct": float(cfg.get("near_trigger_pct", 5.0)),
              "watch": [], "waiting": [], "problems": [], "tidy": [],
              "swept": 0, "escalated": 0, "skipped": 0, "checked": 0, "alerts": 0}
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    today = date.today()

    # --- Pass 1: Watch names with a buy case -----------------------------------
    cases = _open_buy_cases(cur)
    prior_state = _state(cur)
    logger.info(f"Sweeping {len(cases)} open buy case(s).")
    result["swept"] = len(cases)

    candidates = []
    for case in cases:
        ticker = case["ticker"]
        try:
            quote = json.loads(main.fmp_price_snapshot(ticker))
            price = float(quote.get("price") or 0)
        except Exception as e:
            logger.error(f"[{ticker}] Could not fetch a price ({e}); skipping.")
            result["problems"].append(f"{ticker}: price fetch failed ({e}); not checked.")
            continue
        if not price:
            logger.warning(f"[{ticker}] No live price; skipping.")
            result["problems"].append(f"{ticker}: no live price; not checked.")
            continue

        trigger = parse_trigger_price(case["buy_case"])
        prior = prior_state.get(ticker)
        latest_earnings = (_latest_earnings_date(ticker, fmp_earnings_calendar)
                           if cfg.get("escalate_on_new_earnings", True) else None)
        escalate, reason, urgency = _decide(case, price, trigger, prior, cfg, latest_earnings)

        streak = next_unreadable_streak(trigger, prior)
        if trigger is None:
            result["tidy"].append(
                f"{ticker}: price trigger unreadable ({streak} sweep(s) in a row); "
                f"regenerate with python buy_case.py {ticker}")

        cur.execute('''
            INSERT INTO buy_check_sweep_state
                   (ticker, last_swept_on, last_price, trigger_price, last_earnings_seen,
                    unreadable_streak, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (ticker) DO UPDATE SET
                last_swept_on = EXCLUDED.last_swept_on,
                last_price = EXCLUDED.last_price,
                trigger_price = EXCLUDED.trigger_price,
                last_earnings_seen = COALESCE(EXCLUDED.last_earnings_seen,
                                              buy_check_sweep_state.last_earnings_seen),
                unreadable_streak = EXCLUDED.unreadable_streak,
                -- Clearing the alert stamp when the streak resets is what makes
                -- the warning repeatable for a LATER episode without making it
                -- weekly within one.
                unreadable_alerted_on = CASE WHEN EXCLUDED.unreadable_streak = 0
                                             THEN NULL
                                             ELSE buy_check_sweep_state.unreadable_alerted_on END,
                updated_at = CURRENT_TIMESTAMP
        ''', (ticker, today, price, trigger, latest_earnings, streak))

        # Alerted on the sweep the threshold is first crossed, and not again
        # until the streak has been broken and rebuilt.
        warn_after = int(cfg.get("warn_after_unreadable_sweeps", 3))
        if (streak >= warn_after and not (prior or {}).get("unreadable_alerted_on")
                and not dry_run):
            logger.warning(
                f"[{ticker}] Price trigger has been unreadable for {streak} sweeps. "
                f"It is escalated to the model every week as a result, at ~$0.08 a "
                f"time. Regenerate the buy case against a live quote: "
                f"python buy_case.py {ticker}")
            try:
                import notifications
                notifications.send_unreadable_trigger_alert(ticker, streak)
                cur.execute("UPDATE buy_check_sweep_state SET unreadable_alerted_on = %s "
                            "WHERE ticker = %s", (today, ticker))
            except Exception as e:
                # Never fail the sweep over a chore reminder.
                logger.warning(f"[{ticker}] Could not send the stale-buy-case alert ({e}).")

        row = {"ticker": ticker, "price": price, "trigger": trigger,
               "gap_pct": ((price - trigger) / trigger * 100.0) if trigger else None,
               "escalated": escalate, "reason": reason, "recommendation": None,
               "chart": None, "alerted": False}
        result["watch"].append(row)
        if escalate:
            # The chart, for escalated names only: these are the ones a reader may
            # act on this week, and a Watch reaching its price trigger usually got
            # there by falling — the reader should know whether it is still falling.
            row["chart"] = _chart(ticker, quote, main, momentum, mcfg)
            candidates.append({**case, "price": price, "trigger": trigger,
                               "reason": reason, "urgency": urgency, "row": row})
            logger.info(f"[{ticker}] ESCALATE — {reason}")
        else:
            result["skipped"] += 1
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
    result["escalated"] = len(candidates)
    if len(candidates) > ceiling:
        # A market-wide drop would otherwise put every watchlist name over the
        # line at once and turn one scheduled job into an unbounded bill.
        dropped = candidates[ceiling:]
        logger.warning(f"{len(candidates)} tickers escalated but the per-sweep ceiling is "
                       f"{ceiling}; checking the {ceiling} closest to their triggers. The "
                       f"rest will be picked up next sweep: "
                       f"{', '.join(c['ticker'] for c in dropped)}")
        result["problems"].append(
            f"{len(candidates)} names escalated, over the per-sweep ceiling of {ceiling}; "
            f"left for next week: {', '.join(c['ticker'] for c in dropped)}")
        for c in dropped:
            c["row"]["dropped"] = True
        candidates = candidates[:ceiling]

    if dry_run:
        logger.info(f"DRY RUN — would run the buy-check agent on "
                    f"{len(candidates)} ticker(s): "
                    f"{', '.join(c['ticker'] for c in candidates) or 'none'}. "
                    f"No LLM calls made, no alerts sent.")
    else:
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
                    result["problems"].append(f"{ticker}: buy check gave no answer.")
                    continue
            except Exception as e:
                logger.error(f"[{ticker}] Buy check failed ({e}); leaving it for next sweep.")
                result["problems"].append(f"{ticker}: buy check failed ({e}).")
                continue
            result["checked"] += 1
            c["row"]["recommendation"] = recommendation

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
                    detail=(f"Buy case from run {c['run_id']}, written {c['written_on']}.\n"
                            f"Chart: {_chart_phrase(c['row'].get('chart'))}."),
                )
                c["row"]["alerted"] = True
                result["alerts"] += 1
                logger.info(f"[{ticker}] BUY — alert sent.")
            elif already:
                logger.info(f"[{ticker}] BUY, but it already read BUY at the last sweep; "
                            f"not re-alerting.")
            else:
                logger.info(f"[{ticker}] {recommendation} — no alert.")
        conn.commit()

    # Read-only, after this week's checks are stored, so they count.
    try:
        result["tidy"] += _stale_case_flags(cur, cases,
                                            int(cfg.get("flag_stale_case_after_checks", 2)))
    except Exception as e:
        logger.warning(f"Could not read past buy checks for stale cases ({e}).")

    # --- Pass 2: Buy verdicts the entry gate said to wait on --------------------
    # Free: a quote and a price history per name, no model. Runs in a dry run too
    # (it costs nothing), but records and alerts nothing there.
    if mcfg.get("enabled", False):
        waiting = _waiting_buys(cur, int(cfg.get("entry_gate_max_wait_days", 90)), today)
        gate_prior = _gate_state(cur)
        logger.info(f"Entry gate: {len(waiting)} Buy verdict(s) told to wait.")
        for w in waiting:
            ticker = w["ticker"]
            prior = gate_prior.get(w["verdict_id"]) or {}
            row = {**w, "price": None, "chart": None, "alerted": False,
                   "cleared_on": prior.get("cleared_on")}
            result["waiting"].append(row)
            try:
                quote = json.loads(main.fmp_price_snapshot(ticker))
                row["price"] = float(quote.get("price") or 0) or None
            except Exception as e:
                quote = {}
                logger.error(f"[{ticker}] Could not fetch a price ({e}).")
            if not row["price"]:
                result["problems"].append(f"{ticker}: no live price for the entry gate.")
                continue
            m = _chart(ticker, quote, main, momentum, mcfg)
            row["chart"] = m
            state = m.get("state") or "UNKNOWN"
            alert = gate_should_alert(state, prior)
            logger.info(f"[{ticker}] Entry gate: {state}"
                        + (" — CLEARED, alerting." if alert and not dry_run else ""))
            if dry_run:
                continue
            cur.execute('''
                INSERT INTO entry_gate_watch_state
                       (verdict_id, ticker, entry_call, last_checked_on, last_gate,
                        last_price, cleared_on, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (verdict_id) DO UPDATE SET
                    last_checked_on = EXCLUDED.last_checked_on,
                    last_gate = EXCLUDED.last_gate,
                    last_price = EXCLUDED.last_price,
                    cleared_on = COALESCE(entry_gate_watch_state.cleared_on,
                                          EXCLUDED.cleared_on),
                    updated_at = CURRENT_TIMESTAMP
            ''', (w["verdict_id"], ticker, w["entry_call"], today, state, row["price"],
                  today if alert else None))
            conn.commit()
            if alert:
                try:
                    import notifications
                    notifications.send_entry_cleared_alert(
                        ticker, row["price"], w["verdict_date"], w["entry_call"],
                        _chart_phrase(m))
                    row["alerted"] = True
                    result["alerts"] += 1
                except Exception as e:
                    logger.warning(f"[{ticker}] Could not send the entry-cleared alert ({e}).")

    cur.close()
    conn.close()
    return result


def send_summary(result: dict, test: bool = False) -> None:
    """Send the weekly summary. Never raises: the sweep's work is already done."""
    try:
        import notifications
        title, body, push, priority = format_summary(result, date.today(), test=test)
        logger.info(f"Summary: {title}")
        sent = notifications.send_sweep_summary(title, body, push, priority)
        logger.info(f"Summary delivered: {sent}")
        if not any(sent.values()):
            logger.error("The weekly summary reached NO channel. Check the Pushover "
                         "and Plunk settings in .env.")
    except Exception:
        logger.exception("Could not send the weekly summary.")


def _brief(result: dict) -> dict:
    """The counters for the closing log line, without the per-ticker detail."""
    return {k: v for k, v in result.items() if not isinstance(v, list)}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the free pre-filter and report who WOULD be checked. "
                             "No LLM calls, no alerts, no cost.")
    parser.add_argument("--test-summary", action="store_true",
                        help="A dry run that DOES send the weekly summary, marked [TEST], "
                             "to check delivery and format. No LLM calls, nothing recorded.")
    args = parser.parse_args()
    dry_run = args.dry_run or args.test_summary

    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(log_dir, "buy_check_sweep.log"))],
    )
    try:
        result = run_sweep(dry_run=dry_run)
    except Exception as e:
        logger.exception("Buy-check sweep failed with an unhandled exception.")
        # A failed sweep is the one week the reader most needs to hear from it:
        # otherwise a broken job and a quiet week look the same.
        if not args.dry_run:
            send_summary({"failed": f"{type(e).__name__}: {e}"}, test=args.test_summary)
        sys.exit(1)

    logger.info(f"Buy-check sweep complete. {_brief(result)}")
    if result.get("disabled"):
        return
    if args.test_summary:
        send_summary(result, test=True)
    elif args.dry_run:
        title, body, _, _ = format_summary(result, date.today())
        logger.info(f"DRY RUN — the summary that would be sent:\n{title}\n\n{body}")
    else:
        try:
            import yaml
            with open(os.path.join(SRC_ROOT, "specs", "config.yaml"), "r",
                      encoding="utf-8") as fh:
                weekly = ((yaml.safe_load(fh) or {}).get("buy_check_sweep", {}) or {}) \
                    .get("weekly_summary", True)
        except Exception:
            weekly = True
        if weekly:
            send_summary(result)


if __name__ == "__main__":
    main_cli()
