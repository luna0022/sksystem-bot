
#!/usr/bin/env python3
"""SK System Telegram signal bot — стратегия MACD-DCA, спот, только лонг.

Universe: топ-N по капитализации (CoinGecko), пересечённый с USDT-парами KuCoin.
Timeframe: 4ч (переключается кнопками 15m/1h/4h/1d). MacdTracker: MACD(12,26,9),
покупка лота $500 на пересечении ВВЕРХ линии MACD через сигнальную, пока MACD < 0
(макс. 6 лотов), продажа ВСЕГО на пересечении ВНИЗ, пока MACD > 0.
Плеча нет — ликвидаций не существует.
Web dashboard (app/web.py) reads data/state.json and writes data/cmd.json;
this bot applies those commands. Env + data/config.json override strategy
params live.
"""
import io
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.strategy_macd import MParams, MacdTracker
from app.market import (fetch_klines, fetch_history, load_universe, build_exchange_map,
                        fetch_klines_ex, ping_exchange, EXCHANGES,
                        fetch_binance_top_usdt, fetch_kucoin_top_usdt)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sksystem")

DATA = os.environ.get("DATA_DIR", "/app/data")
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
VALID_TFS = ("15m", "1h", "4h", "1d")
TF = os.environ.get("TF", "4h")
if TF not in VALID_TFS:
    TF = "4h"
SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
TOP_N = int(os.environ.get("TOP_N", "200"))
MAX_POS = int(os.environ.get("MAX_POSITIONS", "10"))
DEMO = os.environ.get("DEMO", "true").lower() == "true"
WARMUP_BARS = 400

NL = chr(10)
KB = {"keyboard": [["📊 Daily", "💰 Profit", "🏦 Balance"],
                   ["📈 Status", "📋 Table", "🚀 Performance"],
                   ["🔢 Count", "▶️ Start", "⏹ Stop"],
                   ["❓ Help", "🔄 Reset"]],
      "resize_keyboard": True}

os.makedirs(DATA, exist_ok=True)


def cfg() -> MParams:
    """Params from env, overridden by data/config.json (live-tunable)."""
    p = MParams(
        lot_usd=float(os.environ.get("LOT_USD", "300")),
        max_lots=int(os.environ.get("MAX_LOTS", "6")),
        fast=int(os.environ.get("MACD_FAST", "12")),
        slow=int(os.environ.get("MACD_SLOW", "26")),
        signal=int(os.environ.get("MACD_SIGNAL", "9")),
        fee_pct=float(os.environ.get("FEE_PCT", "0.1")),
    )
    path = os.path.join(DATA, "config.json")
    if os.path.exists(path):
        try:
            for k, v in json.load(open(path)).items():
                if hasattr(p, k):
                    setattr(p, k, v)
        except Exception:
            pass
    return p


# ------------------------------------------------------------ telegram


COMMANDS = [
    ("menu", "меню кнопками"), ("start", "запустить сканер"), ("stop", "остановить сканер"),
    ("status", "статус и позиции"), ("table", "таблица позиций"), ("profit", "профит и баланс"),
    ("balance", "демо-баланс"), ("daily", "статистика за день"), ("performance", "по парам"),
    ("count", "счётчики"), ("reset", "сброс статистики"), ("help", "справка"),
    ("tf", "смена таймфрейма: /tf 15m, 1h, 4h или 1d"),
]


def tg_set_commands():
    """Persistent Telegram Menu button (the one in the input field)."""
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
                      json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]},
                      timeout=20)
    except Exception as e:
        log.error("setMyCommands: %s", e)


INLINE_MENU = {
    "inline_keyboard": [
        [{"text": "📊 Daily", "callback_data": "/daily"},
         {"text": "💰 Profit", "callback_data": "/profit"},
         {"text": "🏦 Balance", "callback_data": "/balance"}],
        [{"text": "📈 Status", "callback_data": "/status"},
         {"text": "📋 Table", "callback_data": "/table"},
         {"text": "🚀 Performance", "callback_data": "/performance"}],
        [{"text": "▶️ Start", "callback_data": "/start"},
         {"text": "⏹ Stop", "callback_data": "/stop"},
         {"text": "🔢 Count", "callback_data": "/count"}],
        [{"text": "⏱ 15м", "callback_data": "/tf 15m"},
         {"text": "⏱ 1ч", "callback_data": "/tf 1h"},
         {"text": "⏱ 4ч", "callback_data": "/tf 4h"},
         {"text": "⏱ 1д", "callback_data": "/tf 1d"}],
        [{"text": "🔄 Reset", "callback_data": "/reset"}],
    ]
}


