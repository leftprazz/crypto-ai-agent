# Crypto Alert Agent (BTC, ETH, SOL, HYPE)

Monitors price momentum and 24-hour sentiment for BTC, ETH, SOL, and the largest-market-cap token with ticker `HYPE`.
- Refresh every 10 minutes, rolling 24h window
- Triggers on ≥ +2% 24h move (with step thresholds +2%, +4%, …), 60 min cooldown
- Combines Twitter/X and News sentiment; optional RSI(14), SMA(20/50)
- Sends alert emails with the exact subject & JSON body you specified
- 23:50 Asia/Jakarta daily digest

## Quick start
1) Create `.env` from `.env.example` and fill the keys.
2) `pip install -r requirements.txt`
3) Run: `python main.py`

Or via Docker:
```
docker compose up --build -d
```

## Notes
- Primary price: CoinMarketCap (CMC) or TradingView (placeholder). Fallback: CoinGecko.
- For technicals, the agent tries CoinGecko market chart to compute RSI/SMA if CMC/TV don’t provide OHLC. If unavailable, fields are set to `null`.
- Sentiment uses Twitter API v2 (preferred) or `snscrape` fallback (no API key), and NewsAPI (preferred) or Google News RSS fallback.
- All timestamps use Asia/Jakarta (UTC+7).
