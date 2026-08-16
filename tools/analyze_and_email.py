"""Analyse one ticker, then email whatever reports the run produced.

    python tools/analyze_and_email.py BKNG
    python tools/analyze_and_email.py BKNG "Booking Holdings" --skip-sale-advisor

A thin wrapper around `python main.py TICKER`, for the case the MCP layer's
`start_ticker_analysis(email_report=True)` serves: someone asks about a ticker while
you are away from the machine, an agent runs the analysis for you, and the reports
need to arrive somewhere you can actually read them on a phone.

WHY A WRAPPER RATHER THAN A STEP IN THE SERVER

The MCP server launches runs as detached subprocesses and returns a job id; it does
not stay attached, and it may be restarted or stopped while a run is still going. If
the server sent the mail, an emailed report would depend on the server still being
alive several minutes later. Making the email the last step of the JOB removes that
dependency entirely — the run either finishes and mails, or it does not, and nothing
outside it has to survive for that to hold.

It also keeps the MCP layer's rule intact: `agent_mcp/` reaches the pipeline only by
running a command, and this is just another command.

WHY THE PLUNK CALL IS REPEATED HERE

`src/notifications.py` already sends through Plunk, and this deliberately does not
import it. Two reasons. Its `_send_email` renders a short alert — it converts blank
lines to paragraphs, which turns a Markdown report's tables and code blocks into
mush — so the body would have to be built here regardless. And keeping this file out
of `src/` preserves the property that the MCP work added nothing to the pipeline
package. What is shared is the ENV VAR NAMES, which is the repo's existing
convention and the thing that actually matters: one set of credentials serves the
tracker, the sweep alerts and this.
"""

import argparse
import html
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
REPORTS_DIR = SRC_DIR / "reports"
PLUNK_URL = "https://next-api.useplunk.com/v1/send"

#: Report kinds a single-ticker run can produce, in the order they belong in an
#: email: the verdict first, then the documents derived from it.
KINDS = ("Final_Report", "Refined_Report", "Sale_Advisory", "Buy_Case")

#: Total characters of report text to include. Comfortably more than three reports
#: need; a guard against mailing something enormous rather than a real limit.
MAX_BODY_CHARS = 400_000


def recipient() -> str:
    """Where reports go.

    `ALERT_EMAIL_TO` by default — the address the buy-trigger alerts already use, so
    one setting governs everything this repo sends. `REPORT_EMAIL_TO` overrides it
    for the case where reports and alerts want different inboxes.

    There is deliberately NO way to pass a recipient in. This runs behind a
    network-reachable MCP tool, and a free-text destination would make anyone
    holding the bearer token able to send mail from your domain to anywhere.
    """
    return os.getenv("REPORT_EMAIL_TO") or os.getenv("ALERT_EMAIL_TO", "")


def find_reports(ticker: str, newer_than: float) -> list:
    """The reports for `ticker` that this run actually wrote.

    Filtered by modification time, so a stale advisory left over from a previous
    run is not mailed as though it were part of this one. A one-second grace
    absorbs filesystem timestamp granularity.
    """
    if not REPORTS_DIR.exists():
        return []
    found = []
    for kind in KINDS:
        for path in sorted(REPORTS_DIR.glob(f"{ticker}_{kind}*.md")):
            if path.stat().st_mtime >= newer_than - 1:
                found.append(path)
    return found


def verdict_of(paths: list) -> str:
    """The verdict, read off the report filename the pipeline chose."""
    for path in paths:
        for kind in ("Final_Report", "Refined_Report"):
            marker = f"_{kind}_"
            if marker in path.name:
                return path.name.split(marker, 1)[1][:-3]
    return ""


