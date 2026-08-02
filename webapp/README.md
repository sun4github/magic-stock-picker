# Report Viewer (web app)

A small, read-only Flask web app for browsing the pipeline's analysis reports.
Designed to run on a Raspberry Pi (or any machine) and connect to the same
PostgreSQL database the agent writes to.

## What it does

It offers **two ways to browse the same reports**, switchable at the top of the page.

### Browse by ticker

1. Pick a **ticker** — an alphabetical dropdown / type-ahead text box (any ticker
   that has at least one analysis run).
2. Pick a **pipeline run** — sorted newest-first, with the run date shown. A run
   produced by `refine.py` (see below) is marked here — **✓ critic-reviewed** or
   **⚠ critic did NOT agree** — before you click into it.
3. View the **Bear Case**, **Bull Case**, **Sale Advisory**, and **Final Report**
   for that run, rendered as markdown, with the **recommendation** (Buy / Watch /
   Avoid) badge.
4. **Download** any report as a `.md` file to the viewing device.

### Critic-reviewed runs

Some runs come from `python refine.py TICKER` (the repo root README documents the
command) rather than from the pipeline: an independent critic agent reviews an
already-produced report and the analyst revises against its findings until they
agree or the budget runs out. Those runs get two things an ordinary run doesn't:

- A **standing chip** next to the verdict badge — green "✓ critic agreed · N
  round(s)" or amber "⚠ critic did NOT agree · N round(s)" — so you know at a
  glance whether the report was checked and how that went, without opening the
  fifth tab.
- A fifth tab, **Critic Review**, listing every round of the exchange oldest-first.
  This is the only place the *full* back-and-forth is visible — the stored Final
  Report carries only the critic's last review, and only when they never agreed.

A critic-reviewed run's Bear/Bull/Sale tabs are usually **borrowed** from the run
it reviewed (stamped "From the reviewed run …") rather than duplicated, since
`refine.py` critiques an existing report without re-running that research.

### Browse by pipeline run

1. Pick a **pipeline run** — sorted newest-first, with the run date and ticker
   count shown. Only **multi-ticker** runs appear here (value-discovery screens);
   single-ticker on-demand one-offs are excluded — browse those under "by ticker".
2. See **every ticker analyzed in that run**, grouped **Buy first, then Watch,
   then Avoid**, and ordered by **Magic Formula rank** within each group (best
   rank first) — each with its recommendation badge, its rank, and a summary
   tally. Runs recorded before ranks were stored show `—` and fall back to
   alphabetical order within their group.
3. Click **View reports** on any ticker to open its Bear / Bull / Sale Advisory /
   Final reports for that specific run (a "Back to run" link returns to the list).
4. **Download CSV** — all tickers, recommendations and ranks for the run
   (`Ticker,Company,Recommendation,MagicFormulaRank`), in the same order, to the
   viewing device.

Data comes from the `ticker_runs`, `agent_outputs`, and `final_reports` tables —
the app is read-only and never modifies the database. The "by pipeline run" view
is `ticker_runs` grouped by `run_id`; the per-ticker view is `ticker_runs`
filtered by `ticker`.

## 🍋 Learn the terms — investing lessons via a lemonade stand

Reading a real 10-K for fun is a niche hobby. So there's a third mode in the top
nav — **🍋 Learn the terms** — that teaches the exact same formulas the screener
runs on actual companies, except the "company" here is a kid's lemonade stand
with a folding table and a hand-painted sign, and nobody's life savings are on
the line while you figure it out.

**Drag the sliders, watch the whole business wobble.** Cups sold, price per
cup, ingredients (lemons + ice) cost, operating expenses, the cost of the stand
itself, debt, cash, goodwill (yes, even lemonade stands can overpay for
acquisitions), market cap, and tax rate — ten knobs, and every one of them
updates a live income statement, balance sheet, cash flow statement, and the two
numbers Greenblatt actually built the Magic Formula around: **Return on
Capital** and **Earnings Yield**. Hover any line item for a popover with its
formula, a plain-English definition, and a worked lemonade-stand example — no
finance degree, or even a summer job running a stand, required.

