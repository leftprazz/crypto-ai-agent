#!/usr/bin/env python3
"""
Crypto Alert Agent
- Monitors BTC, ETH, SOL, HYPE (largest cap 'HYPE')
- Refreshes every N minutes, keeps rolling 24h state in SQLite
- Fires alert emails with exact subject & JSON body schema
- Sends daily digest at 23:50 Asia/Jakarta
"""

import os, time, math, json, smtplib, logging, hashlib
from email.mime.text import MIMEText
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
"""

with ENGINE.begin() as conn:
    for stmt in SCHEMA_SQL.strip().split(";\n\n"):
        if stmt.strip():
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

# ---------------------------- Data Sources ----------------------------

# CoinGecko request helper with API key header
def cg_get(path: str, params: dict | None = None):
    headers = {}
    if CG_API_KEY:
        # kirim dua-duanya; CG akan terima salah satunya sesuai tier
        headers["x-cg-pro-api-key"] = CG_API_KEY
        headers["x-cg-demo-api-key"] = CG_API_KEY
    url = f"{CG_API_BASE}{path}"
    r = requests.get(url, params=params or {}, headers=headers, timeout=20)
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
        # CMC lookup by symbol, pick largest market cap
        try:
            url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/map"
            r = requests.get(url, params={"symbol":"HYPE"}, headers={"X-CMC_PRO_API_KEY": CMC_API_KEY}, timeout=20)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                return None
            # For map results, we need quotes to get mcap; fetch quotes for all ids then choose largest mcap
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

        r = cg_get(f"/coins/{cid}/market_chart", {"vs_currency":"usd","days":"2","interval":"hourly"})
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

def fetch_twitter_texts(query: str, limit: int = 50) -> List[str]:
    headers = {"Authorization": f"Bearer {TWITTER_BEARER}"} if TWITTER_BEARER else None
    if headers:
        try:
            url = "https://api.twitter.com/2/tweets/search/recent"
            params = {
                "query": f"{query} -is:retweet lang:en",
                "max_results": min(100, limit),
                "tweet.fields": "created_at,lang"
            }
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                record_failure("twitter_rate", r.text[:200])
                return []
            r.raise_for_status()
            data = r.json().get("data", [])
            return [d.get("text","") for d in data]
        except Exception as e:
            record_failure("twitter_api", str(e))
            return []

    # Fallback: CLI snscrape sekali saja
    try:
        import subprocess, json as _json
        since_ts = int((datetime.now(timezone.utc)-timedelta(hours=24)).timestamp())
        cmd = ["snscrape", "--jsonl", "--max-results", str(limit),
               "twitter-search", f"{query} lang:en since_time:{since_ts}"]
        out = subprocess.check_output(cmd, text=True)
        texts, seen = [], set()
        for line in out.splitlines():
            obj = _json.loads(line)
            content = obj.get("content","")
            h = hashlib.sha1(content.encode("utf-8")).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            texts.append(content)
        return texts
    except Exception as e:
        # stop di sini, jangan fallback -m snscrape.cli lagi
        record_failure("snscrape", str(e))
        return []


def fetch_news_texts(coin: str, limit: int = 20) -> Tuple[List[str], List[str]]:
    """
    Returns (texts, headlines) for the last 24h.
    """
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
    # Fallback: Google News RSS
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
    # Twitter
    tw_texts = []
    for term in [coin, {"BTC":"Bitcoin","ETH":"Ethereum","SOL":"Solana"}.get(coin, coin)]:
        tw_texts.extend(fetch_twitter_texts(str(term), limit=40))
    tw_texts = list({t:1 for t in tw_texts}.keys())  # de-dupe
    tw_score, tw_conf = score_texts(tw_texts)
    # News
    news_texts, headlines = fetch_news_texts(coin, limit=20)
    news_score, news_conf = score_texts(news_texts)
    # Composite (weighted by confidence and source presence)
    weights = []
    comps = []
    if tw_texts:
        weights.append(max(0.01, tw_conf))
        comps.append(tw_score)
    if news_texts:
        weights.append(max(0.01, news_conf))
        comps.append(news_score)
    if not weights:
        composite = 0.0
    else:
        composite = float(np.average(comps, weights=weights))
    return SentimentBlock(
        composite=composite,
        twitter_x={"score": tw_score, "confidence": tw_conf, "sample_size": len(tw_texts)},
        news={"score": news_score, "confidence": news_conf, "top_headlines": headlines}
    )

def should_alert(coin: str, change_24h_pct: float, current_price: float) -> Tuple[bool, int, Optional[float]]:
    """
    Returns (should_alert, step, baseline_price_if_first).
    step = 1 for +2%, 2 for +4%, etc.
    """
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
            # compute step
            step = int(change_24h_pct // MIN_ALERT_STEP_PCT)
            if step <= (row["last_step"] or 0):
                return (False, 0, None)
            return (True, step, None)
        else:
            # first alert today
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
    """
    Returns (decision, rationale)
    """
    consider = False
    reasons = []
    if change_24h_pct is not None and change_24h_pct >= 2.0:
        # sentiment or volume or SMA filter
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

# ---------------------------- Email ----------------------------

def send_email(subject: str, body_json: dict):
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
            info = fetch_all_for_coin(coin)
            if not info:
                continue
            should, step, baseline_price = should_alert(coin, info["change_24h_pct"], info["price"])
            if not should:
                continue
            decision, rationale = decide_entry(info["change_24h_pct"], info["sentiment"], info["price"],
                                               info["technicals"], info["volume_24h"], None)
            subject, payload = make_email_payload(coin, info, decision, rationale)
            send_email(subject, payload)
            update_alert_state(coin, step, baseline_price)
            logging.info(f"Alert sent for {coin}: step {step}, decision={decision}")
        except Exception as e:
            record_failure("tick", f"{coin}: {e}")
    logging.info("==== Tick end ====")

def daily_digest():
    try:
        date_k = today_key()
        with ENGINE.begin() as conn:
            rows = conn.execute(text("SELECT coin, last_step FROM alerts WHERE date_key=:d"), {"d": date_k}).mappings().all()
        summary = []
        for r in rows:
            summary.append({"coin": r["coin"], "last_step": r["last_step"]})
        subject = f"[DIGEST] Summary — {now_local().strftime('%Y-%m-%d %H:%M')} Asia/Jakarta"
        body = {
            "timestamp_local": f"{now_local().strftime('%Y-%m-%d %H:%M')} (Asia/Jakarta)",
            "summary": summary,
            "note": "For each coin, 'last_step' corresponds to the highest +2% step that triggered today."
        }
        send_email(subject, body)
        logging.info("Daily digest sent.")
    except Exception as e:
        record_failure("daily_digest", str(e))

def main():
    logging.info("Starting Crypto Alert Agent…")
    scheduler = BackgroundScheduler(timezone=str(TZ))
    # periodic tick
    scheduler.add_job(tick, IntervalTrigger(minutes=REFRESH_MINUTES, timezone=str(TZ)), max_instances=1, coalesce=True)
    # daily digest at HH:MM local
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
