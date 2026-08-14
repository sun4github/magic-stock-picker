"""Push and email alerts for things that are actually news.

Deliberately a near-copy of magic-stock-tracker's `app/notifications.py`, which
already sends Pushover + Plunk from the same two accounts. The alternative was
importing it across repo boundaries, which would make this repo's scheduled jobs
depend on the other repo being checked out beside it on every machine that runs
them. The two send different messages about different things; they only share a
transport.

Environment variables are the SAME NAMES the tracker uses, so one set of
credentials copied into this repo's .env serves both:

    PO_APP_KEY, PO_USER_KEY, PO_DEVICE      (Pushover; device optional)
    PLUNK_EMAIL_KEY, PLUNK_FROM_EMAIL,
    PLUNK_FROM_NAME, ALERT_EMAIL_TO         (email)

Missing credentials are a warning, never an error: a job's real work is done by
the time it gets here, and failing the run because a push could not be delivered
would throw away the result that prompted it.

NOT used by the ledger snapshot job, which is silent by design — a price being
recorded is not news.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PLUNK_URL = "https://next-api.useplunk.com/v1/send"


def _send_pushover(title: str, message: str, priority: int = 0) -> bool:
    app_key, user_key = os.getenv("PO_APP_KEY", ""), os.getenv("PO_USER_KEY", "")
    if not (app_key and user_key):
        logger.warning("PO_APP_KEY/PO_USER_KEY not set; skipping Pushover notification.")
        return False
    data = {
        "token": app_key, "user": user_key, "title": title,
        "message": message, "priority": priority,
        # Same sound the tracker uses for a sell alert, for the same reason: a
        # trigger firing is a "go and do something with money" event.
        "sound": "cashregister",
    }
    device = os.getenv("PO_DEVICE", "")
    if device:
        data["device"] = device
    try:
        requests.post(PUSHOVER_URL, data=data, timeout=10).raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Pushover notification: {e}")
        return False


def _send_email(subject: str, message: str) -> bool:
    key = os.getenv("PLUNK_EMAIL_KEY", "")
    sender, to = os.getenv("PLUNK_FROM_EMAIL", ""), os.getenv("ALERT_EMAIL_TO", "")
    if not (key and sender and to):
        logger.warning("PLUNK_EMAIL_KEY/PLUNK_FROM_EMAIL/ALERT_EMAIL_TO not set; "
                       "skipping email notification.")
        return False
    body = "<p>" + message.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    try:
        requests.post(
            PLUNK_URL,
            json={"to": to, "subject": subject, "body": body, "from": sender,
                  "name": os.getenv("PLUNK_FROM_NAME", "Magic Stock Picker")},
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        ).raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send email via Plunk: {e}")
        return False


def send_buy_trigger_alert(ticker: str, company_name: str, price: float,
                           trigger_price: float, reason: str, detail: str = "") -> None:
    """Alert that a watchlist name's buy conditions now read BUY.

    Each channel is attempted independently, so a failure or missing config on
    one does not suppress the other.

    The message names the price, the trigger it cleared and why the sweep looked
    at this ticker at all, because the reader is deciding whether to open the
    full report — and a notification that says only "BUY AAPL" makes that
    decision impossible without opening it every time.
    """
    title = f"BUY signal: {ticker} at ${price:.2f}"
    lines = [f"{company_name} ({ticker}) is at ${price:.2f}."]
    if trigger_price:
        lines.append(f"Its buy case set a price trigger of ${trigger_price:.2f}.")
    lines.append(f"Escalated because: {reason}")
    lines.append("The buy-check agent has confirmed the conditions read BUY.")
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    lines.append(f"Read the full check: reports/{ticker}_Buy_Check.md")
    # Priority 1 (bypasses quiet hours) — this one is time-sensitive in a way a
    # price snapshot never is: a trigger that fired can un-fire before it is read.
    message = "\n".join(lines)
    _send_pushover(title, message, priority=1)
    _send_email(title, message)


def send_unreadable_trigger_alert(ticker: str, weeks: int, cost_per_check: float = 0.08) -> None:
    """Tell the holder a buy case needs regenerating, once per episode.

    Not urgent, and priority reflects that: nothing is broken, the weekly check
    still runs correctly and still alerts on a Buy. What it cannot do is answer
    cheaply, because a trigger like "below 8.0x forward earnings" needs judgement
    rather than a comparison — so that name is escalated to the model every week
    instead of being screened out for free.

    Sent ONCE when the streak is first reached rather than on every subsequent
    sweep. A weekly reminder about a small recurring cost is exactly the message
    a reader learns to swipe away, which would then also cost them the Buy alert
    that arrives through the same channel.
    """
    title = f"Buy case needs refreshing: {ticker}"
    message = "\n".join([
        f"{ticker}'s buy case has had no readable price trigger for {weeks} "
        f"sweeps in a row.",
        "",
        "Nothing is broken — the weekly check still runs and will still alert "
        "you if the conditions read Buy. But the price trigger is written in a "
        "form the free pre-filter cannot compare against a quote (a valuation "
        "multiple rather than a dollar amount), so this one is sent to the model "
        f"every week at about ${cost_per_check:.2f} a time instead of being "
        "screened out for nothing.",
        "",
        f"Usually the cause is that the price fetch failed on the day the case "
        f"was written. Regenerating it against a live quote fixes it:",
        f"    python buy_case.py {ticker}",
    ])
    # Priority -1: delivered quietly, no sound, no interruption. It is a chore,
    # not an event.
    _send_pushover(title, message, priority=-1)
    _send_email(title, message)
