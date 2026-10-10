
#!/usr/bin/env python3
"""SK System backtest.

  python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --months 6
  python backtest.py --symbols BTCUSDT --months 12 --tp1 2.5 --tp2 5
"""
import argparse
import os
import time
from datetime import datetime, timezone

import pandas as pd

from app.engine_ob import ObParams, ObTracker
from app.market import load_cached_vision


def months_between(y0, m0, y1, m1):
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def run(df, start, p):
    hv = df["high"].values
    lv = df["low"].values
    ov = df["open"].values
    cv = df["close"].values
    tr = ObTracker(p)
    equity = 1000.0
    eq = []
    trades = []
    fee, slip = p.fee_pct / 100.0, p.slippage_pct / 100.0
    n = len(df)
    start_i = next(i for i in range(n) if df["open_time"].iat[i] >= start)
    pos = None
    for i in range(start_i, n):
        for ev in tr.update(hv, lv, ov, cv, i):
            if ev[0] == "entry":
                _, d, entry, sl, tp, box = ev
                qty = p.margin_usd * p.leverage / entry
                pos = {"dir": d, "entry": entry, "qty": qty}
            elif ev[0] == "exit" and pos is not None:
                _, px, reason = ev
                px = px * (1 - slip * pos["dir"])
                pnl = (px - pos["entry"]) * pos["dir"] * pos["qty"]
                pnl -= (pos["entry"] + px) * pos["qty"] * fee
                equity += pnl
                ret = (px - pos["entry"]) / pos["entry"] * 100 * pos["dir"]
                trades.append({"side": "L" if pos["dir"] == 1 else "S",
                               "exit": reason, "pnl_pct": ret})
                pos = None
        eq.append(equity)
    tr_df = pd.DataFrame(trades)
    m = {}
    if len(tr_df):
        wins = tr_df[tr_df["pnl_pct"] > 0]["pnl_pct"].sum()
        losses = -tr_df[tr_df["pnl_pct"] <= 0]["pnl_pct"].sum()
        eqs = pd.Series(eq)
        m = {"trades": len(tr_df), "winrate": round((tr_df["pnl_pct"] > 0).mean() * 100, 1),
             "pf": round(wins / losses, 2) if losses > 0 else float("inf"),
             "ret_pct": round((equity / 1000 - 1) * 100, 2),
             "maxdd": round(((eqs.cummax() - eqs) / eqs.cummax() * 100).max(), 2),
             "by_exit": tr_df["exit"].value_counts().to_dict()}
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--tf", choices=["1h", "4h"], default="4h")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--cache", default="./data")
    ap.add_argument("--tp1", type=float, default=None)
    ap.add_argument("--tp2", type=float, default=None)
    args = ap.parse_args()
    p = ObParams()
    if args.tp1 is not None:
        p.tp1_pct = args.tp1
    if args.tp2 is not None:
        p.tp2_pct = args.tp2
    start = pd.Timestamp.now(tz="UTC") - pd.DateOffset(months=args.months)
    today = pd.Timestamp.now(tz="UTC").normalize()
    months = [m for m in months_between(2024, 6, today.year, today.month) if m < f"{today:%Y-%m}"]
    days = [(today - pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, today.day + 1)]
    for sym in args.symbols:
        t0 = time.time()
        df = load_cached_vision(sym, args.tf, args.cache, months, days)
        m = run(df, start, p)
        print(f"== {sym} {args.tf}: {m}  ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
