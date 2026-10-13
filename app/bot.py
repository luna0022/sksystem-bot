
#!/usr/bin/env python3
"""SK System Telegram signal bot — стратегия 0->A->B (SK v6), спот, только лонг.

Universe: топ-200 USDT-пар Binance по суточному объёму (обновляется раз в 6 ч).
Timeframe: 1h. Каждый раунд (5 мин) новым закрытым свечам каждого символа
скармливается SKTracker: детекция 0->A->B -> сигнал в точке B (касание зоны
0.5-0.786) -> вход $100 с капитала $1000 (макс. 10 позиций).
Money management: TP1 +2% на 30%, TP2 1.618 на 30%, TP3 2.0 на 40%,
SL: закрытие ниже B / пробой точки 0 (аварийный стоп).

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
from app.strategy_0ab import SParams, SKTracker, invert_event
from app.market import (fetch_klines, fetch_history, load_universe, build_exchange_map,
                        fetch_klines_ex, ping_exchange, EXCHANGES,
                        fetch_binance_top_usdt, fetch_kucoin_top_usdt)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sksystem")

DATA = os.environ.get("DATA_DIR", "/app/data")
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
TF = os.environ.get("TF", "1h")
if TF not in ("1h", "4h"):
    TF = "1h"
SECONDS = {"1h": 3600, "4h": 14400}
TOP_N = int(os.environ.get("TOP_N", "200"))
MAX_POS = int(os.environ.get("MAX_POSITIONS", "10"))
DEMO = os.environ.get("DEMO", "true").lower() == "true"
LEVERAGE = float(os.environ.get("LEVERAGE", "1"))
# плечо по направлениям: лонг и шорт — фьючерсы, 2x (максимум)
LONG_LEVERAGE = float(os.environ.get("LONG_LEVERAGE", os.environ.get("LEVERAGE", "2")))
SHORT_LEVERAGE = float(os.environ.get("SHORT_LEVERAGE", "2"))
# с плечом>1: не берём сетапы, где стоп дальше MAX_STOP_PCT% от входа
# (иначе ликвидация наступит раньше стопа: у 3x маржа сгорает при ~-30% цены)
MAX_STOP_PCT = float(os.environ.get("MAX_STOP_PCT", "8"))
WARMUP_BARS = 400

NL = chr(10)
KB = {"keyboard": [["📊 Daily", "💰 Profit", "🏦 Balance"],
                   ["📈 Status", "📋 Table", "🚀 Performance"],
                   ["🔢 Count", "▶️ Start", "⏹ Stop"],
                   ["❓ Help", "🔄 Reset"]],
      "resize_keyboard": True}

os.makedirs(DATA, exist_ok=True)


def cfg() -> SParams:
    """Params from env, overridden by data/config.json (live-tunable)."""
    p = SParams(
        entry_usd=float(os.environ.get("ENTRY_USD", "100")),
        capital_usd=float(os.environ.get("CAPITAL_USD", "1000")),
        tp1_pct=float(os.environ.get("TP1_PCT", "2.0")),
        tp1_frac=float(os.environ.get("TP1_FRAC", "0.30")),
        entry_mode=os.environ.get("ENTRY_MODE", "zone"),
        min_impulse_atr=float(os.environ.get("MIN_IMPULSE_ATR", "1.5")),
        zone_min=float(os.environ.get("ZONE_MIN", "0.5")),
        zone_max=float(os.environ.get("ZONE_MAX", "0.786")),
        life_active=int(os.environ.get("LIFE_ACTIVE", "150")),
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
    d = df.tail(72).reset_index(drop=True)
    up, dn = "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(6.4, 4.2), dpi=110)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#10231a")
    for i, r in d.iterrows():
        cnd = up if r["close"] >= r["open"] else dn
        ax.plot([i, i], [r["low"], r["high"]], color=cnd, lw=0.8, zorder=2)
        lo_, hi_ = min(r["open"], r["close"]), max(r["open"], r["close"])
        ax.add_patch(Rectangle((i - 0.32, lo_), 0.64, max(hi_ - lo_, 1e-9),
                               facecolor=cnd, edgecolor=cnd, zorder=3))
    # «зелёный блок»: цели 1.618 / 1.809 / 2.0 от B
    lv = pos.get("lv") or []
    if lv:
        top = max(px for _, px in lv)
        bot = min(px for _, px in lv)
        off = len(df) - 72
        x0 = max(int(pos.get("opened_i", len(df))) - off, 0)
        ax.add_patch(Rectangle((x0, bot), len(d) - x0 + 4, top - bot,
                               facecolor="#26a69a", alpha=0.13,
                               edgecolor="#26a69a", lw=1.0, zorder=1))
        ax.text(x0 + 0.3, top, "block TP", color="#3fb68b", fontsize=8, va="bottom")
        for k, (name, y) in enumerate(lv):
            ax.hlines(y, -1, len(d), colors="#2ea87e", linestyles=":", lw=1.0)
            ax.text(len(d) - 0.5, y, f"{name}  {fmt_px(y)}",
                    color="#7ee0bb", fontsize=7, va="center", ha="right",
                    bbox=dict(boxstyle="round,pad=0.2", fc="#0d1117", ec="#2ea87e", lw=0.5))
    xmin, xmax = -1, len(d)
    _lng = pos.get("dir", 1) == 1
    rows = [(pos["entry"], "#ffd54a", "ENTRY", "--"),
            (pos["tp1"], up, "TP1 +2%", "-"),
            (pos["fail_px"], dn, "SL (close<B)" if _lng else "SL (close>B)", "-"),
            (pos["cat_sl"], "#b04a4a", "SL catastr (0)", ":")]
    for y, c_, name, st in rows:
        pct = (y - pos["entry"]) / pos["entry"] * 100
        ax.hlines(y, xmin, xmax, colors=c_, linestyles=st, lw=1.4)
        ax.text(xmax - 0.5, y, f"{name}  {fmt_px(y)}  ({pct:+.1f}%)", color=c_,
                fontsize=8, va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.25", fc="#0d1117", ec=c_, lw=0.6))
    ax.set_title(f"#{symbol} · {TF} · {'LONG' if pos.get('dir', 1) == 1 else 'SHORT'} · SK 0→A→B", color="#e6edf3",
                 fontsize=10, loc="left")
    ax.tick_params(colors="#8b949e", labelsize=7)
    for sp in ax.spines.values():
        sp.set_color("#30363d")
    ax.set_xlim(xmin, xmax)
    ax.grid(color="#21262d", lw=0.5)
    fig.text(0.01, 0.01, "SK System | 0→A→B | сигнал в точке B | TP1 +2%/30% · TP2 1.618 = 100% | SL: закрытие<B",
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
    # инициализируем трекер, если его нет (новая монета или сброс через reset:
    # reset чистит trackers/processed, но cache остаётся)
    if symbol not in STATE["trackers"]:
        df0 = load_symbol(symbol)
        cache[symbol] = df0
        tr_l, tr_s = SKTracker(p), SKTracker(p)
        if not df0.empty:
            # прокручиваем тёплые бары: детектор сразу видит структуры
            # 0->A->B, уже находящиеся в работе (позиции не открываем).
            # Шорт-трекер крутим в инвертированном пространстве цен
            # (-high/-low/...): падения становятся ростом, шорт = зеркальный лонг.
            tr_l.armed = tr_s.armed = False
            hv, lv = df0["high"].values, df0["low"].values
            ov, cv = df0["open"].values, df0["close"].values
            for i in range(len(df0) - 1):
                tr_l.update(hv, lv, ov, cv, i)
                tr_s.update(-lv, -hv, -ov, -cv, i)
            tr_l.armed = tr_s.armed = True
        STATE["trackers"][symbol] = (tr_l, tr_s)
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
    tr_l, tr_s = STATE["trackers"][symbol]

    def handle(ev, d, lev):
        if ev[0] == "sig":
            _, entry, info = ev
            if STATE["paused"] or len(book.open) >= MAX_POS or symbol in book.open:
                return
            if lev > 1 and info.get("fail_px"):
                stop_pct = abs(entry - info["fail_px"]) / entry * 100
                if stop_pct > MAX_STOP_PCT:
                    log.info("skip %s %s: stop %.1f%% > max %.1f%% (leverage %gx)",
                             symbol, "LONG" if d == 1 else "SHORT",
                             stop_pct, MAX_STOP_PCT, lev)
                    return
            open_pos(symbol, d, entry, info, df, p, lev)
        elif ev[0] == "part" and symbol in book.open:
            pos = book.open[symbol]
            if pos["dir"] != d:
                return
            _, px, frac, label = ev
            part_pos(symbol, pos, float(px), float(frac), label, p)
        elif ev[0] == "exit" and symbol in book.open:
            pos = book.open[symbol]
            if pos["dir"] != d:
                return
            _, px, reason = ev
            close_pos(symbol, pos, float(px), reason, p)

    for i in range(STATE["processed"].get(symbol, 0) + 1, last_closed + 1):
        for ev in tr_l.update(hv, lv, ov, cv, i):
            handle(ev, 1, LONG_LEVERAGE)
        for ev in tr_s.update(-lv, -hv, -ov, -cv, i):
            handle(invert_event(ev), -1, SHORT_LEVERAGE)
    STATE["processed"][symbol] = last_closed


def _fees(notional, p):
    return notional * p.fee_pct / 100.0 * 2   # вход + выход


def open_pos(symbol, d, entry, info, df, p, lev):
    pos = {"dir": d, "entry": entry, "opened": time.time(), "lev": lev,
           "p0": info["p0"], "pA": info["pA"], "pB": info["pB"],
           "fail_px": info["fail_px"], "cat_sl": info["cat_sl"],
           "tp1": info["tp1"], "lv": [(lv, px) for lv, px in info["lv"]],
           "left_frac": 1.0, "entry_usd": p.entry_usd,
           "opened_i": int(info["opened_i"])}
    book.trade_no += 1
    book.signals += 1
    STATE["entries"] = STATE.get("entries", 0) + 1
    pp = book.per_pair.setdefault(symbol, {"signals": 0, "closed": 0, "wins": 0,
                                           "losses": 0, "pnl": 0.0})
    pp["signals"] += 1
    book.open[symbol] = pos
    book.save()
    log.info("signal %s %s entry=%s p0=%s pA=%s", symbol,
             "LONG" if d == 1 else "SHORT", entry, info["p0"], info["pA"])
    log_event(f"OPEN {'LONG' if d == 1 else 'SHORT'} #{symbol} @ {fmt_px(entry)}")
    block = " · ".join(f"{lv} → {fmt_px(px)} ({(px - entry) / entry * 100:+.1f}%)"
                       for lv, px in pos["lv"])
    dir_txt = "🟢 LONG" if d == 1 else "🔴 SHORT (фьючерсы)"
    sl_txt = (f"🛑 SL: закрытие ниже B (<code>{fmt_px(info['fail_px'])}</code>) · катастрофа ниже 0 (<code>{fmt_px(info['cat_sl'])}</code>)"
              if d == 1 else
              f"🛑 SL: закрытие выше B (<code>{fmt_px(info['fail_px'])}</code>) · катастрофа выше 0 (<code>{fmt_px(info['cat_sl'])}</code>)")
    tg_photo(make_chart(symbol, df, pos), NL.join([
        "🔵 <b>Сигнал SK 0→A→B — точка B</b>",
        f"Trade #{book.trade_no}{' · DEMO' if DEMO else ''} · вход ${p.entry_usd:g} · плечо {lev:g}x (номинал ${p.entry_usd * lev:g}) · капитал ${p.capital_usd:g}",
        f"#{symbol} · {TF} · {dir_txt}",
        "",
        f"👉 ENTRY: <code>{fmt_px(entry)}</code>",
        f"ⓘ Структура: 0 <code>{fmt_px(info['p0'])}</code> → A <code>{fmt_px(info['pA'])}</code>",
        f"🧱 Зелёный блок (цели от B): {block}",
        "",
        f"🎯 TP1: <code>{fmt_px(info['tp1'])}</code> ({'+' if d == 1 else '−'}{p.tp1_pct:g}%) — закрою {p.tp1_frac * 100:g}%",
        f"🎯 TP2: уровень 1.618 — закрою 100% позиции",
        sl_txt,
        "",
        f"📊 Signals for pair: {pp['signals']}",
        f"Prices in USDT. Плечо {lev:g}x — PnL считается на маржу, движение цены x{lev:g}.",
    ]))


def part_pos(symbol, pos, px, frac, label, p):
    """Частичная фиксация (TP1 +2% / TP2 1.618)."""
    d = pos["dir"]
    lev = pos.get("lev", 1.0)
    notional = pos["entry_usd"] * frac * lev
    pnl_usdt = notional * (px - pos["entry"]) / pos["entry"] * d
    pnl_usdt -= _fees(notional, p)
    pos["left_frac"] = max(pos["left_frac"] - frac, 0.0)
    pos["tp1_done" if "TP1" in label else "tp2_done"] = True
    book.balance += pnl_usdt
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    book.daily.setdefault(day, {"n": 0, "pnl": 0.0})
    book.daily[day]["pnl"] += pnl_usdt
    book.save()
    log_event(f"PART {label} #{symbol} @ {fmt_px(px)}")
    tg_send(NL.join([
        f"💰 <b>Частичная фиксация — {label}</b>",
        f"#{symbol} {TF} · {'🟢 LONG 📈' if d == 1 else '🔴 SHORT 📉'}",
        f"👉 ENTRY: <code>{fmt_px(pos['entry'])}</code>",
        f"✂️ Закрыто {frac * 100:g}% (${pos['entry_usd'] * frac:g}) по <code>{fmt_px(px)}</code>",
        f"💵 PnL: <b>{pnl_usdt:+.2f} USDT</b>",
        f"🧷 Остаток позиции: {pos['left_frac'] * 100:g}%",
        f"🏦 Balance: {book.balance:,.2f} USDT",
    ]), kb=True)


def close_pos(symbol, pos, px, reason, p):
    d = pos["dir"]
    lev = pos.get("lev", 1.0)
    frac = pos.get("left_frac", 1.0)
    ret = (px - pos["entry"]) / pos["entry"] * 100 * d
    notional = pos["entry_usd"] * frac * lev
    pnl_usdt = notional * (px - pos["entry"]) / pos["entry"] * d
    pnl_usdt -= _fees(notional, p)
    won = pnl_usdt > 0
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
    book.daily[day]["pnl"] += pnl_usdt
    total = book.wins + book.losses
    wr = f"{book.wins / total * 100:.0f}%" if total else "—"
    icon = "✅" if str(reason).startswith("TP") else ("⏱" if reason == "TIME" else "🛑")
    title = {"TP2 1.618": "TAKE-PROFIT 1.618 (финал, 100%)", "SL": "STOP-LOSS",
             "TIME": "TIME OUT", "BE": "БЕЗУБЫТОК"}.get(reason, reason)
    dur = max(int(time.time() - pos["opened"]) // 3600, 1)
    book.save()
    del book.open[symbol]
    log_event(("CLOSE " if not str(reason).startswith("TP") else "TP HIT ") + "#" + symbol + " " + str(reason) + " " + ("%+.2f%%" % ret))
    tg_send(NL.join([
        f"{icon} <b>Позиция закрыта — {title}</b>",
        f"#{symbol} {TF} · {'🟢 LONG 📈' if d == 1 else '🔴 SHORT 📉'}",
        f"👉 ENTRY: <code>{fmt_px(pos['entry'])}</code>",
        f"🔚 EXIT: <code>{fmt_px(px)}</code> ({ret:+.2f}%)",
        f"💰 PnL остатка: <b>{pnl_usdt:+.2f} USDT</b> (закрыто {frac * 100:g}%)",
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
        lines.append(f"   {sym} {'LONG' if p_['dir'] == 1 else 'SHORT'} · entry {fmt_px(p_['entry'])} · age {age}h"
                     + (" · TP1✓" if p_.get("tp1_done") else "") + (" · TP2✓" if p_.get("tp2_done") else "")
                     + f" · left {p_.get('left_frac', 1) * 100:g}%")
    return NL.join(lines)


def cmd_table():
    if not book.open:
        return "📋 No open positions"
    rows = ["<code>Symbol       Entry          TP1(+2%)       SL(close<B)     Left   Age</code>"]
    for sym, p_ in book.open.items():
        age = f"{max(int(time.time() - p_['opened']) // 3600, 0)}h"
        rows.append("<code>" + f"{sym:<12}{fmt_px(p_['entry']):<14}{fmt_px(p_['tp1']):<14}"
                    f"{fmt_px(p_['fail_px']):<15}{p_.get('left_frac', 1) * 100:<5g}%{age}" + "</code>")
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

def build_universe():
    """(symbols, exchange). Binance top-200 предпочтительно; если Binance
    гео-блочит серверный IP (451) — KuCoin: те же монеты и цены."""
    try:
        uni = fetch_binance_top_usdt(TOP_N)
        if uni:
            return uni, "binance"
    except Exception as e:
        log.error("binance universe failed: %s", e)
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
    log.info("universe: %d symbols via %s (Binance top by 24h volume)", len(STATE["universe"]), ex)
    try:
        probe = fetch_klines("BTCUSDT", TF, limit=2)
        log.info("binance api check OK (%d rows)", len(probe))
    except Exception as e:
        log.error("binance api check FAILED: %s", e)
    tg_set_commands()
    tg_send(NL.join(["🤖 <b>SK System bot started</b>",
                     f"├ Universe: Binance top-{len(STATE['universe'])} by 24h volume",
                     "├ Timeframe: " + TF + " · стратегия 0→A→B (лонг 1x спот + шорт 2x фьючерсы)",
                     "├ Сигнал: касание зоны B (0.5–0.786 от 0→A)",
                     "├ TP1 +2% на 30% · TP2 (1.618) закрывает 100%",
                     f"├ Вход ${cfg().entry_usd:g} · плечо {LONG_LEVERAGE:g}x лонг / {SHORT_LEVERAGE:g}x шорт · капитал ${cfg().capital_usd:g} · макс. {MAX_POS} позиций",
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
