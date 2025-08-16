#!/usr/bin/env python3
"""
Crypto Alert Agent
- Monitors BTC, ETH, SOL, HYPE (largest cap 'HYPE')
- Refreshes every N minutes, keeps rolling 24h state in SQLite
- Fires alert emails with exact subject & JSON body schema
- Sends daily digest at 23:50 Asia/Jakarta
"""

import os, time, math, json, smtplib, logging, hashlib, html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Tuple
import certifi

import requests
import pandas as pd
import numpy as np

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import feedparser

from dotenv import load_dotenv
load_dotenv()
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# Twitter
DISABLE_TWITTER_SCRAPE = os.getenv("DISABLE_TWITTER_SCRAPE", "0") in ("1", "true", "True")
# ==== X / Twitter quota config ====
X_MONTHLY_LIMIT = int(os.getenv("X_MONTHLY_LIMIT", "100"))  # Free plan: 100 posts / month
X_DAILY_BUDGET  = int(os.getenv("X_DAILY_BUDGET", "3"))     # ~100/30 ≈ 3 per day
# Rotasi: hari genap -> BTC,ETH; hari ganjil -> SOL,HYPE  (boleh diubah)
ROTATION_GROUPS = [["BTC", "ETH"], ["SOL", "HYPE"]]

# ---------------------------- Config ----------------------------

TZ_NAME = os.getenv("TZ", "Asia/Jakarta")
TZ = ZoneInfo(TZ_NAME)

REFRESH_MINUTES = int(os.getenv("REFRESH_MINUTES", "10"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))
MIN_ALERT_STEP_PCT = float(os.getenv("MIN_ALERT_STEP_PCT", "2.0"))
DAILY_DIGEST_LOCALTIME = os.getenv("DAILY_DIGEST_LOCALTIME", "23:50")