**Six scenarios, one stand, wildly different fortunes.** Rather than making you
stumble onto the "aha" yourself, six preset buttons hand it to you on a
napkin:

| Preset | What it teaches |
| :--- | :--- |
| 📄 Cheat-sheet example | The baseline numbers — matches the downloadable cheat sheet exactly |
| ✅ Cheap & high quality | The actual sweet spot the Magic Formula goes hunting for |
| 💎 Great business, priced for perfection | Great lemonade, terrible price — quality alone doesn't make it cheap |
| ⚠️ Cheap but mediocre | Rock-bottom price, and the lemonade explains why |
| 🕳️ Value trap | Looks cheap, is drowning in debt, and quietly getting worse by the cup |
| 🎭 Goodwill rollup (the ROC mirage) | The stand "bought" a few rival stands, and its ROC now flatters it for reasons that have nothing to do with lemonade |

Click through them and watch the same ten numbers rearrange into every corner
of the cheap-vs-quality grid the Magic Formula grades stocks on — including the
one corner where the formula itself can be fooled by goodwill, which is exactly
the trap [§2.F of the architecture spec](../src/specs/agent_architecture.md)
warns the real screener about. Turns out a lemonade stand can teach you to
distrust an inflated ROC just as well as a real balance sheet can.

**An "illustrative screen" badge** applies the same cheap-and-good logic as the
real Phase A screener (Earnings Yield ≥ 10%, Return on Capital ≥ 25%) so a
Buy/Watch/Avoid-style badge reacts to your sliders in real time. It's a teaching
simplification, not this app's actual verdict — the real one only shows up after
the bear, bull, and sale-advisor agents have fought it out over an actual 10-K,
not a lemonade stand's napkin math. Think of the Learn tab as training wheels
for [`src/magic_formula_starter_screener.py`](../src/magic_formula_starter_screener.py),
running the same eligibility logic on a business simple enough to hold in your
head.

**Take the stand home with you.** The **📄 Open cheat sheet** / **⬇ Download**
buttons hand you a print-ready reference sheet
(`static/learn/lemonade-cheat-sheet.html`) with every formula, every
definition, and the same baseline numbers as the "Cheat-sheet example" preset —
print it, save it as a PDF, or just keep it open in another tab while you poke
at the sliders. Same stand, same numbers, no matter which one you open.

## Requirements

- Python 3.10+
- Network access to the PostgreSQL database
- `pip install -r requirements.txt` (flask, psycopg2-binary, python-dotenv, markdown)

On a Raspberry Pi, `psycopg2-binary` and the rest install from prebuilt ARM
wheels — no compilation needed.

## Setup & run

```bash
cd webapp
pip install -r requirements.txt
cp .env.example .env        # then edit .env with your DATABASE_URL
python app.py
```

The app listens on `0.0.0.0:8000` by default (set `PORT` in `.env` to change it),
so it's reachable from other devices on the network at
`http://<raspberry-pi-ip>:8000`.

### Choosing the port

The app reads `PORT` from `.env` (default `8000`) and binds `0.0.0.0`, so it's
reachable at `http://<raspberry-pi-ip>:<PORT>`. Set `PORT=8080` (or any port) in
`.env`, or via the `Environment=` line in the systemd unit below.

### Running as a permanent background service (Raspberry Pi)

For an always-on job that survives reboots and crashes, install a `systemd`
service. Create `/etc/systemd/system/report-viewer.service` (adjust path, user,
port):

```ini
[Unit]
Description=Magic Stock Picker Report Viewer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/magic-stock-picker/webapp
Environment=PORT=8080
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now report-viewer      # start now + on every boot
sudo systemctl status report-viewer            # check status
journalctl -u report-viewer -f                 # follow logs
```

For heavier traffic you can front it with a WSGI server
(`pip install gunicorn` then set
`ExecStart=/usr/bin/gunicorn -b 0.0.0.0:8080 app:app`), but the built-in server
is fine for personal/LAN use.

## Notes

- The app renders the reports' own markdown (trusted pipeline output) to HTML
  server-side, so it works offline with no external CDN/JS dependencies.
- If the ticker dropdown is empty, no runs have been analyzed yet (or
  `ticker_runs` hasn't been populated) — run the agent first.