def build_body(ticker: str, paths: list, verdict: str) -> str:
    """One HTML document holding every report, readable on a phone.

    Each report goes inside <pre>. Markdown tables, the price header every report
    opens with, and the numeric alignment throughout only survive in a monospaced
    block — converted to paragraphs they become unreadable, which for a document
    whose whole point is figures is worse than sending nothing.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif">',
        f"<h2>{html.escape(ticker)}"
        + (f" &mdash; {html.escape(verdict)}" if verdict else "")
        + "</h2>",
        f"<p style='color:#666'>Generated {stamp} by magic-stock-picker. "
        f"{len(paths)} document{'s' if len(paths) != 1 else ''}.</p>",
        "<p style='color:#666'>A report is a point-in-time document: the price it "
        "opens with is what the stock cost when it was written, not now.</p>",
    ]

    budget = MAX_BODY_CHARS
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > budget:
            text = text[:budget] + "\n\n[... truncated; read the full file on the host ...]"
        budget -= len(text)
        title = path.stem.replace("_", " ")
        head.append(f"<hr><h3>{html.escape(title)}</h3>")
        head.append(
            "<pre style='white-space:pre-wrap;word-wrap:break-word;"
            "font-family:ui-monospace,Consolas,monospace;font-size:13px;"
            "line-height:1.45'>" + html.escape(text) + "</pre>"
        )
        if budget <= 0:
            head.append("<p><em>Remaining reports omitted for length.</em></p>")
            break
    head.append("</div>")
    return "".join(head)


def send(subject: str, body_html: str) -> bool:
    key, sender, to = (
        os.getenv("PLUNK_EMAIL_KEY", ""),
        os.getenv("PLUNK_FROM_EMAIL", ""),
        recipient(),
    )
    if not (key and sender and to):
        print("PLUNK_EMAIL_KEY / PLUNK_FROM_EMAIL / ALERT_EMAIL_TO not all set; "
              "skipping the email. The reports are still on disk.")
        return False
    try:
        requests.post(
            PLUNK_URL,
            json={"to": to, "subject": subject, "body": body_html, "from": sender,
                  "name": os.getenv("PLUNK_FROM_NAME", "Magic Stock Picker")},
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        ).raise_for_status()
        print(f"Emailed to {to}: {subject}")
        return True
    except requests.RequestException as exc:
        print(f"Failed to send the email: {exc}")
        return False


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ticker")
    parser.add_argument("company", nargs="?")
    parser.add_argument("--skip-sale-advisor", action="store_true")
    parser.add_argument("--skip-buy-case", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    argv = [sys.executable, "main.py", ticker]
    if args.company:
        argv.append(args.company)
    for flag, given in (("--skip-sale-advisor", args.skip_sale_advisor),
                        ("--skip-buy-case", args.skip_buy_case),
                        ("--force", args.force)):
        if given:
            argv.append(flag)

    started = time.time()
    print(f"$ {' '.join(argv)}")
    # Inherits stdout/stderr, so the MCP job log shows the run exactly as it would
    # from a terminal; this wrapper adds lines, it does not swallow any.
    result = subprocess.run(argv, cwd=str(SRC_DIR))

    if result.returncode != 0:
        print(f"\nAnalysis exited {result.returncode}; sending a failure note instead.")
        send(
            f"[{ticker}] analysis FAILED",
            f"<p>The run for <b>{html.escape(ticker)}</b> exited with code "
            f"{result.returncode}. Nothing was emailed because no report was "
            f"produced. Check the job log on the host.</p>",
        )
        return result.returncode

    paths = find_reports(ticker, started)
    if not paths:
        print("\nRun succeeded but wrote no new reports — nothing to email.")
        print("(A report reused from inside the reuse window is not rewritten, so "
              "its timestamp is older than this run. Re-run with --force to "
              "regenerate and mail it.)")
        return 0

    verdict = verdict_of(paths)
    subject = f"[{ticker}]" + (f" {verdict}" if verdict else "") + " — stock analysis"
    print(f"\nEmailing {len(paths)} report(s): {', '.join(p.name for p in paths)}")
    send(subject, build_body(ticker, paths, verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
