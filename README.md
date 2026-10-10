
# SK System — Telegram signal bot

Impulse + Fibonacci retracement strategy (port of the TradingView "SK Fib R"
indicator) with percent-based money management. Binance spot data, top-100
market-cap universe, hourly scan, chart images to Telegram, web dashboard.

## Logic
1. Find the last uncorrected impulse A->B (split at 0.500 touch; quality:
   length 3..150 bars, size >= 2 ATR, >=55% directional candles).
2. Wait for the correction into the zone 0.500-0.786 of the impulse.
3. Entry on close back beyond 0.559. Stop beyond 0.786 (+0.05 ATR).
   Invalid: body close beyond 0.786 / full zone cross / 200 bars expiry.
4. Exits (user rules): TP1 +2.5% (50%), TP2 +5% (25%), runner 25% with
   trailing stop -> breakeven, +1% from +5%, +2% from +8%; timeout 72 h.

## Run
    cp .env.example .env   # fill TELEGRAM_TOKEN / TELEGRAM_CHAT_ID / WEB_TOKEN
    docker compose up -d --build
    docker compose logs -f sksystem-bot

Web dashboard: http://<server>:8080  (open with #<WEB_TOKEN> in the URL)

## Backtest
    docker compose run --rm --no-deps --entrypoint python sksystem-bot backtest.py --months 6

## Live tuning
Web dashboard "Save params" writes data/config.json — the bot picks it up
without a restart (tp/trail percents, min_atr_mult, quality_filter, ...).
