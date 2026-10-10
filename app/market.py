
"""Market data: Binance REST (live bot) + CoinGecko (universe) + the public
data.binance.vision archive (backtests). No API keys required anywhere."""
import io
import json
import os
import time
import zipfile
import urllib.request

import pandas as pd
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}
API_BASES = ["https://api.binance.com", "https://api1.binance.com",
             "https://api2.binance.com", "https://api3.binance.com",
             "https://api4.binance.com"]
VISION = "https://data.binance.vision"

DEFAULT_UNIVERSE = (
    "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,TRXUSDT,"
    "AVAXUSDT,LINKUSDT,TONUSDT,DOTUSDT,LTCUSDT,UNIUSDT,NEARUSDT,ICPUSDT,"
    "APTUSDT,FILUSDT,ARBUSDT,OPUSDT,SUIUSDT,SEIUSDT,ATOMUSDT,XTZUSDT,"
    "HBARUSDT,VETUSDT,ALGOUSDT,INJUSDT")


def _get_bytes(url, timeout=60, tries=3):
    hosts = [VISION, "https://data-api.binance.vision"]
    last = None
    for host in hosts:
        u = url.replace(VISION, host, 1)
        for k in range(tries):
            try:
                r = requests.get(u, headers=HEADERS, timeout=timeout)
                r.raise_for_status()
                return r.content
            except Exception as e:
                last = e
                time.sleep(1.0 + k)
    raise last


def _to_df(rows):
    if isinstance(rows, dict):
        raise RuntimeError(f"unexpected Binance response: {str(rows)[:200]}")
    if not rows:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df.columns = ["open_time", "open", "high", "low", "close", "volume",
                  "close_time", "qvol", "trades", "tbb", "tbq", "ignore"][:len(df.columns)]
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["open_time", "open", "high", "low", "close", "volume"]]


def fetch_klines(symbol, interval="1h", start_ms=None, limit=1000):
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if start_ms:
        params["startTime"] = int(start_ms)
    last = None
    for base in API_BASES:
        try:
            r = requests.get(f"{base}/api/v3/klines", params=params,
                             headers=HEADERS, timeout=25)
            if r.status_code != 200:
                last = f"{base}: HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(0.4)
                continue
            return _to_df(r.json())
        except Exception as e:
            last = f"{base}: {type(e).__name__} {e}"
            time.sleep(0.4)
    raise RuntimeError(f"all Binance API bases failed: {last}")


def fetch_history(symbol, interval="1h", since=None, pause=0.15):
    """Paginated history since `since` (tz-aware datetime or None = latest only)."""
    start = int(since.timestamp() * 1000) if since is not None else None
    out = []
    while True:
        df = fetch_klines(symbol, interval, start_ms=start, limit=1000)
        if df.empty:
            break
        out.append(df)
        start = int(df["open_time"].iloc[-1].timestamp() * 1000) + 1
        if len(df) < 1000:
            break
        time.sleep(pause)
    if not out:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    full = pd.concat(out).drop_duplicates("open_time").sort_values("open_time")
    if since is not None:
        full = full[full["open_time"] >= pd.Timestamp(since)]
    return full.reset_index(drop=True)


# --------------------------------------------------------------- universe

def fetch_spot_usdt():
    r = requests.get("https://api.binance.com/api/v3/exchangeInfo",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {s["symbol"] for s in r.json()["symbols"]
            if s["symbol"].endswith("USDT") and s["status"] == "TRADING"
            and s.get("isSpotTradingAllowed", True)}


def fetch_coingecko_top(n):
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           f"?vs_currency=usd&order=market_cap_desc&per_page={n}&page=1")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [c["symbol"].upper() for c in r.json()]


def load_universe(top_n, cache_file):
    if os.path.exists(cache_file):
        try:
            cached = json.load(open(cache_file))
            if time.time() - cached.get("ts", 0) < 24 * 3600 and cached.get("symbols"):
                return cached["symbols"]
        except Exception:
            pass
    try:
        spot = fetch_spot_usdt()
        symbols = [f"{s}USDT" for s in fetch_coingecko_top(top_n) if f"{s}USDT" in spot]
        if not symbols:
            raise RuntimeError("empty universe")
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        json.dump({"ts": time.time(), "symbols": symbols}, open(cache_file, "w"))
        return symbols
    except Exception:
        return DEFAULT_UNIVERSE.split(",")


# ---------------------------------------------------- vision archive (bt)