def tg_send_menu():
    for cid in CHATS:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": cid, "text": "🎛 <b>Меню</b> — нажми кнопку:",
                                "parse_mode": "HTML", "reply_markup": INLINE_MENU},
                          timeout=20)
        except Exception as e:
            log.error("tg menu: %s", e)


def tg_send(text, kb=False):
    payload = {"chat_id": "", "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if kb:
        payload["reply_markup"] = json.dumps(KB)
    for cid in CHATS:
        payload["chat_id"] = cid
        for a in range(3):
            try:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                              data=payload, timeout=20)
                break
            except Exception as e:
                log.error("tg send %d: %s", a + 1, e)
                time.sleep(2)


def tg_photo(png, caption):
    for cid in CHATS:
        for a in range(3):
            try:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                              files={"photo": ("chart.png", png, "image/png")},
                              data={"chat_id": cid, "caption": caption,
                                    "parse_mode": "HTML"}, timeout=30)
                break
            except Exception as e:
                log.error("tg photo %d: %s", a + 1, e)
                time.sleep(2)


def tg_updates(offset):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 5}, timeout=10)
        return r.json().get("result", [])
    except Exception:
        return []


def fmt_px(v):
    return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"


# ------------------------------------------------------------- charts

def make_chart(symbol, df, pos):
    """Свечи + панель MACD, маркеры лотов и средняя цена входа."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from app.strategy_macd import MacdTracker
    d = df.tail(150).reset_index(drop=True)
    up, dn = "#26a69a", "#ef5350"
    fig, (ax, axm) = plt.subplots(2, 1, figsize=(6.4, 5.2), dpi=110,
                                  sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")
    axm.set_facecolor("#0d1117")
    for i, r in d.iterrows():
        cnd = up if r["close"] >= r["open"] else dn
        ax.plot([i, i], [r["low"], r["high"]], color=cnd, lw=0.8, zorder=2)
        lo_, hi_ = min(r["open"], r["close"]), max(r["open"], r["close"])
        ax.add_patch(Rectangle((i - 0.32, lo_), 0.64, max(hi_ - lo_, 1e-9),
                               facecolor=cnd, edgecolor=cnd, zorder=3))
    # маркеры лотов + средняя цена
    off = max(len(df) - 150, 0)
    lots = (pos or {}).get("lots") or []
    invested = sum(l for _, l in lots)
    if lots:
        qty = sum(l / px for px, l in lots)
        avg = invested / qty
        xs = [k - off for k, (px, _l) in enumerate(lots)]
        ax.scatter(xs, [px for px, _l in lots], marker="^", s=70,
                   color="#ffd54a", zorder=5, label=f"buy lots ({len(lots)})")
        ax.hlines(avg, -1, len(d), colors="#ffd54a", linestyles="--", lw=1.4)
        ax.text(len(d) - 0.5, avg, f"avg {fmt_px(avg)}", color="#ffd54a",
                fontsize=8, va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.25", fc="#0d1117", ec="#ffd54a", lw=0.6))
    # MACD
    tr = MacdTracker(cfg())
    tr.armed = False
    hv, lv = d["high"].values, d["low"].values
    ov, cv = d["open"].values, d["close"].values
    for i in range(len(d)):
        tr.update(hv, lv, ov, cv, i)
    m = [x if x is not None else float("nan") for x in tr.macd]
    sg = [x if x is not None else float("nan") for x in tr.sig]
    axm.plot(m, color="#58a6ff", lw=1.2, label="MACD")
    axm.plot(sg, color="#e6edf3", lw=1.0, label="signal")
    axm.axhline(0, color="#8b949e", lw=0.6)
    axm.fill_between(range(len(d)), m, sg,
                     where=[(not np.isnan(a)) and (not np.isnan(b)) and a >= b
                            for a, b in zip(m, sg)],
                     color=up, alpha=0.25)
    axm.fill_between(range(len(d)), m, sg,
                     where=[(not np.isnan(a)) and (not np.isnan(b)) and a < b
                            for a, b in zip(m, sg)],
                     color=dn, alpha=0.25)
    ax.set_title(f"#{symbol} · {TF} · MACD-DCA (спот)", color="#e6edf3", fontsize=10, loc="left")
    axm.legend(loc="upper left", fontsize=7, labelcolor="#8b949e", frameon=False)
    for a_ in (ax, axm):
        a_.tick_params(colors="#8b949e", labelsize=7)
        for sp in a_.spines.values():
            sp.set_color("#30363d")
    ax.set_xlim(-1, len(d))
    ax.grid(color="#21262d", lw=0.5)
    axm.grid(color="#21262d", lw=0.5)
    fig.text(0.01, 0.01, "SK System | MACD-DCA | покупка $500 на пересечении вверх MACD<0 · продажа всего на пересечении вниз MACD>0 | спот",
             color="#8b949e", fontsize=7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- book

class Book:
    def __init__(self):
        self.open = {}
        self.per_pair = {}
        self.wins = self.losses = self.tps = self.sls = self.times = 0
        self.balance = float(os.environ.get("CAPITAL_USD", "2000"))
        self.start_balance = float(os.environ.get("CAPITAL_USD", "2000"))
        self.trade_no = 0
        self.signals = 0
        self.scans = 0
        self.daily = {}
        self.path = os.path.join(DATA, "trades.json")
        if os.path.exists(self.path):
            try:
                st = json.load(open(self.path))
                self.__dict__.update({k: st.get(k, getattr(self, k, None)) for k in st})
            except Exception:
                pass

    def save(self):
        json.dump({k: v for k, v in self.__dict__.items() if k != "path"},
                  open(self.path, "w"), default=str)



def log_event(text):
    try:
        path = os.path.join(DATA, "events.json")
        evs = json.load(open(path)) if os.path.exists(path) else []
        evs.append({"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"), "text": text})
        json.dump(evs[-60:], open(path, "w"))
    except Exception:
        pass


book = Book()
STATE = {"paused": False, "universe": [], "last_scan": None,
         "cache": {}, "trackers": {}, "processed": {}, "signals": 0, "entries": 0,
         "exch_map": {}, "exch": {}, "alive": set(EXCHANGES), "map_ts": 0}


# -------------------------------------------------------------- engine glue

def exchange_for(symbol):
    """best live exchange for the symbol (availability re-checked every round)."""
    opts = STATE.get("exch_map", {}).get(symbol) or ["binance"]
    alive = STATE.get("alive", set(EXCHANGES))
    for ex in EXCHANGES:
        if ex in opts and ex in alive:
            return ex
    return None


def load_symbol(symbol):
    ex = exchange_for(symbol) or "binance"
    try:
        df = fetch_klines_ex(symbol, TF, ex, limit=WARMUP_BARS + 50)
    except Exception:
        df = fetch_history(symbol, TF)
    STATE["exch"][symbol] = ex
    return df.tail(WARMUP_BARS).reset_index(drop=True) if not df.empty else df


def process_symbol(symbol, p):
    cache = STATE["cache"]
    # инициализируем трекер, если его нет (новая монета или сброс через reset:
    # reset чистит trackers/processed, но cache остаётся)
    if symbol not in STATE["trackers"]:
        df0 = load_symbol(symbol)
        cache[symbol] = df0
        tr = MacdTracker(p)
        if not df0.empty:
            # прогрев: MACD и пивоты считаются по истории, события не эмитим
            tr.armed = False
            hv, lv = df0["high"].values, df0["low"].values
            ov, cv = df0["open"].values, df0["close"].values
            for i in range(len(df0) - 1):
                tr.update(hv, lv, ov, cv, i)
            tr.armed = True
        STATE["trackers"][symbol] = tr
        STATE["processed"][symbol] = len(df0) - 2
        return
    df = cache[symbol]
    ex = STATE["exch"].get(symbol) or "binance"
    try:
        new = fetch_klines_ex(symbol, TF, ex, limit=5).tail(5)
    except Exception:
        new = fetch_klines(symbol, TF, limit=3)
    if not new.empty:
        df = (pd.concat([df, new]).drop_duplicates("open_time")
              .sort_values("open_time").tail(WARMUP_BARS).reset_index(drop=True))
        cache[symbol] = df
    last_closed = len(df) - 2
    if last_closed <= STATE["processed"].get(symbol, -1):
        return
    hv, lv = df["high"].values, df["low"].values
    ov, cv = df["open"].values, df["close"].values
    tr = STATE["trackers"][symbol]

    def can_spend():
        return (book.balance >= p.lot_usd
                and (symbol in book.open or len(book.open) < MAX_POS))

    for i in range(STATE["processed"].get(symbol, 0) + 1, last_closed + 1):
        tr.can_buy = can_spend()
        for ev in tr.update(hv, lv, ov, cv, i):
            if ev[0] == "buy":
                _, px, info = ev
                buy_lot(symbol, px, info, df, p)
            elif ev[0] == "sell_all":
                _, px, info = ev
                sell_all(symbol, px, info, df, p)
    STATE["processed"][symbol] = last_closed


def _fees(notional, p):
    return notional * p.fee_pct / 100.0 * 2   # вход + выход


def buy_lot(symbol, px, info, df, p):
    """Докупка лота на пересечении вверх MACD ниже нуля (спот, демо-учёт кэшем)."""
    lot = float(info["lot_usd"])
    pos = book.open.get(symbol)
    if pos is None:
        pos = {"lots": [], "opened": time.time()}
        book.open[symbol] = pos
    fee = lot * p.fee_pct / 100.0
    pos["lots"].append([px, lot])
    book.balance -= lot + fee
    book.trade_no += 1
    book.signals += 1
    STATE["entries"] = STATE.get("entries", 0) + 1
    pp = book.per_pair.setdefault(symbol, {"signals": 0, "closed": 0, "wins": 0,
                                           "losses": 0, "pnl": 0.0})
    pp["signals"] += 1
    book.save()
    n = len(pos["lots"])
    invested = sum(l for _, l in pos["lots"])
    qty = sum(l / e for e, l in pos["lots"])
    avg = invested / qty
    log.info("buy %s lot #%d price=%s invested=%.2f", symbol, n, px, invested)
    log_event(f"BUY #{symbol} lot {n} @ {fmt_px(px)} (${lot:g})")
    tg_photo(make_chart(symbol, df, pos), NL.join([
        f"🟢 <b>MACD-DCA — покупка лота #{n}</b>",
        f"Trade #{book.trade_no}{' · DEMO' if DEMO else ''} · спот, без плеча",
        f"#{symbol} · {TF} · MACD <code>{info['macd']:.6g}</code> пересёк сигнальную ВВЕРХ (<0)",
        "",
        f"👉 Куплено на <code>{fmt_px(px)}</code> на ${lot:g}",
        f"📦 Всего лотов: {n} · вложено <b>${invested:g}</b> · средняя цена <code>{fmt_px(avg)}</code>",
        f"🏦 Balance: {book.balance:,.2f} USDT",
        "",
        f"Продам всё, когда MACD пересечёт сигнальную ВНИЗ выше нуля.",
    ]))


def sell_all(symbol, px, info, df, p):
    """Продажа всей позиции на пересечении вниз MACD выше нуля."""
    pos = book.open.get(symbol)
    if not pos or not pos["lots"]:
        return
    lots = pos["lots"]
    invested = sum(l for _, l in lots)
    proceeds = sum(l * px / e for e, l in lots)
    fee = proceeds * p.fee_pct / 100.0
    pnl = proceeds - fee - invested
    book.balance += proceeds - fee
    won = pnl > 0
    pp = book.per_pair.setdefault(symbol, {"signals": 0, "closed": 0, "wins": 0,
                                           "losses": 0, "pnl": 0.0})
    pp["closed"] += 1
    ret_pct = (proceeds - fee) / invested * 100 - 100
    pp["pnl"] += ret_pct
    if won:
        book.wins += 1
        pp["wins"] += 1
        book.tps += 1
    else:
        book.losses += 1
        pp["losses"] += 1
        book.sls += 1
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    book.daily.setdefault(day, {"n": 0, "pnl": 0.0})
    book.daily[day]["n"] += 1
    book.daily[day]["pnl"] += pnl
    dur = max(int(time.time() - pos["opened"]) // 3600, 1)
    total = book.wins + book.losses
    wr = f"{book.wins / total * 100:.0f}%" if total else "—"
    book.save()
    del book.open[symbol]
    log.info("sell_all %s px=%s invested=%.2f pnl=%+.2f", symbol, px, invested, pnl)
    log_event(f"SELL ALL #{symbol} @ {fmt_px(px)} {pnl:+.2f} USDT")
    tg_photo(make_chart(symbol, df, None), NL.join([
        f"{'✅' if won else '📉'} <b>MACD-DCA — продано ВСЁ</b> ({'профит' if won else 'убыток'})",
        f"#{symbol} · {TF} · MACD <code>{info['macd']:.6g}</code> пересёк сигнальную ВНИЗ (>0)",
        "",
        f"├ Лотов куплено: {len(lots)} на ${invested:g}",
        f"├ Продано по: <code>{fmt_px(px)}</code>",
        f"├ Выручка: ${proceeds - fee:,.2f} (комиссия {fee:.2f})",
        f"├ PnL цикла: <b>{pnl:+.2f} USDT</b> ({ret_pct:+.2f}%)",
        f"├ Держали: ~{dur} ч",
        f"└ 📊 Циклов: {total} ({book.wins}W / {book.losses}L) · winrate {wr} · balance {book.balance:,.2f} USDT",
    ]))


# -------------------------------------------------------------- commands

def cmd_status():
    lines = [
        "📈 <b>Status</b>",
        f"├ Scanner: {'⏹ paused' if STATE['paused'] else '▶️ running'}",
        f"├ Timeframe: {TF} · MACD-DCA спот · лот ${cfg().lot_usd:g} · макс. лотов {cfg().max_lots}",
        f"├ Universe: {len(STATE['universe'])} symbols",
        f"├ Last scan: {STATE.get('last_scan') or '—'}",
        f"├ Entries total: {STATE.get('entries', 0)}",
        f"└ Open positions: {len(book.open)}",
    ]
    for sym, p_ in book.open.items():
        age = max(int(time.time() - p_["opened"]) // 3600, 0)
        lots = p_.get("lots") or []
        invested = sum(l for _, l in lots)
        avg = invested / sum(l / px for px, l in lots) if lots else 0
        lines.append(f"   {sym} · {len(lots)} лотов · средняя {fmt_px(avg)} · вложено ${invested:g} · {age} ч")
    return NL.join(lines)


def cmd_table():
    if not book.open:
        return "📋 No open positions"
    rows = ["<code>Symbol       Lots   AvgEntry        Invested   Age</code>"]
    for sym, p_ in book.open.items():
        age = f"{max(int(time.time() - p_['opened']) // 3600, 0)}h"
        lots = p_.get("lots") or []
        invested = sum(l for _, l in lots)
        avg = invested / sum(l / px for px, l in lots) if lots else 0
        rows.append("<code>" + f"{sym:<14}{len(lots):<6}{fmt_px(avg):<15}"
                    f"${invested:<10g}{age}" + "</code>")
    return "📋 <b>Open positions</b>" + NL + NL.join(rows)


def cmd_profit():
    total = book.wins + book.losses
    wr = f"{book.wins/total*100:.0f}%" if total else "—"
    ret = (book.balance / book.start_balance - 1) * 100
    return NL.join(["💰 <b>Profit</b>",
                    f"├ Trades: {total} ({book.wins}W / {book.losses}L) · winrate {wr}",
                    f"├ TP: {book.tps} · SL: {book.sls} · time-out: {book.times}",
                    f"└ Balance: <b>{book.balance:,.2f} USDT</b> ({ret:+.2f}%)"])


def cmd_balance():
    ret = (book.balance / book.start_balance - 1) * 100
    return NL.join(["🏦 <b>Balance</b> (demo)",
                    f"├ Start: {book.start_balance:,.2f} USDT",
                    f"├ Now:   {book.balance:,.2f} USDT",
                    f"└ PnL:   {ret:+.2f}%"])


def cmd_daily():
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = book.daily.get(day, {"n": 0, "pnl": 0.0})
    return NL.join([f"📊 <b>Daily</b> ({day})",
                    f"├ Closed trades: {d['n']}",
                    f"└ PnL: {d['pnl']:+.3f}%"])


def cmd_performance():
    rows = ["<code>Pair           Sig  W   L   Winrate  PnL%</code>"]
    for sym, pp in sorted(book.per_pair.items(), key=lambda kv: -kv[1]["signals"]):
        t = pp["wins"] + pp["losses"]
        wr = f"{pp['wins']/t*100:.0f}%" if t else "—"
        rows.append("<code>" + f"{sym:<14}{pp['signals']:<4}{pp['wins']:<3}{pp['losses']:<3}{wr:<8}{pp['pnl']:+.2f}" + "</code>")
    return "🚀 <b>Performance per pair</b>" + NL + NL.join(rows[:20])


def cmd_help():
    return NL.join(["📖 <b>SK System bot commands</b>",
                    "├ 📊 Daily — today closed trades",
                    "├ 💰 Profit — total PnL + balance",
                    "├ 🏦 Balance — demo balance",
                    "├ 📈 Status — scanner state + open positions",
                    "├ 📋 Table — open positions table",
                    "├ 🚀 Performance — per-pair stats",
                    "├ 🔢 Count — scans / signals counters",
                    "├ ▶️ Start / ⏹ Stop — scan on/off",
                    "├ 🔄 Reset — clear stats & positions",
                    "└ ❓ Help — this list"])


def cmd_count():
    return NL.join(["🔢 <b>Count</b>",
                    f"├ Scans: {book.scans}",
                    f"├ Signals: {book.signals}",
                    f"├ Open: {len(book.open)}",
                    f"└ Closed: {book.wins + book.losses}"])


def do_reset():
    book.open.clear()
    book.per_pair.clear()
    book.wins = book.losses = book.tps = book.sls = book.times = 0
    book.balance = book.start_balance
    book.trade_no = book.signals = 0
    book.daily.clear()
    book.save()
    STATE["trackers"] = {}
    STATE["processed"] = {}


def set_tf(new):
    """Смена таймфрейма на лету. Трекеры и кэш свечей сбрасываются (прогрев
    заново), открытые демо-позиции закрываются без PnL по цене входа —
    иначе менеджер позиций (внутри трекера) потерял бы их."""
    global TF
    new = (new or "").strip().lower()
    if new not in VALID_TFS:
        return None
    if new == TF:
        return TF
    n_open = len(book.open)
    book.open.clear()          # маржа в демо не резервировалась — баланс не трогаем
    book.save()
    STATE["trackers"] = {}
    STATE["processed"] = {}
    STATE["cache"] = {}
    STATE["exch"] = {}
    TF = new
    log.info("timeframe switched to %s (%d open positions dropped)", TF, n_open)
    log_event(f"TF switched to {TF}")
    return TF


def dispatch(text):
    t = text.strip().lower()
    if t in ("/start", "▶️ start"):
        STATE["paused"] = False
        tg_send("▶️ Scanner <b>started</b>", kb=True)
    elif t in ("/stop", "⏹ stop"):
        STATE["paused"] = True
        tg_send("⏹ Scanner <b>paused</b>", kb=True)
    elif t in ("/help", "❓ help"):
        tg_send(cmd_help(), kb=True)
    elif t in ("/status", "📈 status"):
        tg_send(cmd_status(), kb=True)
    elif t in ("/table", "📋 table", "/status table"):
        tg_send(cmd_table(), kb=True)
    elif t in ("/profit", "💰 profit"):
        tg_send(cmd_profit(), kb=True)
    elif t in ("/balance", "🏦 balance"):
        tg_send(cmd_balance(), kb=True)
    elif t in ("/daily", "📊 daily"):
        tg_send(cmd_daily(), kb=True)
    elif t in ("/performance", "🚀 performance"):
        tg_send(cmd_performance(), kb=True)
    elif t in ("/count", "🔢 count"):
        tg_send(cmd_count(), kb=True)
    elif t in ("/reset", "🔄 reset"):
        do_reset()
        tg_send("🔄 Stats and demo balance <b>reset</b>", kb=True)
    elif t.startswith("/tf"):
        parts = t.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ""
        cur = set_tf(arg)
        if cur is None:
            tg_send("❓ Такого таймфрейма нет. Доступно: 15m · 1h · 4h · 1d", kb=True)
        elif cur == arg:
            tg_send(f"⏱ Таймфрейм переключён на <b>{cur}</b>. Трекеры и прогрев сброшены, "
                    f"открытые позиции закрыты без PnL. Ближайший цикл — прогрев заново.", kb=True)
        else:
            tg_send(f"⏱ Таймфрейм уже <b>{cur}</b>", kb=True)
    elif t in ("/menu", "menu", "меню"):
        tg_send_menu()


def command_poller():
    offset = 0
    while True:
        for upd in tg_updates(offset):
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if cb:
                data = cb.get("data") or ""
                try:
                    if data:
                        dispatch(data)
                    requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                        json={"callback_query_id": cb["id"]}, timeout=10)
                except Exception as e:
                    log.error("callback %r: %s", data, e)
                continue
            text = (upd.get("message") or {}).get("text") or ""
            if text:
                try:
                    dispatch(text)
                except Exception as e:
                    log.error("command %r: %s", text, e)
        time.sleep(3)


def webcmd_watcher():
    """Apply commands written by the web dashboard (data/cmd.json)."""
    path = os.path.join(DATA, "cmd.json")
    while True:
        try:
            if os.path.exists(path):
                cmd = json.load(open(path))
                os.remove(path)
                name = cmd.get("cmd")
                if name == "pause":
                    STATE["paused"] = True
                elif name == "resume":
                    STATE["paused"] = False
                elif name == "reset":
                    do_reset()
                elif name == "restart":
                    log.info("restart requested via web")
                    os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error("webcmd watcher: %s", e)
        time.sleep(3)


def write_state():
    try:
        json.dump({
            "paused": STATE["paused"],
            "last_scan": STATE.get("last_scan"),
            "scans": book.scans,
            "signals": book.signals,
            "entries": STATE.get("entries", 0),
            "balance": book.balance,
            "wins": book.wins, "losses": book.losses,
            "open": {s: {k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if isinstance(vv, (int, float, str))})
                         for k, v in p.items() if k != "lvl"}
                       for s, p in book.open.items()},
            "universe_size": len(STATE["universe"]),
        }, open(os.path.join(DATA, "state.json"), "w"), default=str)
    except Exception as e:
        log.error("write_state: %s", e)


# ---------------------------------------------------------------- main

STABLES = {"USDT", "USDC", "USDS", "DAI", "FDUSD", "TUSD", "USDE", "BUSD",
           "USDJ", "EURI", "XUSD", "BFUSD", "WUSD", "USDP", "FRAX", "LUSD",
           "GUSD", "PYUSD", "EURC", "AEUR", "USDSB", "USD1", "USDD", "SUSDE"}


def fetch_top_by_market_cap(n):
    """Топ-n по КАПИТАЛИЗАЦИИ (CoinGecko), оставляем только монеты с
    торгуемой USDT-парой на KuCoin. Стейблы и мелочь < $50млн капы — прочь."""
    cap = []
    for page in (1, 2):
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                         params={"vs_currency": "usd", "order": "market_cap_desc",
                                 "per_page": 250, "page": page}, timeout=30)
        r.raise_for_status()
        cap += [(c["symbol"].upper(), c.get("market_cap") or 0) for c in r.json()]
    r = requests.get("https://api.kucoin.com/api/v2/symbols", timeout=30)
    r.raise_for_status()
    kusdt = {s["baseCurrency"] for s in r.json()["data"]
             if s["quoteCurrency"] == "USDT" and s.get("enableTrading")}
    out = []
    for sym, mc in cap:
        if sym in STABLES or mc < 50_000_000:
            continue
        if sym in kusdt:
            out.append(sym + "USDT")
        if len(out) >= n:
            break
    return out


def build_universe():
    """(symbols, exchange). Вселенная — топ по КАПИТАЛИЗАЦИИ (CoinGecko),
    пересечённый с USDT-парами KuCoin (Binance с сервера всё равно гео-блок).
    Фолбэки: старые списки по обороту, затем кэш."""
    try:
        uni = fetch_top_by_market_cap(TOP_N)
        if uni:
            return uni, "kucoin"
    except Exception as e:
        log.error("coingecko cap universe failed: %s", e)
    try:
        uni = fetch_kucoin_top_usdt(TOP_N)
        if uni:
            return uni, "kucoin"
    except Exception as e:
        log.error("kucoin universe failed: %s", e)
    return load_universe(TOP_N, os.path.join(DATA, "universe.json")), "binance"


def main():
    log.info("SK System bot start. top_n=%s max_pos=%s demo=%s tf=%s", TOP_N, MAX_POS, DEMO, TF)
    uni, ex = build_universe()
    ex_map = {s: [ex] for s in uni}
    STATE["exch_map"] = ex_map
    STATE["map_ts"] = time.time()
    STATE["universe"] = sorted(ex_map.keys())
    log.info("universe: %d symbols via %s (CoinGecko top-%d by market cap)",
             len(STATE["universe"]), ex, TOP_N)
    try:
        probe = fetch_klines("BTCUSDT", TF, limit=2)
        log.info("binance api check OK (%d rows)", len(probe))
    except Exception as e:
        log.error("binance api check FAILED: %s", e)
    tg_set_commands()
    tg_send(NL.join(["🤖 <b>SK System bot started</b>",
                     f"├ Universe: CoinGecko top-{len(STATE['universe'])} by market cap",
                     "├ Timeframe: " + TF + " · MACD-DCA (спот, без плеча)",
                     "├ Покупка $" + f"{cfg().lot_usd:g}" + " на пересечении вверх MACD<0, макс. лотов " + f"{cfg().max_lots}",
                     "├ Продажа ВСЕГО на пересечении вниз MACD>0",
                     f"├ Капитал ${book.start_balance:g} · макс. {MAX_POS} монет в работе · топ-{TOP_N} по капитализации",
                     f"└ Demo balance: {book.balance:,.2f} USDT"]), kb=True)
    threading.Thread(target=command_poller, daemon=True).start()
    threading.Thread(target=webcmd_watcher, daemon=True).start()
    while True:
        time.sleep(300)   # 5-minute rounds: exchange health + new closed bars
        STATE["alive"] = {ex for ex in EXCHANGES if ping_exchange(ex)}
        if time.time() - STATE["map_ts"] > 6 * 3600:
            try:
                uni, ex = build_universe()
                if uni:
                    STATE["exch_map"] = {s: [ex] for s in uni}
                    STATE["universe"] = sorted(STATE["exch_map"].keys())
                    STATE["map_ts"] = time.time()
                    log.info("universe rebuilt: %d symbols via %s", len(uni), ex)
            except Exception as e:
                log.error("universe rebuild: %s", e)
        if STATE["paused"]:
            continue
        p = cfg()
        t0 = time.time()
        for i, symbol in enumerate(STATE["universe"]):
            try:
                process_symbol(symbol, p)
                if symbol not in STATE["cache"] and (i + 1) % 25 == 0:
                    log.info("warmup %d/%d", i + 1, len(STATE["universe"]))
            except Exception as e:
                log.exception("scan %s: %s", symbol, e)
        book.scans += 1
        book.save()
        write_state()
        STATE["last_scan"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
        log.info("scan round done in %.0fs | open=%d | entries=%d",
                 time.time() - t0, len(book.open), STATE.get("entries", 0))


if __name__ == "__main__":
    main()
