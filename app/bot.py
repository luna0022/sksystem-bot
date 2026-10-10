
#!/usr/bin/env python3
"""SK System Telegram signal bot.

Universe: top-N by market cap (CoinGecko) x active USDT spot pairs (Binance).
Every hour, right after the 1h candle closes, each symbol's Tracker is fed the
new bar: impulse scan -> zone 0.5-0.786 -> trigger 0.559 -> entry on close.
Open positions are managed by the %-rules (TP1 50% / TP2 25% / trailing SL).

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

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.engine_ob import ObParams, ObTracker
from app.market import (fetch_klines, fetch_history, load_universe, build_exchange_map,
                        fetch_klines_ex, ping_exchange, EXCHANGES)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sksystem")

DATA = os.environ.get("DATA_DIR", "/app/data")
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
TF = os.environ.get("TF", "4h")
if TF not in ("1h", "4h"):
    TF = "4h"
SECONDS = {"1h": 3600, "4h": 14400}
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


def cfg() -> ObParams:
    """Params from env, overridden by data/config.json (live-tunable)."""
    p = ObParams(
        sl_buffer_pct=float(os.environ.get("SL_BUFFER_PCT", "2.0")),
        min_rr=float(os.environ.get("MIN_RR", "0.5")),
        timeout_bars=int(os.environ.get("TIMEOUT_BARS", "36")),
        leverage=float(os.environ.get("LEVERAGE", "3.0")),
        risk_pct=float(os.environ.get("RISK_PCT", "1.0")),
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
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    d = df.tail(48).reset_index(drop=True)
    up, dn = "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(6.4, 4.2), dpi=110)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#10231a" if pos["dir"] == 1 else "#2a1518")
    for i, r in d.iterrows():
        cnd = up if r["close"] >= r["open"] else dn
        ax.plot([i, i], [r["low"], r["high"]], color=cnd, lw=0.8, zorder=2)
        lo_, hi_ = min(r["open"], r["close"]), max(r["open"], r["close"])
        ax.add_patch(Rectangle((i - 0.32, lo_), 0.64, max(hi_ - lo_, 1e-9),
                               facecolor=cnd, edgecolor=cnd, zorder=3))
    off = len(df) - 48
    x_box = max(pos.get("box_born", off) - off, 0)
    bt, bb = max(pos["box_top"], pos["box_bottom"]), min(pos["box_top"], pos["box_bottom"])
    ax.add_patch(Rectangle((x_box, bb), len(d) - x_box + 4, bt - bb,
                           facecolor="#ffd54a", alpha=0.14,
                           edgecolor="#ffd54a", lw=1.0, zorder=1))
    ax.text(x_box + 0.3, bt, "block", color="#d29922", fontsize=8, va="bottom")
    xmin, xmax = -1, len(d)
    rows = [(pos["entry"], "#ffd54a", "ENTRY", "--"),
            (pos["tp"], up, "TP", "-"),
            (pos["sl"], dn, "SL", "-")]
    for y, c_, name, st in rows:
        pct = (y - pos["entry"]) / pos["entry"] * 100 * pos["dir"]
        ax.hlines(y, xmin, xmax, colors=c_, linestyles=st, lw=1.4)
        ax.text(xmax - 0.5, y, f"{name}  {fmt_px(y)}  ({pct:+.1f}%)", color=c_,
                fontsize=8, va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.25", fc="#0d1117", ec=c_, lw=0.6))
    side = "LONG" if pos["dir"] == 1 else "SHORT"
    ax.set_title(f"#{symbol} · {TF} · {side} · order block", color="#e6edf3",
                 fontsize=10, loc="left")
    ax.tick_params(colors="#8b949e", labelsize=7)
    for sp in ax.spines.values():
        sp.set_color("#30363d")
    ax.set_xlim(xmin, xmax)
    ax.grid(color="#21262d", lw=0.5)
    fig.text(0.01, 0.01, "SK System | order blocks | target: opposite block | SL: block + 2%",
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
        self.balance = 1000.0
        self.start_balance = 1000.0
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
    if symbol not in cache:
        cache[symbol] = load_symbol(symbol)
        STATE["processed"][symbol] = len(cache[symbol]) - 2
        STATE["trackers"][symbol] = ObTracker(p)
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
    if last_closed <= STATE["processed"][symbol]:
        return
    hv, lv = df["high"].values, df["low"].values
    ov, cv = df["open"].values, df["close"].values
    tr = STATE["trackers"][symbol]
    for i in range(STATE["processed"][symbol] + 1, last_closed + 1):
        for ev in tr.update(hv, lv, ov, cv, i):
            if ev[0] == "entry":
                _, d, entry, sl, tp, box = ev
                if STATE["paused"] or len(book.open) >= MAX_POS or symbol in book.open:
                    continue
                open_pos(symbol, d, entry, sl, tp, box, df, p)
            elif ev[0] == "exit" and symbol in book.open:
                _, px, reason = ev
                close_pos(symbol, book.open[symbol], float(px), reason, p)
    STATE["processed"][symbol] = last_closed


def open_pos(symbol, d, entry, sl, tp, box, df, p):
    side = "🟢 LONG 📈" if d == 1 else "🔴 SHORT 📉"
    qty = p.margin_usd * p.leverage / entry
    pos = {"dir": d, "entry": entry, "sl": sl, "tp": tp, "opened": time.time(),
           "box_top": box.top, "box_bottom": box.bottom, "box_born": box.born,
           "qty": qty, "margin": p.margin_usd}
    book.trade_no += 1
    book.signals += 1
    pp = book.per_pair.setdefault(symbol, {"signals": 0, "closed": 0, "wins": 0,
                                           "losses": 0, "pnl": 0.0})
    pp["signals"] += 1
    book.open[symbol] = pos
    book.save()
    log.info("signal %s %s entry=%s sl=%s tp=%s", symbol, d, entry, sl, tp)
    log_event("OPEN " + ("LONG" if d == 1 else "SHORT") + " #" + symbol + " @ " + fmt_px(entry))
    rr = abs(tp - entry) / max(abs(entry - sl), 1e-12)
    tg_photo(make_chart(symbol, df, pos), NL.join([
        "🔵 <b>New position</b>",
        f"Trade #{book.trade_no}{' · DEMO' if DEMO else ''} · вход ${p.margin_usd:g} · плечо x{p.leverage:g}",
        f"#{symbol} {TF}",
        f"💎 SIDE: {side}",
        "",
        f"👉 ENTRY: <code>{fmt_px(entry)}</code>",
        f"🧱 Block: <code>{fmt_px(min(box.top, box.bottom))} – {fmt_px(max(box.top, box.bottom))}</code>",
        f"🛑 SL: <code>{fmt_px(sl)}</code> (за блок +{p.sl_buffer_pct:g}%)",
        "",
        f"🎯 TP: <code>{fmt_px(tp)}</code> | 💲 {(tp-entry)/entry*100*d:+.2f}% · R/R 1:{rr:.1f}",
        f"(тейк — ближайший противоположный блок)",
        "",
        f"📊 Signals for pair: {pp['signals']}",
        f"✅ Closed trades: {pp['closed']}",
        "Prices in USDT. TP %: price movement, without leverage.",
    ]))


def close_pos(symbol, pos, px, reason, p):
    d = pos["dir"]
    ret = (px - pos["entry"]) / pos["entry"] * 100 * d
    pnl_usdt = pos.get("qty", 0) * (px - pos["entry"]) * d
    pnl_usdt -= (pos["entry"] + px) * pos.get("qty", 0) * p.fee_pct / 100.0
    won = ret > 0
    book.balance += pnl_usdt
    pp = book.per_pair.setdefault(symbol, {"signals": 0, "closed": 0, "wins": 0,
                                           "losses": 0, "pnl": 0.0})
    pp["closed"] += 1
    pp["pnl"] += ret
    if won:
        book.wins += 1
        pp["wins"] += 1
    else:
        book.losses += 1
        pp["losses"] += 1
    if reason == "SL":
        book.sls += 1
    elif reason == "TIME":
        book.times += 1
    else:
        book.tps += 1
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    book.daily.setdefault(day, {"n": 0, "pnl": 0.0})
    book.daily[day]["n"] += 1
    book.daily[day]["pnl"] += ret
    total = book.wins + book.losses
    wr = f"{book.wins/total*100:.0f}%" if total else "—"
    icon = "✅" if reason == "TP" else ("⏱" if reason == "TIME" else "🛑")
    title = {"TP": "TAKE-PROFIT (block reached)", "SL": "STOP-LOSS", "TIME": "TIME OUT"}[reason]
    dur = max(int(time.time() - pos["opened"]) // 3600, 1)
    book.save()
    del book.open[symbol]
    log_event(("CLOSE " if reason != "TP" else "TP HIT ") + "#" + symbol + " " + reason + " " + ("%+.2f%%" % ret))
    tg_send(NL.join([
        f"{icon} <b>Position closed — {title}</b>",
        f"#{symbol} {TF} · {'🟢 LONG 📈' if d == 1 else '🔴 SHORT 📉'}",
        f"👉 ENTRY: <code>{fmt_px(pos['entry'])}</code>",
        f"🔚 EXIT: <code>{fmt_px(px)}</code> ({ret:+.2f}%)",
        f"💰 PnL: <b>{ret:+.2f}%</b> ({pnl_usdt:+.2f} USDT · x{p.leverage:g})",
        f"🕐 Duration: ~{dur} h",
        f"📊 Trades: {book.wins}W / {book.losses}L · winrate {wr} · balance {book.balance:,.2f} USDT",
    ]), kb=True)


# -------------------------------------------------------------- commands

def cmd_status():
    lines = [
        "📈 <b>Status</b>",
        f"├ Scanner: {'⏹ paused' if STATE['paused'] else '▶️ running'}",
        f"├ Universe: {len(STATE['universe'])} symbols",
        f"├ Last scan: {STATE.get('last_scan') or '—'}",
        f"├ Entries total: {STATE.get('entries', 0)}",
        f"└ Open positions: {len(book.open)}",
    ]
    for sym, p_ in book.open.items():
        age = max(int(time.time() - p_["opened"]) // 3600, 0)
        side = "LONG" if p_["dir"] == 1 else "SHORT"
        lines.append(f"   {sym} {side} · entry {fmt_px(p_['entry'])} · age {age}h"
                     + (" · TP1✓" if p_.get("tp1_done") else "") + (" · TP2✓" if p_.get("tp2_done") else ""))
    return NL.join(lines)


def cmd_table():
    if not book.open:
        return "📋 No open positions"
    rows = ["<code>Symbol       Side    Entry          SL             TP1            TP2           Age</code>"]
    for sym, p_ in book.open.items():
        side = "LONG " if p_["dir"] == 1 else "SHORT"
        age = f"{max(int(time.time() - p_['opened']) // 3600, 0)}h"
        rows.append("<code>" + f"{sym:<12}{side:<7}{fmt_px(p_['entry']):<14}{fmt_px(p_['sl']):<14}"
                    f"{fmt_px(p_['tp1_px']):<14}{fmt_px(p_['tp2_px']):<13}{age}" + "</code>")
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

def main():
    log.info("SK System bot start. top_n=%s max_pos=%s demo=%s", TOP_N, MAX_POS, DEMO)
    ex_map = build_exchange_map(TOP_N)
    STATE["exch_map"] = ex_map
    STATE["map_ts"] = time.time()
    STATE["universe"] = sorted(ex_map.keys())
    log.info("universe: %d symbols via %s", len(STATE["universe"]),
             {ex: sum(1 for v in ex_map.values() if ex in v) for ex in EXCHANGES})
    try:
        probe = fetch_klines("BTCUSDT", TF, limit=2)
        log.info("binance api check OK (%d rows)", len(probe))
    except Exception as e:
        log.error("binance api check FAILED: %s", e)
    tg_set_commands()
    tg_send(NL.join(["🤖 <b>SK System bot started</b>",
                     f"├ Universe: top-{len(STATE['universe'])} by market cap",
                     "├ Timeframe: " + TF + " · scan every " + TF,
                     "├ Setup: impulse -> zone 0.5-0.786 -> trigger 0.559",
                     "├ TP1 50% (+2.5%) · TP2 25% (+5%) · runner 25% (trail SL)",
                     f"└ Demo balance: {book.balance:,.2f} USDT"]), kb=True)
    threading.Thread(target=command_poller, daemon=True).start()
    threading.Thread(target=webcmd_watcher, daemon=True).start()
    while True:
        time.sleep(300)   # 5-minute rounds: exchange health + new closed bars
        STATE["alive"] = {ex for ex in EXCHANGES if ping_exchange(ex)}
        if time.time() - STATE["map_ts"] > 6 * 3600:
            try:
                ex_map = build_exchange_map(TOP_N)
                if ex_map:
                    STATE["exch_map"] = ex_map
                    STATE["universe"] = sorted(ex_map.keys())
                    STATE["map_ts"] = time.time()
                    log.info("exchange map rebuilt: %d symbols", len(ex_map))
            except Exception as e:
                log.error("map rebuild: %s", e)
        if STATE["paused"]:
            continue
        p = cfg()
        p.timeout_bars = int(os.environ.get("TIMEOUT_BARS", str(p.timeout_bars)))
        t0 = time.time()
        for i, symbol in enumerate(STATE["universe"]):
            try:
                process_symbol(symbol, p)
                if symbol not in STATE["cache"] and (i + 1) % 10 == 0:
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
