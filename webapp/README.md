# Report Viewer (web app)

A small, read-only Flask web app for browsing the pipeline's analysis reports.
Designed to run on a Raspberry Pi (or any machine) and connect to the same
PostgreSQL database the agent writes to.

## What it does

It offers **two ways to browse the same reports**, switchable at the top of the page.

### Browse by ticker

1. Pick a **ticker** — an alphabetical dropdown / type-ahead text box (any ticker
   that has at least one analysis run).
2. Pick a **pipeline run** — sorted newest-first, with the run date shown.
3. View the **Bear Case**, **Bull Case**, **Sale Advisory**, and **Final Report**
   for that run, rendered as markdown, with the **recommendation** (Buy / Watch /
   Avoid) badge.
4. **Download** any report as a `.md` file to the viewing device.

### Browse by pipeline run

1. Pick a **pipeline run** — sorted newest-first, with the run date and ticker
   count shown. Only **multi-ticker** runs appear here (value-discovery screens);
   single-ticker on-demand one-offs are excluded — browse those under "by ticker".
2. See **every ticker analyzed in that run**, listed A–Z, each with its
   **Buy / Watch / Avoid** recommendation badge and a summary tally.
3. Click **View reports** on any ticker to open its Bear / Bull / Sale Advisory /
   Final reports for that specific run (a "Back to run" link returns to the list).
4. **Download CSV** — all tickers and their recommendations for the run
   (`Ticker,Company,Recommendation`) to the viewing device.

Data comes from the `ticker_runs`, `agent_outputs`, and `final_reports` tables —
the app is read-only and never modifies the database. The "by pipeline run" view
is `ticker_runs` grouped by `run_id`; the per-ticker view is `ticker_runs`
filtered by `ticker`.

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
