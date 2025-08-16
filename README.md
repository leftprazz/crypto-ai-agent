# Crypto Alert Agent

Monitors **BTC, ETH, SOL, and HYPE** (largest-cap coin with ticker `HYPE`) for momentum and 24-hour sentiment, then sends alerts via **Email** and (optionally) **WhatsApp**.

* Refresh every **N** minutes (default **10**), with rolling 24h state in **SQLite**
* Triggers on **≥ +2%** 24h move (steps: +2%, +4%, +6%, …) with **60 min cooldown**
* Sentiment: **News** (+ optional **Twitter/X**) with quota/rotation
* Technicals from CoinGecko market chart (**RSI(14), SMA 20/50**)
* **Email** alert (multipart: pretty HTML + JSON body)
* **Daily Digest** at **23:50 Asia/Jakarta**
* **Optional WhatsApp** alert (seender.web.id) & WA daily digest — non-blocking

---

## Quick Start

1. **Clone & set env**

   ```bash
   cp .env.example .env   # then fill your keys
   ```
2. **Install deps**

   ```bash
   pip install -r requirements.txt
   ```
3. **Run**

   ```bash
   python main.py
   ```

### Docker

```bash
docker compose up --build -d
```

---

## Environment Variables

Create `.env` (fill what you need). All below are supported by the current code.

```ini
# --- Timezone & scheduler ---
TZ=Asia/Jakarta
REFRESH_MINUTES=10
COOLDOWN_MINUTES=60
MIN_ALERT_STEP_PCT=2.0
DAILY_DIGEST_LOCALTIME=23:50

# --- Data sources ---
CMC_API_KEY=               # primary price (optional; if empty, fallback to CoinGecko)
COINGECKO_API_BASE=https://api.coingecko.com/api/v3
COINGECKO_API_KEY=         # optional (demo/pro headers are auto-tried)
NEWSAPI_KEY=               # optional; fallback to Google News RSS
TWITTER_BEARER_TOKEN=      # optional; if empty, can use snscrape fallback
DISABLE_TWITTER_SCRAPE=0   # set 1 to fully disable scraping fallback

# --- X/Twitter quotas & rotation ---
X_MONTHLY_LIMIT=100        # total pulls/mo
X_DAILY_BUDGET=3           # daily budget
# Rotation groups alternate by UTC day ordinal:
# day%2==0 -> ["BTC","ETH"]; day%2==1 -> ["SOL","HYPE"]

# --- Email SMTP ---
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=username
SMTP_PASS=password
EMAIL_FROM=Crypto Alerts <crypto-agent@leftprazz.com>
EMAIL_TO=you@example.com

# --- Logging ---
LOG_LEVEL=INFO

# --- WhatsApp (seender.web.id) ---
WA_ENABLE=true             # true/false to enable WA alert
SEENDER_API_KEY=your_api_key
WA_SENDER_DEFAULT=62888xxxx
WA_NUMBERS=62812xxxx,62813xxxx     # comma-separated receivers
WA_FOOTER_DEFAULT=Sent via mpwa
WA_PREFER_GET=true         # GET tends to be more reliable on seender
WA_TIMEOUT=15              # seconds
WA_SEND_DIGEST=false       # true to also send the daily digest to WA
```

> Tip: If you want only email alerts, set `WA_ENABLE=false`.

---

## How It Works

### Trigger Logic

* Fetch price from **CMC** (if `CMC_API_KEY` present) else fallback to **CoinGecko**.
* Maintain per-coin daily row in SQLite (`alerts` table) storing:

  * first baseline price (first alert of the day), last fired step, last alert timestamp.
* Alert fires when:

  * `change_24h_pct >= MIN_ALERT_STEP_PCT`
  * New step exceeded (e.g., moving from <+4% to ≥+4%), and
  * Not within `COOLDOWN_MINUTES` since last alert for that coin.
* Before sending, it enriches with:

  * **Technicals** (RSI/SMA via CoinGecko market chart)
  * **Sentiment**: News always; Twitter/X only when allowed by **daily/monthly quota** and rotation.

### Scheduling

* Apscheduler runs:

  * `tick()` every `REFRESH_MINUTES`
  * `daily_digest()` at `DAILY_DIGEST_LOCALTIME` (Asia/Jakarta)

### Persistence

* Local SQLite at `./data/state.sqlite3`

  * `alerts`: alert per coin per day
  * `failures`: component-level error/notice log
  * `x_quota`: track daily/monthly Twitter pulls

---

## Outputs

### Email Alert

* **Subject**

  ```
  [ALERT] <COIN> +<pct>% in 24h — <Decision> — YYYY-MM-DD HH:MM Asia/Jakarta
  ```
* **Body**

  * Plain text: exact **JSON** (decision, sentiment, technicals, choices)
  * HTML: human-readable summary + “raw JSON” collapsible

### WhatsApp Alert (Optional)

* Uses **seender.web.id** `/send-message` with params:

  ```
  api_key, sender, number, message, footer
  ```
* Sends concise text like:

  ```
  🚨 Crypto Price Alert — BTC
  2025-08-17 01:05 WIB
  Price: $64,123.45 | 24h: 2.37%
  Decision: Consider Entry
  Sentiment: +0.22
  Note: Price up ≥2% in 24h; composite sentiment +0.22.
  ```
* **Non-blocking**: if WA fails, it logs at INFO and continues; email is never blocked.

### Daily Digest

* Email (and optional WA) summary of highest step per coin for the local day.

---

## Requirements

Your `requirements.txt` should include (min set used by the code):

```
apscheduler
SQLAlchemy
requests
pandas
numpy
vaderSentiment
feedparser
python-dotenv
certifi
```

> Optional: `snscrape` (CLI) must be available if you want Twitter fallback scraping. Disable via `DISABLE_TWITTER_SCRAPE=1` if not installed.

---

## Run & Operate

### Python

```bash
python main.py
```

### Docker (compose)

Minimal example:

```yaml
services:
  crypto-alert-agent:
    build: .
    container_name: crypto-alert-agent
    env_file: .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

---

## Troubleshooting

* **No alerts firing**
  Check `% change` actually exceeds `MIN_ALERT_STEP_PCT`, and cooldown isn’t active.

* **Email not received**
  Look at logs; verify SMTP credentials, port/TLS, and spam folder.

* **WA not sent**
  Ensure `WA_ENABLE=true`, `SEENDER_API_KEY`, `WA_SENDER_DEFAULT`, and `WA_NUMBERS` are set.
  The agent logs **success/failure JSON** from seender but never raises errors (by design).

* **Twitter/X quota**
  See `x_quota` table and logs:

  ```
  [X-Quota] {'used_today': x, 'remaining_today': y, 'used_month': a, 'remaining_month': b} | rotation today: ['BTC','ETH']
  ```

* **Technicals = null**
  CoinGecko market chart may return too few candles (<50). The agent will continue without RSI/SMA.

---

## Security Notes

* Keep `.env` out of version control.
* CMC/News/Twitter keys grant access/billing—protect them.
* WhatsApp uses a third-party gateway (seender); your API key and numbers are sent to their endpoint.

---

## License

MIT (or your preferred license).

---

## Credits

Built for **Crypto Analysis Automation** workflows. Uses public market & news APIs, Apscheduler, SQLite, and VADER sentiment.