CMC_API_KEY = os.getenv("CMC_API_KEY", "")
CG_API_BASE = os.getenv("COINGECKO_API_BASE", "https://api.coingecko.com/api/v3")
TWITTER_BEARER = os.getenv("TWITTER_BEARER_TOKEN", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")
CG_API_BASE = os.getenv("COINGECKO_API_BASE", "https://api.coingecko.com/api/v3")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Crypto Alerts <crypto-agent@leftprazz.com>")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "state.sqlite3")
ENGINE = create_engine(f"sqlite:///{DB_PATH}", poolclass=NullPool, future=True)

COINS = ["BTC", "ETH", "SOL", "HYPE"]

# ---------------------------- DB Setup ----------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    date_key TEXT NOT NULL,    -- YYYY-MM-DD (local)
    first_baseline_price REAL, -- price at first alert of the day
    last_step INTEGER,         -- last +2% step fired (e.g., 1 for +2, 2 for +4, etc.)
    last_alert_ts TEXT         -- ISO timestamp local
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_coin_date ON alerts(coin, date_key);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS x_quota (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_key TEXT NOT NULL,   -- YYYY-MM-DD (UTC)
    month_key TEXT NOT NULL,  -- YYYY-MM (UTC)
    used_today INTEGER NOT NULL DEFAULT 0,
    used_month INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_xquota_day ON x_quota(date_key);
CREATE INDEX IF NOT EXISTS ix_xquota_month ON x_quota(month_key);

"""

with ENGINE.begin() as conn:
    # Pecah di setiap ';' agar selalu satu statement per execute
    for stmt in SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(text(stmt))

# ---------------------------- Utilities ----------------------------

def now_local() -> datetime:
    return datetime.now(TZ)

def today_key(dt: Optional[datetime] = None) -> str:
    dt = dt or now_local()
    return dt.strftime("%Y-%m-%d")

def record_failure(component: str, detail: str):
    logging.warning(f"[{component}] {detail}")
    with ENGINE.begin() as conn:
        conn.execute(text("INSERT INTO failures(ts, component, detail) VALUES(:ts, :c, :d)"),
                     {"ts": now_local().isoformat(), "c": component, "d": detail[:1000]})

def backoff_delays():
    # 5 tries: 1s, 2s, 4s, 8s, 16s
    delay = 1
    for _ in range(5):
        yield delay
        delay *= 2

# ==== Quota helpers (X API) ====
def utc_today_key():
    return datetime.utcnow().strftime("%Y-%m-%d")

def utc_month_key():
    return datetime.utcnow().strftime("%Y-%m")

def _ensure_quota_row():
    dkey, mkey = utc_today_key(), utc_month_key()
    with ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT id, used_today, used_month, month_key FROM x_quota WHERE date_key=:d"),
            {"d": dkey}
        ).mappings().first()
        if row:
            # jika bulan berganti, reset used_month ke 0
            if row["month_key"] != mkey:
                conn.execute(text(
                    "UPDATE x_quota SET used_today=0, used_month=0, month_key=:m WHERE id=:id"
                ), {"m": mkey, "id": row["id"]})
        else:
            conn.execute(text(
                "INSERT INTO x_quota(date_key, month_key, used_today, used_month) VALUES(:d,:m,0,0)"
            ), {"d": dkey, "m": mkey})

def x_quota_status():
    _ensure_quota_row()
    dkey, mkey = utc_today_key(), utc_month_key()
    with ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT used_today, used_month FROM x_quota WHERE date_key=:d"),
            {"d": dkey}
        ).mappings().first()
    used_today = row["used_today"] if row else 0
    used_month = row["used_month"] if row else 0
    return {
        "used_today": used_today,
        "remaining_today": max(0, X_DAILY_BUDGET - used_today),
        "used_month": used_month,
        "remaining_month": max(0, X_MONTHLY_LIMIT - used_month),
    }

def x_quota_consume(n=1):
    _ensure_quota_row()
    dkey = utc_today_key()
    with ENGINE.begin() as conn:
        conn.execute(text("""
            UPDATE x_quota
               SET used_today = used_today + :n,
                   used_month = used_month + :n
             WHERE date_key = :d
        """), {"n": n, "d": dkey})

def rotation_coins_for_today():
    # gunakan tanggal UTC untuk rotasi yang stabil
    idx = datetime.utcnow().toordinal() % len(ROTATION_GROUPS)
    return set(ROTATION_GROUPS[idx])

def twitter_allowed_today(coin: str, override: bool = False) -> bool:
    qs = x_quota_status()
    if qs["remaining_today"] <= 0 or qs["remaining_month"] <= 0:
        return False
    if override:
        return True
    # hanya coin yang masuk grup rotasi hari ini
    return coin in rotation_coins_for_today()

# ---------------------------- Data Sources ----------------------------

# CoinGecko request helper with API key header
def cg_get(path: str, params: dict | None = None):
    url = f"{CG_API_BASE}{path}"
    params = params or {}
    # coba DEMO dulu kalau ada key
    if CG_API_KEY:
        # 1) DEMO
        try:
            r = requests.get(url, params=params,
                             headers={"x-cg-demo-api-key": CG_API_KEY},
                             timeout=20)
            if r.status_code < 400:
                return r
            record_failure("coingecko_http", f"{r.status_code} {url} :: {r.text[:200]}")
        except requests.HTTPError as e:
            record_failure("coingecko_http", f"{e} {url}")
        except Exception as e:
            record_failure("coingecko_req", f"{e} {url}")

        # 2) PRO (kalau ternyata key kamu pro)
        try:
            r = requests.get(url, params=params,
                             headers={"x-cg-pro-api-key": CG_API_KEY},
                             timeout=20)
            if r.status_code >= 400:
                record_failure("coingecko_http", f"{r.status_code} {url} :: {r.text[:200]}")
            r.raise_for_status()
            return r
        except Exception as e:
            record_failure("coingecko_req", f"{e} {url}")
            raise
    else:
        # tanpa key (public) — bisa rate/limits
        r = requests.get(url, params=params, timeout=20)
        if r.status_code >= 400:
            record_failure("coingecko_http", f"{r.status_code} {url} :: {r.text[:200]}")
        r.raise_for_status()
        return r


def get_coin_ids_map() -> Dict[str, str]:
    try:
        r = cg_get("/coins/list")
        data = r.json()
        by_symbol = {}
        for c in data:
            sym = c.get("symbol","").upper()
            by_symbol.setdefault(sym, []).append(c["id"])
        return by_symbol
    except Exception as e:
        record_failure("coingecko_ids", str(e))
        return {}


CG_IDS_CACHE = None

def resolve_hype_symbol_coingecko() -> Optional[str]:
    global CG_IDS_CACHE
    try:
        if CG_IDS_CACHE is None:
            CG_IDS_CACHE = get_coin_ids_map()
        ids = CG_IDS_CACHE.get("HYPE", [])
        if not ids:
            return None
        best_id, best_mcap = None, -1
        for cid in ids:
            try:
                r = cg_get(f"/coins/{cid}", {
                    "localization":"false","tickers":"false","market_data":"true",
                    "community_data":"false","developer_data":"false","sparkline":"false"
                })
                md = r.json().get("market_data",{})
                mcap = md.get("market_cap",{}).get("usd")
                if mcap is not None and mcap > best_mcap:
                    best_mcap = mcap
                    best_id = cid
            except Exception:
                continue
        return best_id
    except Exception as e:
        record_failure("resolve_hype", str(e))
        return None


def get_price_primary(coin: str) -> Optional[dict]:
    """
    Primary: CoinMarketCap quotes. Requires CMC_API_KEY.
    Returns dict with keys: price, change_24h_pct, volume_24h, range_low, range_high
    """
    if not CMC_API_KEY:
        return None
    symbol = coin
    if coin == "HYPE":
        try:
            url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/map"
            r = requests.get(url, params={"symbol":"HYPE"}, headers={"X-CMC_PRO_API_KEY": CMC_API_KEY}, timeout=20)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                return None
            ids = [str(d["id"]) for d in data if d.get("symbol","").upper()=="HYPE"]
            if not ids:
                return None
            q = requests.get("https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest",
                             params={"id": ",".join(ids), "convert":"USD"},
                             headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
                             timeout=20)
            q.raise_for_status()
            qd = q.json().get("data", {})
            best, best_mcap = None, -1
            for _id, obj in qd.items():
                quote = obj["quote"]["USD"]
                mcap = quote.get("market_cap") or -1
                if mcap > best_mcap:
                    best_mcap = mcap
                    best = quote
            if not best:
                return None
            return {
                "price": best.get("price"),
                "change_24h_pct": best.get("percent_change_24h"),
                "volume_24h": best.get("volume_24h"),
                "range_low": best.get("low_24h"),
                "range_high": best.get("high_24h"),
                "source": "CMC"
            }
        except Exception as e:
            record_failure("cmc_hype", str(e))
            return None
    else:
        try:
            url = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"
            r = requests.get(url, params={"symbol": symbol, "convert":"USD"},
                             headers={"X-CMC_PRO_API_KEY": CMC_API_KEY}, timeout=20)
            r.raise_for_status()
            data = r.json()["data"][symbol][0]["quote"]["USD"]
            return {
                "price": data.get("price"),
                "change_24h_pct": data.get("percent_change_24h"),
                "volume_24h": data.get("volume_24h"),
                "range_low": data.get("low_24h"),
                "range_high": data.get("high_24h"),
                "source": "CMC"
            }
        except Exception as e:
            record_failure("cmc_price", str(e))
            return None

def get_price_fallback(coin: str) -> Optional[dict]:
    try:
        global CG_IDS_CACHE
        if CG_IDS_CACHE is None:
            CG_IDS_CACHE = get_coin_ids_map()

        if coin == "HYPE":
            cid = resolve_hype_symbol_coingecko()
            if not cid:
                return None
        else:
            canonical = {"BTC":"bitcoin", "ETH":"ethereum", "SOL":"solana"}
            cid = canonical.get(coin)
            if not cid:
                ids = CG_IDS_CACHE.get(coin.upper(), [])
                if not ids:
                    return None
                cid = ids[0]

        r = cg_get("/coins/markets", {"vs_currency":"usd","ids":cid})
        arr = r.json()
        if not arr:
            return None
        x = arr[0]
        return {
            "price": x.get("current_price"),
            "change_24h_pct": x.get("price_change_percentage_24h"),
            "volume_24h": x.get("total_volume"),
            "range_low": x.get("low_24h"),
            "range_high": x.get("high_24h"),
            "source": "CoinGecko"
        }
    except Exception as e:
        record_failure("coingecko_price", str(e))
        return None

def get_technicals_via_coingecko(coin: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        if coin == "HYPE":
            cid = resolve_hype_symbol_coingecko()
            if not cid:
                return (None, None, None)
        else:
            canonical = {"BTC":"bitcoin", "ETH":"ethereum", "SOL":"solana"}
            cid = canonical.get(coin)
            if not cid:
                return (None, None, None)

        r = cg_get(f"/coins/{cid}/market_chart", {
            "vs_currency": "usd",
            "days": "90",
            "interval": "daily"
        })
        prices = r.json().get("prices", [])
        closes = [p[1] for p in prices]
        if len(closes) < 50:
            return (None, None, None)
        s = pd.Series(closes, dtype=float)
        sma20 = float(s.rolling(20).mean().iloc[-1])
        sma50 = float(s.rolling(50).mean().iloc[-1])
        delta = s.diff()
        up = delta.clip(lower=0)
        down = -1*delta.clip(upper=0)
        roll_up = up.rolling(14).mean()
        roll_down = down.rolling(14).mean()
        rs = roll_up / (roll_down.replace(0, np.nan))
        rsi = 100 - (100 / (1 + rs))
        rsi14 = float(rsi.iloc[-1])
        return (rsi14, sma20, sma50)
    except Exception as e:
        record_failure("technicals", str(e))
        return (None, None, None)

# ---------------------------- Sentiment ----------------------------

analyzer = SentimentIntensityAnalyzer()

def score_texts(texts: List[str]) -> Tuple[float, float]:
    """Return (score, confidence). Score in [-1,1]; confidence as abs(mean_compound)."""
    if not texts:
        return (0.0, 0.0)
    scores = [analyzer.polarity_scores(t)["compound"] for t in texts]
    mean = float(np.mean(scores))
    conf = float(min(1.0, abs(mean)))
    return (mean, conf)

def fetch_twitter_texts(query: str, limit: int = 50, *, coin: str | None = None, override: bool = False) -> List[str]:
    # Patuh kuota/rotasi
    c = (coin or "").upper()
    if not twitter_allowed_today(c, override=override):
        return []

    # 1) X API (jika bearer tersedia)
    headers = {"Authorization": f"Bearer {TWITTER_BEARER}"} if TWITTER_BEARER else None
    if headers:
        try:
            # Ambil kecil saja & pilih yang paling “bermakna”
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": f"{query} -is:retweet lang:en",
                "max_results": 10,  # kecil supaya hemat
                "tweet.fields": "created_at,lang,public_metrics"
            }
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                record_failure("twitter_rate", r.text[:200])
                return []
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                return []

            # Pilih 1 tweet paling tinggi engagement (hemat 1 kuota)
            data.sort(key=lambda t: sum((t.get("public_metrics") or {}).values()), reverse=True)
            picked = data[:1]  # <-- hanya 1 post/hit
            texts = [d.get("text","") for d in picked if d.get("text")]

            # Catat konsumsi kuota (1 post pull)
            if texts:
                x_quota_consume(1)
            return texts
        except Exception as e:
            record_failure("twitter_api", str(e))
            # jangan jatuh ke scraper kalau bearer ada; tetap hemat kuota
            return []

    # 2) Jika DISABLE_TWITTER_SCRAPE, langsung skip
    if DISABLE_TWITTER_SCRAPE:
        return []

    # 3) Fallback snscrape (akan jarang dipakai; tetap 1 kuota agar konsisten)
    try:
        import subprocess, json as _json, hashlib
        since_ts = int((datetime.now(timezone.utc)-timedelta(hours=24)).timestamp())
        cmd = ["snscrape", "--jsonl", "--max-results", "10", "twitter-search", f"{query} lang:en since_time:{since_ts}"]
        out = subprocess.check_output(cmd, text=True, timeout=25)
        best_line = None
        best_score = -1
        for line in out.splitlines():
            obj = _json.loads(line)
            pm = obj.get("retweetCount", 0) + obj.get("likeCount", 0) + obj.get("replyCount", 0) + obj.get("quoteCount", 0)
            if pm > best_score:
                best_score = pm
                best_line = obj.get("content", "")
        if best_line:
            x_quota_consume(1)
            return [best_line]
        return []
    except Exception as e:
        record_failure("snscrape", str(e)[:300])
        return []


def fetch_news_texts(coin: str, limit: int = 20) -> Tuple[List[str], List[str]]:
    q_terms = {
        "BTC": ["Bitcoin"],
        "ETH": ["Ethereum"],
        "SOL": ["Solana"],
        "HYPE": ["HYPE crypto"]
    }
    terms = q_terms.get(coin, [coin])
    texts, headlines = [], []
    if NEWSAPI_KEY:
        try:
            q = " OR ".join(terms)
            url = "https://newsapi.org/v2/everything"
            from_dt = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            params = {"q": q, "language": "en", "from": from_dt, "sortBy": "publishedAt", "pageSize": limit, "apiKey": NEWSAPI_KEY}
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            arts = r.json().get("articles", [])
            seen_titles = set()
            for a in arts:
                title = a.get("title","").strip()
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    headlines.append(title)
                    txt = " ".join([title, a.get("description","") or ""])
                    texts.append(txt.strip())
            return texts, headlines[:5]
        except Exception as e:
            record_failure("newsapi", str(e))
    try:
        q = " OR ".join(terms)
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={q.replace(' ', '%20')}&hl=en-US&gl=US&ceid=US:en")
        seen = set()
        for entry in feed.entries[:limit]:
            title = entry.title.strip()
            if title not in seen:
                seen.add(title)
                headlines.append(title)
                texts.append(title + " " + (getattr(entry, "summary", "") or ""))
        return texts, headlines[:5]
    except Exception as e:
        record_failure("gn_rss", str(e))
        return [], []

# ---------------------------- Alert Logic ----------------------------

@dataclass
class Technicals:
    rsi14: Optional[float] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None

@dataclass
class SentimentBlock:
    composite: float = 0.0
    twitter_x: Dict = None
    news: Dict = None

def compute_sentiment(coin: str) -> SentimentBlock:
    tw_texts = []
    for term in [coin, {"BTC":"Bitcoin","ETH":"Ethereum","SOL":"Solana"}.get(coin, coin)]:
        tw_texts.extend(fetch_twitter_texts(str(term), limit=40))
    tw_texts = list({t:1 for t in tw_texts}.keys())
    tw_score, tw_conf = score_texts(tw_texts)
    news_texts, headlines = fetch_news_texts(coin, limit=20)
    news_score, news_conf = score_texts(news_texts)
    weights, comps = [], []
    if tw_texts:
        weights.append(max(0.01, tw_conf))
        comps.append(tw_score)
    if news_texts:
        weights.append(max(0.01, news_conf))
        comps.append(news_score)
    composite = 0.0 if not weights else float(np.average(comps, weights=weights))
    return SentimentBlock(
        composite=composite,
        twitter_x={"score": tw_score, "confidence": tw_conf, "sample_size": len(tw_texts)},
        news={"score": news_score, "confidence": news_conf, "top_headlines": headlines}
    )

def compute_sentiment_light(coin: str) -> SentimentBlock:
    # versi ringan: hanya news (tanpa twitter) agar hemat kuota saat non-alert
    news_texts, headlines = fetch_news_texts(coin, limit=20)
    news_score, news_conf = score_texts(news_texts)
    return SentimentBlock(
        composite=news_score if news_texts else 0.0,
        twitter_x={"score": 0.0, "confidence": 0.0, "sample_size": 0},
        news={"score": news_score, "confidence": news_conf, "top_headlines": headlines}
    )

def compute_sentiment_with_override(coin: str) -> SentimentBlock:
    # ambil 1 tweet override + news
    tw_texts = fetch_twitter_texts(coin, limit=10, coin=coin, override=True)
    tw_score, tw_conf = score_texts(tw_texts)
    news_texts, headlines = fetch_news_texts(coin, limit=20)
    news_score, news_conf = score_texts(news_texts)
    weights, comps = [], []
    if tw_texts: weights.append(max(0.01, tw_conf)); comps.append(tw_score)
    if news_texts: weights.append(max(0.01, news_conf)); comps.append(news_score)
    composite = float(np.average(comps, weights=weights)) if weights else 0.0
    return SentimentBlock(
        composite=composite,
        twitter_x={"score": tw_score, "confidence": tw_conf, "sample_size": len(tw_texts)},
        news={"score": news_score, "confidence": news_conf, "top_headlines": headlines}
    )

def should_alert(coin: str, change_24h_pct: float, current_price: float) -> Tuple[bool, int, Optional[float]]:
    if change_24h_pct is None or current_price is None:
        return (False, 0, None)
    if change_24h_pct < MIN_ALERT_STEP_PCT:
        return (False, 0, None)
    date_k = today_key()
    with ENGINE.begin() as conn:
        row = conn.execute(text("SELECT first_baseline_price, last_step, last_alert_ts FROM alerts WHERE coin=:c AND date_key=:d"),
                           {"c": coin, "d": date_k}).mappings().first()
        now_ts = now_local()
        if row:
            last_alert_ts = row["last_alert_ts"]
            if last_alert_ts:
                last_dt = datetime.fromisoformat(last_alert_ts)
                if (now_ts - last_dt) < timedelta(minutes=COOLDOWN_MINUTES):
                    return (False, 0, None)
            step = int(change_24h_pct // MIN_ALERT_STEP_PCT)
            if step <= (row["last_step"] or 0):
                return (False, 0, None)
            return (True, step, None)
        else:
            step = int(change_24h_pct // MIN_ALERT_STEP_PCT)
            if step < 1:
                return (False, 0, None)
            return (True, step, current_price)

def update_alert_state(coin: str, step: int, baseline_price: Optional[float] = None):
    date_k = today_key()
    now_ts = now_local().isoformat()
    with ENGINE.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM alerts WHERE coin=:c AND date_key=:d"), {"c": coin, "d": date_k}).first()
        if row:
            conn.execute(text("UPDATE alerts SET last_step=:s, last_alert_ts=:ts WHERE coin=:c AND date_key=:d"),
                         {"s": step, "ts": now_ts, "c": coin, "d": date_k})
        else:
            conn.execute(text("""INSERT INTO alerts(coin, date_key, first_baseline_price, last_step, last_alert_ts)
                                 VALUES(:c, :d, :bp, :s, :ts)"""),
                         {"c": coin, "d": date_k, "bp": baseline_price, "s": step, "ts": now_ts})

# ---------------------------- Decision Engine ----------------------------

def decide_entry(change_24h_pct: float, sentiment: SentimentBlock, price: float,
                 tech: Technicals, volume_24h: Optional[float], vol_vs_30d: Optional[float]=None) -> Tuple[str, str]:
    consider = False
    reasons = []
    if change_24h_pct is not None and change_24h_pct >= 2.0:
        if sentiment.composite >= 0.15:
            consider = True
            reasons.append(f"composite sentiment {sentiment.composite:+.2f}")
        elif vol_vs_30d is not None and vol_vs_30d >= 1.2:
            consider = True
            reasons.append(f"24h volume vs 30d avg {vol_vs_30d:.2f}×")
        elif tech.sma20 and tech.sma50 and price and (price > tech.sma20 >= tech.sma50):
            consider = True
            reasons.append("price>SMA20 and SMA20≥SMA50")
    if consider:
        rationale = "Price up ≥2% in 24h; " + ", ".join(reasons) + "."
        return "Consider Entry", rationale
    else:
        rationale = "Positive price move but lacking confirming sentiment/volume/MA trend; skipping for now."
        return "No Entry", rationale

# ---------------------------- Email helpers ----------------------------

def build_alert_email_html(coin: str, info: dict, decision: str, rationale: str, ts_local: str) -> str:
    pct = info["change_24h_pct"]
    price = info["price"]
    vol = info["volume_24h"]
    rsi = info["technicals"].rsi14
    sma20 = info["technicals"].sma20
    sma50 = info["technicals"].sma50
    sent = info["sentiment"]
    headlines = sent.news.get("top_headlines", []) if sent and sent.news else []

    # small helpers
    def fmt_money(x):
        return "-" if x in (None, "") else f"${x:,.2f}"
    def fmt_num(x, nd=2):
        return "-" if x in (None, "") else f"{x:.{nd}f}"

    raw_json = html.escape(json.dumps({
        "coin": coin,
        "price": price,
        "change_24h_pct": pct,
        "volume_24h": vol,
        "technicals": {"rsi14": rsi, "sma20": sma20, "sma50": sma50},
        "sentiment": {
            "composite": sent.composite if sent else None,
            "twitter_x": sent.twitter_x if sent else None,
            "news": sent.news if sent else None
        },
        "decision": decision,
        "rationale": rationale
    }, ensure_ascii=False, indent=2))

    return f"""
<!doctype html>
<html>
  <body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; color:#0f172a; background:#ffffff; padding:16px;">
    <div style="max-width:640px;margin:auto;border:1px solid #e5e7eb;border-radius:12px;padding:20px;">
      <h2 style="margin:0 0 8px 0;">🚀 Crypto Price Alert</h2>
      <div style="font-size:14px;color:#64748b;">{ts_local} (Asia/Jakarta)</div>

      <hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0;" />

      <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;font-size:15px;line-height:1.5;">
        <tr>
          <td style="padding:6px 0;width:40%;color:#64748b;">Coin</td>
          <td style="padding:6px 0;"><strong>{coin}</strong></td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;">Current Price</td>
          <td style="padding:6px 0;"><strong>{fmt_money(price)}</strong> <span style="color:#64748b;">USD</span></td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;">Change (24h)</td>
          <td style="padding:6px 0;"><strong>{fmt_num(pct, 2)}%</strong></td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;">Volume (24h)</td>
          <td style="padding:6px 0;">{fmt_money(vol)} <span style="color:#64748b;">USD</span></td>
        </tr>
      </table>

      <h3 style="margin:16px 0 8px 0;">📊 Technicals</h3>
      <ul style="margin:0 0 12px 18px;padding:0;">
        <li>RSI(14): <strong>{fmt_num(rsi, 2)}</strong></li>
        <li>SMA20: <strong>{fmt_num(sma20, 4)}</strong></li>
        <li>SMA50: <strong>{fmt_num(sma50, 4)}</strong></li>
      </ul>

      <h3 style="margin:16px 0 8px 0;">🧠 Decision</h3>
      <div><strong>{decision}</strong></div>
      <div style="color:#334155;margin-top:4px;">{html.escape(rationale)}</div>

      <h3 style="margin:16px 0 8px 0;">📰 Top Headlines</h3>
      {"<div style='color:#64748b;'>No fresh headlines.</div>" if not headlines else ""}
      <ol style="margin:0 0 12px 18px;padding:0;">
        {"".join(f"<li>{html.escape(h)}</li>" for h in headlines)}
      </ol>

      <details style="margin-top:12px;">
        <summary style="cursor:pointer;color:#2563eb;">Show raw JSON</summary>
        <pre style="white-space:pre-wrap;background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;font-size:12px;">{raw_json}</pre>
      </details>

      <p style="color:#64748b;font-size:12px;margin-top:16px;">This is not financial advice. Do your own research.</p>
    </div>
  </body>
</html>
    """.strip()

def send_email_multipart(subject: str, plain_text: str, html_body: str):
    """Send multipart email (text + html)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

# (keperluan lain tetap tersedia)
def send_email(subject: str, body_json: dict):
    """Legacy single-part JSON (dipakai untuk kebutuhan lain kalau mau)."""
    msg = MIMEText(json.dumps(body_json, ensure_ascii=False, indent=2))
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

# ---------------------------- Core Tick ----------------------------

def fetch_all_for_coin(coin: str) -> Optional[dict]:
    price_block = get_price_primary(coin) or get_price_fallback(coin)
    if not price_block:
        return None
    rsi14, sma20, sma50 = get_technicals_via_coingecko(coin)
    tech = Technicals(rsi14=rsi14, sma20=sma20, sma50=sma50)

    sent = compute_sentiment(coin)
    return {
        "price": price_block["price"],
        "change_24h_pct": price_block["change_24h_pct"],
        "volume_24h": price_block["volume_24h"],
        "range_low": price_block["range_low"],
        "range_high": price_block["range_high"],
        "source": price_block["source"],
        "technicals": tech,
        "sentiment": sent
    }

def make_email_payload(coin: str, info: dict, decision: str, rationale: str) -> Tuple[str, dict]:
    ts_local = now_local().strftime("%Y-%m-%d %H:%M")
    pct = info["change_24h_pct"]
    subject = f"[ALERT] {coin} +{pct:.2f}% in 24h — {decision} — {ts_local} Asia/Jakarta"
    body = {
      "timestamp_local": f"{ts_local} (Asia/Jakarta)",
      "coin": coin,
      "price": { "current": round(info['price'], 8) if info['price'] else None, "currency": "USD" },
      "change_24h_pct": round(pct, 4) if pct is not None else None,
      "range_24h": { "low": info["range_low"] or None, "high": info["range_high"] or None },
      "volume_24h": { "value": info["volume_24h"] or None, "unit": "USD" },
      "technicals": {
        "rsi14": info["technicals"].rsi14,
        "sma20": info["technicals"].sma20,
        "sma50": info["technicals"].sma50
      },
      "sentiment": {
        "composite": round(info["sentiment"].composite, 4),
        "twitter_x": { 
            "score": round(info["sentiment"].twitter_x["score"], 4), 
            "confidence": round(info["sentiment"].twitter_x["confidence"], 4), 
            "sample_size": info["sentiment"].twitter_x["sample_size"]
        },
        "news": { 
            "score": round(info["sentiment"].news["score"], 4), 
            "confidence": round(info["sentiment"].news["confidence"], 4), 
            "top_headlines": info["sentiment"].news["top_headlines"] 
        }
      },
      "decision": decision,
      "rationale": rationale,
      "choices": [
        {
          "name": "Market Entry (Now)",
          "enabled": decision == "Consider Entry",
          "entry_price": round(info['price'], 8) if info['price'] else None,
          "stop_loss": { "type": "percent", "value": -3.0 },
          "take_profit": [
            { "type": "percent", "value": 4.0 },
            { "type": "percent", "value": 8.0 }
          ],
          "position_size": "moderate" if info["sentiment"].composite >= 0.30 else "small",
          "notes": "Momentum + confirming context; consider tight risk with staged targets." if decision == "Consider Entry" else "Disabled when decision is 'No Entry'."
        },
        {
          "name": "Pullback Buy",
          "enabled": decision == "Consider Entry",
          "limit_price": "current * (1 - 0.010 to 0.015)",
          "stop_loss": { "type": "percent", "value": -3.0 },
          "take_profit": [
            { "type": "percent", "value": 4.0 },
            { "type": "percent", "value": 8.0 }
          ],
          "position_size": "moderate" if info["sentiment"].composite >= 0.30 else "small",
          "notes": "Enter on minor dip while trend holds; reduces slippage risk." if decision == "Consider Entry" else "Disabled when decision is 'No Entry'."
        },
        {
          "name": "Wait / No Trade",
          "enabled": True,
          "notes": "Re-check next tick; act if sentiment strengthens (≥ +0.15) or volume expands (≥1.2× 30d avg)."
        }
      ],
      "disclaimer": "This is not financial advice. Do your own research."
    }
    return subject, body

def tick():
    logging.info("==== Tick start ====")
    for coin in COINS:
        try:
            # --- Fase 0: data harga/teknikal
            price_block = get_price_primary(coin) or get_price_fallback(coin)
            if not price_block:
                logging.info(f"[{coin}] Skipping — data unavailable")
                continue
            rsi14, sma20, sma50 = get_technicals_via_coingecko(coin)
            tech = Technicals(rsi14=rsi14, sma20=sma20, sma50=sma50)

            # --- Fase 1: sentiment ringan (tanpa Twitter) agar hemat kuota
            sent = compute_sentiment_light(coin)

            info = {
                "price": price_block["price"],
                "change_24h_pct": price_block["change_24h_pct"],
                "volume_24h": price_block["volume_24h"],
                "range_low": price_block["range_low"],
                "range_high": price_block["range_high"],
                "source": price_block["source"],
                "technicals": tech,
                "sentiment": sent
            }

            # Keputusan alert murni dari %24h & cooldown/step
            should, step, baseline_price = should_alert(coin, info["change_24h_pct"], info["price"])
            if not should:
                pct = info.get("change_24h_pct")
                if pct is None or pct < MIN_ALERT_STEP_PCT:
                    logging.info(f"[{coin}] Skipping email alert — price change below threshold ({(pct or 0):.2f}%)")
                else:
                    logging.info(f"[{coin}] Skipping email alert — cooldown/new step not met")
                continue

            # --- Fase 2 (override): sebelum kirim email, ambil 1 tweet untuk coin ini
            info["sentiment"] = compute_sentiment_with_override(coin)

            decision, rationale = decide_entry(
                info["change_24h_pct"], info["sentiment"], info["price"],
                info["technicals"], info["volume_24h"], None
            )
            subject, payload = make_email_payload(coin, info, decision, rationale)
            send_email(subject, payload)
            update_alert_state(coin, step, baseline_price)
            logging.info(f"Alert sent for {coin}: step {step}, decision={decision}")
        except Exception as e:
            record_failure("tick", f"{coin}: {e}")
    logging.info(f"[X-Quota] {x_quota_status()} | rotation today: {sorted(list(rotation_coins_for_today()))}")
    logging.info("==== Tick end ====")

def daily_digest():
    try:
        date_k = today_key()
        with ENGINE.begin() as conn:
            rows = conn.execute(
                text("SELECT coin, last_step FROM alerts WHERE date_key=:d"),
                {"d": date_k}
            ).mappings().all()

        step_map = {r["coin"]: r["last_step"] for r in rows}

        lines = []
        lines.append(f"Daily Digest — {now_local().strftime('%Y-%m-%d %H:%M')} Asia/Jakarta")
        lines.append("")
        lines.append(f"{'Coin':<6} {'Last Step':<10} {'Note'}")
        lines.append("-" * 50)
        for coin in COINS:
            last_step = step_map.get(coin, "-")
            note = (
                f"Highest +{last_step*2}% step today"
                if isinstance(last_step, int) and last_step > 0
                else "No alert triggered"
            )
            lines.append(f"{coin:<6} {str(last_step):<10} {note}")

        body_text = "\n".join(lines)

        msg = MIMEText(body_text, "plain")
        subject = f"[DIGEST] Summary — {now_local().strftime('%Y-%m-%d %H:%M')} Asia/Jakarta"
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

        logging.info("Daily digest sent.")
    except Exception as e:
        record_failure("daily_digest", str(e))


def main():
    logging.info("Starting Crypto Alert Agent…")
    scheduler = BackgroundScheduler(timezone=str(TZ))
    scheduler.add_job(tick, IntervalTrigger(minutes=REFRESH_MINUTES, timezone=str(TZ)), max_instances=1, coalesce=True)
    hh, mm = map(int, DAILY_DIGEST_LOCALTIME.split(":"))
    scheduler.add_job(daily_digest, CronTrigger(hour=hh, minute=mm, timezone=str(TZ)), max_instances=1, coalesce=True)
    scheduler.start()
    try:
        tick()  # run once at start
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()

if __name__ == "__main__":
    main()
