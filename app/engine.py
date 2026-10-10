
"""SK System engine — port of the TradingView "SK Fib R" indicator logic.

Wave scan (f_scanUncorrected equivalent):
 - walk the window, track the impulse origin/extreme of one direction
 - a qualified wave is split at a 0.500 touch -> a new wave starts from the
   correction extreme; a quick reclaim of the parent extreme inside
   min_leg_bars bars counts as an internal pullback and continues the wave
 - quality filters: min/max leg length, impulse size >= min_atr_mult * ATR,
   share of directional candles >= dir_share (only when quality_filter)

Setup (per symbol, persistent direction — the fix vs the naive port):
 - zone 0.500..0.786 of the impulse, trigger 0.559
 - entry: close back beyond the trigger after the zone was touched
 - stop: beyond 0.786 + 0.05*ATR buffer
 - invalidation: body close beyond 0.786, full zone cross, expiry

Money management (percent of entry, user rules):
 - TP1 +2.5%: close 50%, stop -> breakeven
 - TP2 +5.0%: close 25%, stop -> +1%
 - runner 25%: stop -> +2% once price reaches +8%; exits on stop / timeout
All functions work on precomputed numpy arrays; ATR once per symbol.
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Params:
    lookback: int = 60
    atr_len: int = 14
    min_leg_bars: int = 3
    max_leg_bars: int = 150
    min_atr_mult: float = 2.0
    dir_share: float = 0.55
    quality_filter: bool = True
    zone_near: float = 0.500
    zone_far: float = 0.786
    confirm_ratio: float = 0.559
    expiry_bars: int = 200
    stop_atr_buffer: float = 0.05
    timeout_bars: int = 72
    tp1_pct: float = 2.5
    tp2_pct: float = 5.0
    trail1_at_pct: float = 5.0
    trail1_pct: float = 1.0
    trail2_at_pct: float = 8.0
    trail2_pct: float = 2.0
    tp1_frac: float = 0.50
    tp2_frac: float = 0.25
    risk_pct: float = 1.0
    leverage_cap: float = 5.0
    fee_pct: float = 0.1
    slippage_pct: float = 0.05


def atr_from_arrays(hi, lo, cl, period=14):
    tr = np.maximum(hi[1:] - lo[1:],
                    np.maximum(np.abs(hi[1:] - cl[:-1]), np.abs(lo[1:] - cl[:-1])))
    out = np.empty_like(hi)
    out[0] = tr[0] if len(tr) else 0.0
    a = 1.0 / period
    for k in range(1, len(hi)):
        out[k] = tr[k - 1] if k == 1 else (1 - a) * out[k - 1] + a * tr[k - 1]
    return out


def scan_wave(hi, lo, op, cl, atr_v, i, direction, p: Params):
    """Last uncorrected impulse ending at or before bar i (dict or None)."""
    origin = end = None
    ob = eb = None
    p_origin = p_end = p_ob = None
    cand_bar = -10**9
    pending = False
    start = max(0, i - p.lookback)
    for k in range(start, i + 1):
        o = lo[k] if direction == 1 else hi[k]
        e = hi[k] if direction == 1 else lo[k]
        if origin is None:
            origin = end = o
            ob = eb = k
            continue
        new_end = (e > end) if direction == 1 else (e < end)
        if pending and (k - cand_bar) < p.min_leg_bars and \
                ((e > p_end) if direction == 1 else (e < p_end)):
            origin, ob = p_origin, p_ob
            end, eb = e, k
            pending = False
            continue
        if pending and (k - cand_bar) >= p.min_leg_bars:
            pending = False
        if new_end:
            end, eb = e, k
        bars = eb - ob
        rng = end - origin if direction == 1 else origin - end
        atr_k = atr_v[k]
        qualified = (bars >= p.min_leg_bars and bars <= p.max_leg_bars
                     and (not p.quality_filter or atr_k <= 0 or rng >= p.min_atr_mult * atr_k))
        half = end - rng * 0.500 if direction == 1 else end + rng * 0.500
        corrected = (not new_end) and qualified and \
            ((lo[k] <= half) if direction == 1 else (hi[k] >= half))
        if corrected:
            p_origin, p_end, p_ob = origin, end, ob
            cand_bar = k
            pending = True
            origin = end = o
            ob = eb = k
        elif not qualified and ((o < origin) if direction == 1 else (o > origin)):
            origin = end = o
            ob = eb = k
    if origin is None:
        return None
    bars = eb - ob
    rng = (end - origin) if direction == 1 else (origin - end)
    atr_now = atr_v[i]
    ok_len = p.min_leg_bars <= bars <= p.max_leg_bars
    ok_atr = (not p.quality_filter) or atr_now <= 0 or rng >= p.min_atr_mult * atr_now
    ok_dir = True
    if p.quality_filter and bars >= 2:
        n = good = 0
        for k in range(ob + 1, eb + 1):
            n += 1
            if direction == 1:
                good += 1 if cl[k] > op[k] else 0
            else:
                good += 1 if cl[k] < op[k] else 0
        ok_dir = (good / n) >= p.dir_share
    valid = ok_len and ok_atr and ok_dir
    return {"dir": direction, "origin": float(origin), "end": float(end),
            "ob": ob, "eb": eb, "valid": bool(valid)}


def levels(wave, atr_now, p: Params):
    o, e, d = wave["origin"], wave["end"], wave["dir"]
    rng = abs(e - o)
    def retr(r):
        return e - rng * r if d == 1 else e + rng * r
    return {"dir": d, "origin": o, "end": e, "ob": wave["ob"], "eb": wave["eb"],
            "range": rng,
            "zone_near": retr(p.zone_near), "zone_far": retr(p.zone_far),
            "trigger": retr(p.confirm_ratio),
            "stop": retr(1.0) - d * atr_now * p.stop_atr_buffer,
            "atr": atr_now,
            "tp1": None, "tp2": None}  # filled at entry (percent-based)


class Tracker:
    """Per-symbol pre-entry state machine. The DIRECTION of an active setup is
    fixed at creation; the opposite-direction scan can never hijack it (this
    persistence is what makes zone->trigger->entry reachable at all)."""

    def __init__(self):
        self.stage = 0      # 0 none, 1 waiting zone touch, 2 touched
        self.lvl = None
        self.setup_bar = 0
        self.half = False   # 0.500 touched (no more B-extension)

    def reset(self):
        self.stage = 0
        self.lvl = None
        self.half = False

    def update(self, hi, lo, op, cl, atr_v, i, p: Params):
        if self.stage == 0:
            wl = scan_wave(hi, lo, op, cl, atr_v, i, 1, p)
            ws = scan_wave(hi, lo, op, cl, atr_v, i, -1, p)
            wave = None
            if wl and wl["valid"] and ws and ws["valid"]:
                wave = wl if wl["eb"] >= ws["eb"] else ws
            elif wl and wl["valid"]:
                wave = wl
            elif ws and ws["valid"]:
                wave = ws
            if wave is None:
                return None, None
            self.lvl = levels(wave, float(atr_v[i]), p)
            self.stage = 1
            self.setup_bar = i
            self.half = False
        d = self.lvl["dir"]
        h, l, c = hi[i], lo[i], cl[i]
        # extend B while the correction has not reached 0.500 yet
        if self.stage == 1 and not self.half:
            w = scan_wave(hi, lo, op, cl, atr_v, i, d, p)
            if (w and w["valid"] and w["ob"] == self.lvl["ob"]
                    and ((w["end"] - self.lvl["end"]) * d) > 0):
                self.lvl = levels(w, float(atr_v[i]), p)
            rng = self.lvl["range"]
            half_px = self.lvl["end"] - rng * 0.5 if d == 1 else self.lvl["end"] + rng * 0.5
            if ((l <= half_px) if d == 1 else (h >= half_px)):
                self.half = True
        eb = self.lvl["eb"]
        if i <= eb:
            return None, None
        near, far, trig = self.lvl["zone_near"], self.lvl["zone_far"], self.lvl["trigger"]
        zone_hi, zone_lo = max(near, far), min(near, far)
        # invalidations
        if (c < far) if d == 1 else (c > far):
            self.reset()
            return "invalid", None
        if ((l <= zone_lo) if d == 1 else (h >= zone_hi)):
            self.reset()
            return "invalid", None
        if i - self.setup_bar > p.expiry_bars:
            self.reset()
            return "invalid", None
        if self.stage == 1 and l <= zone_hi and h >= zone_lo:
            self.stage = 2
        if self.stage == 2:
            if (c > trig) if d == 1 else (c < trig):
                self.stage = 3
                return "entry", dict(self.lvl)
        return None, None


def manage_position(pos, h, l, c, p: Params):
    """One bar of post-entry management. Mutates pos. Events:
    ("tp1", px) ("tp2", px) ("sl_move", px) ("close", px, reason)."""
    d = pos["dir"]
    entry = pos["entry"]
    tp1 = entry * (1 + d * p.tp1_pct / 100.0)
    tp2 = entry * (1 + d * p.tp2_pct / 100.0)
    pos["tp1_px"], pos["tp2_px"] = tp1, tp2
    events = []
    profit_pct = d * (c - entry) / entry * 100.0
    if not pos.get("trail1") and profit_pct >= p.trail1_at_pct:
        pos["trail1"] = True
        new_sl = entry * (1 + d * p.trail1_pct / 100.0)
        if d * (new_sl - pos["sl"]) > 0:
            pos["sl"] = new_sl
            events.append(("sl_move", new_sl))
    if not pos.get("trail2") and profit_pct >= p.trail2_at_pct:
        pos["trail2"] = True
        new_sl = entry * (1 + d * p.trail2_pct / 100.0)
        if d * (new_sl - pos["sl"]) > 0:
            pos["sl"] = new_sl
            events.append(("sl_move", new_sl))
    hit_sl = (l <= pos["sl"]) if d == 1 else (h >= pos["sl"])
    hit_tp2 = not pos.get("tp2_done") and ((h >= tp2) if d == 1 else (l <= tp2))
    hit_tp1 = not pos.get("tp1_done") and ((h >= tp1) if d == 1 else (l <= tp1))
    if hit_sl:
        events.append(("close", pos["sl"], "SL"))
    elif hit_tp2:
        events.append(("tp2", tp2))
    elif hit_tp1:
        events.append(("tp1", tp1))
    return events


def remaining_frac(pos, p: Params):
    if pos.get("tp2_done"):
        return 1.0 - p.tp1_frac - p.tp2_frac
    if pos.get("tp1_done"):
        return 1.0 - p.tp1_frac
    return 1.0
