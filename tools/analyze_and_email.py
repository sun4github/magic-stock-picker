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
import base64
import html
import os
import re
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

#: Plunk's limits: 10 attachments, 10MB total. A run produces at most four documents
#: of a few tens of kilobytes, so these are guards against something unexpected
#: rather than limits anyone will meet.
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 9_000_000


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


def _price_line(paths: list) -> str:
    """The share price and timestamp, lifted from the report's own price table.

    Every report opens with a one-row table whose first cell is the bolded price and
    whose second is when it was taken. Parsed rather than recomputed so the summary
    can never disagree with the document attached beside it.
    """
    for path in paths:
        if "_Final_Report" not in path.name and "_Refined_Report" not in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^\|\s*\*\*(\$[\d,.]+)\*\*\s*\|\s*([^|]+?)\s*\|", text, re.M)
        if match:
            return f"{match.group(1)} as of {match.group(2)}"
    return ""


def _gist(paths: list) -> str:
    """The opening sentence of the analyst's verdict, as the one line of substance.

    The point of the summary is to let someone reading on a phone decide whether to
    open the attachment now or later, and the first paragraph under `## Final
    Verdict` is where the analyst states the case in a sentence.
    """
    for path in paths:
        if "_Final_Report" not in path.name and "_Refined_Report" not in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^## Final Verdict\s*\n+(.+?)(?:\n\n|\Z)", text, re.M | re.S)
        if not match:
            continue
        para = " ".join(match.group(1).split())
        if len(para) > 300:
            cut = para[:300].rsplit(". ", 1)[0]
            para = (cut + ".") if len(cut) > 80 else para[:300].rstrip() + "…"
        return para
    return ""


def build_summary(ticker: str, paths: list, verdict: str) -> str:
    """A few lines of orientation. The reports themselves travel as attachments.

    Deliberately short. Inlining the reports made a message thousands of lines long
    that had to be scrolled past to find anything, and Markdown tables rendered in an
    email body are worse than the same table in a file the reader opens deliberately.
    """
    lines = [
        f"<b>{html.escape(ticker)}</b>"
        + (f" &mdash; <b>{html.escape(verdict)}</b>" if verdict else ""),
    ]
    price = _price_line(paths)
    if price:
        lines.append(f"Priced at {html.escape(price)}, when the report was written.")
    gist = _gist(paths)
    if gist:
        lines.append(html.escape(gist))
    lines.append(
        "Attached: " + html.escape(", ".join(p.stem.replace("_", " ") for p in paths)) + "."
    )
    lines.append(
        "<span style='color:#777'>A stored report is point-in-time — check the current "
        "price before acting on it.</span>"
    )
    body = "".join(f"<p style='margin:0 0 10px'>{line}</p>" for line in lines)
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "font-size:15px;line-height:1.5\">" + body + "</div>"
    )


def build_attachments(paths: list) -> list:
    """The reports, base64-encoded, as Plunk expects them.

    Plunk allows 10 attachments and 10MB in total; a run produces at most four
    documents of a few tens of kilobytes, so the cap is a guard against something
    unexpected rather than a limit anyone will meet. Anything that would breach it is
    dropped with a note rather than silently truncated, because a half a report is
    worse than a clearly absent one.
    """
    out, total = [], 0
    for path in paths[:MAX_ATTACHMENTS]:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        if total + len(encoded) > MAX_ATTACHMENT_BYTES:
            print(f"  skipping {path.name}: would exceed the {MAX_ATTACHMENT_BYTES // 1_000_000}MB "
                  f"attachment limit")
            continue
        total += len(encoded)
        out.append({
            "filename": path.name,
            "content": encoded,
            "contentType": "text/markdown",
            "disposition": "attachment",
        })
    return out


def send(subject: str, body_html: str, attachments: list = None) -> bool:
    key, sender, to = (
        os.getenv("PLUNK_EMAIL_KEY", ""),
        os.getenv("PLUNK_FROM_EMAIL", ""),
        recipient(),
    )
    if not (key and sender and to):
        print("PLUNK_EMAIL_KEY / PLUNK_FROM_EMAIL / ALERT_EMAIL_TO not all set; "
              "skipping the email. The reports are still on disk.")
        return False
    payload = {"to": to, "subject": subject, "body": body_html, "from": sender,
               "name": os.getenv("PLUNK_FROM_NAME", "Magic Stock Picker")}
    if attachments:
        payload["attachments"] = attachments
    try:
        response = requests.post(
            PLUNK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            # Longer than the alert path's 10s: the request now carries the reports
            # themselves, so it is an upload rather than a few hundred bytes.
            timeout=60,
        )
        response.raise_for_status()
        count = len(attachments or [])
        print(f"Emailed to {to}: {subject} ({count} attachment{'s' if count != 1 else ''})")
        return True
    except requests.RequestException as exc:
        # The response body usually names the offending field; without it a schema
        # rejection looks identical to the server being down.
        detail = ""
        if getattr(exc, "response", None) is not None:
            detail = f" — {exc.response.status_code}: {exc.response.text[:400]}"
        print(f"Failed to send the email: {exc}{detail}")
        return False


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ticker")
    parser.add_argument("company", nargs="?")
    parser.add_argument("--skip-sale-advisor", action="store_true")
    parser.add_argument("--skip-buy-case", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--email-existing", action="store_true",
        help="Skip the analysis and mail the ticker's STORED reports. Costs nothing "
             "and takes seconds - the way to check delivery and formatting without "
             "paying for a run, and also how to re-send a report you already have.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --email-existing: build the message and print it, send nothing.",
    )
    args = parser.parse_args()

    ticker = args.ticker.upper()

    if args.email_existing:
        # newer_than=0 takes whatever is on disk, rather than only what a run just
        # wrote — there is no run.
        paths = find_reports(ticker, 0)
        if not paths:
            print(f"No stored reports for {ticker}.")
            return 1
        verdict = verdict_of(paths)
        subject = f"[{ticker}]" + (f" {verdict}" if verdict else "") + " — stock analysis"
        body, attachments = build_summary(ticker, paths, verdict), build_attachments(paths)
        if args.dry_run:
            print(f"To:      {recipient() or '(not configured)'}")
            print(f"Subject: {subject}")
            print(f"Body:    {len(body)} chars")
            for a in attachments:
                print(f"Attach:  {a['filename']} ({a['contentType']})")
            print("\n--- dry run: nothing sent ---")
            return 0
        return 0 if send(subject, body, attachments) else 1
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
    send(subject, build_summary(ticker, paths, verdict), build_attachments(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