def _vision_df(url):
    raw = _get_bytes(url)
    if url.endswith(".zip"):
        zf = zipfile.ZipFile(io.BytesIO(raw))
        raw = zf.read(zf.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None)
    df = df.iloc[:, :6]
    df.columns = ["open_time", "open", "high", "low", "close", "volume"]
    unit = "us" if df["open_time"].iloc[0] > 10**14 else "ms"  # archive switched to us ~2025
    df["open_time"] = pd.to_datetime(df["open_time"], unit=unit, utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


def download_vision(symbol, interval, months, daily_days=None):
    parts = []
    def grab(url):
        try:
            parts.append(_vision_df(url))
        except Exception:
            pass
    for m in months:
        grab(f"{VISION}/data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{m}.zip")
        time.sleep(0.05)
    for d in daily_days or []:
        grab(f"{VISION}/data/spot/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d}.zip")
    if not parts:
        raise RuntimeError(f"no archive files for {symbol} {interval}")
    full = pd.concat(parts).drop_duplicates("open_time").sort_values("open_time")
    return full.reset_index(drop=True)


def load_cached_vision(symbol, interval, cache_dir, months, daily_days):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}.csv")
    if os.path.exists(path):
        return pd.read_csv(path, parse_dates=["open_time"])
    df = download_vision(symbol, interval, months, daily_days)
    df.to_csv(path, index=False)
    return df


# ============================================================ multi-exchange
# Unified public-API access: Binance + KuCoin + Bybit spot. No keys needed.
# Symbol naming is unified to "BTCUSDT" everywhere; each exchange adapter
# translates to its own format.

EXCHANGES = ["binance", "kucoin", "bybit"]


def kucoin_symbols_usdt():
    r = requests.get("https://api.kucoin.com/api/v2/symbols", timeout=30)
    r.raise_for_status()
    return {s["symbol"].replace("-", "") for s in r.json()["data"]
            if s.get("enableTrading") and s["symbol"].endswith("-USDT")}


def bybit_symbols_usdt():
    out, cursor = set(), ""
    while True:
        params = {"category": "spot", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = requests.get("https://api.bybit.com/v5/market/instruments-info",
                         params=params, timeout=30)
        r.raise_for_status()
        d = r.json()["result"]
        for s in d["list"]:
            if s["symbol"].endswith("USDT") and s["status"] == "Trading":
                out.add(s["symbol"])
        cursor = d.get("nextPageCursor") or ""
        if not cursor:
            return out


def build_exchange_map(top_n=200):
    """symbol -> list of exchanges that trade it (spot USDT)."""
    try:
        tops = fetch_coingecko_top(top_n)
    except Exception:
        tops = [s.replace("USDT", "") for s in DEFAULT_UNIVERSE.split(",")]
    uni = {f"{s.upper()}USDT" for s in tops}
    mp = {}
    for ex, fn in (("binance", fetch_spot_usdt), ("kucoin", kucoin_symbols_usdt),
                   ("bybit", bybit_symbols_usdt)):
        try:
            for s in fn() & uni:
                mp.setdefault(s, []).append(ex)
        except Exception:
            continue
    return mp


def _to_df6(rows):
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c_ in ["open", "high", "low", "close", "volume"]:
        df[c_] = df[c_].astype(float)
    return df


def fetch_klines_ex(symbol, interval, exchange, limit=200):
    """Unified klines. interval: 1h / 4h / 1d."""
    if exchange == "binance":
        df = fetch_klines(symbol, interval, limit=limit)
        return df[["open_time", "open", "high", "low", "close", "volume"]]
    if exchange == "kucoin":
        tmap = {"1h": "1hour", "4h": "4hour", "1d": "1day"}
        r = requests.get("https://api.kucoin.com/api/v1/market/candles",
                         params={"type": tmap[interval], "symbol": symbol[:-4] + "-USDT"},
                         timeout=30)
        r.raise_for_status()
        rows = [[int(float(k[0]) * 1000), float(k[1]), float(k[3]), float(k[4]), float(k[2]), float(k[5])]
                for k in reversed(r.json()["data"])]  # time, open, high, low, close, vol
        return _to_df6(rows).tail(limit)
    if exchange == "bybit":
        imap = {"1h": "60", "4h": "240", "1d": "D"}
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params={"category": "spot", "symbol": symbol,
                                 "interval": imap[interval], "limit": limit}, timeout=30)
        r.raise_for_status()
        rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
                for k in reversed(r.json()["result"]["list"])]
        return _to_df6(rows)
    raise ValueError(exchange)


def ping_exchange(exchange):
    try:
        if exchange == "binance":
            requests.get("https://api.binance.com/api/v3/ping", timeout=8)
        elif exchange == "kucoin":
            requests.get("https://api.kucoin.com/api/v1/timestamp", timeout=8)
        else:
            requests.get("https://api.bybit.com/v5/market/time", timeout=8)
        return True
    except Exception:
        return False
