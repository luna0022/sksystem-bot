
"""Order Blocks engine — port of the "Order Blocks / MSB" TradingView indicator
(pivots -> market structure break -> demand/supply boxes), with the user's
trading rules:
 - 4h timeframe, trade FROM block TO block: entry at block retest, TP at the
   nearest opposite active block
 - stop-loss beyond the block + 2%
 - leverage x3 (notional cap), risk-based sizing done by caller
Box lifecycle follows the indicator: test (price enters) -> dimmed, fill
(price through) -> box closed. Delays: test_delay / fill_delay bars.
"""
from dataclasses import dataclass, field


@dataclass
class ObParams:
    test_delay: int = 3
    fill_delay: int = 3
    sl_buffer_pct: float = 2.0      # stop beyond the block + 2%
    min_rr: float = 0.5             # skip trades with reward/risk below this
    timeout_bars: int = 36          # 4h bars (6 days)
    leverage: float = 3.0
    margin_usd: float = 100.0       # fixed margin per trade (notional = margin x leverage)
    risk_pct: float = 1.0
    fee_pct: float = 0.1
    slippage_pct: float = 0.05
    max_boxes: int = 60


class Box:
    __slots__ = ("dir", "top", "bottom", "born", "state", "tp_used")

    def __init__(self, d, top, bottom, born):
        self.dir = d            # +1 demand (long), -1 supply (short)
        self.top = top
        self.bottom = bottom
        self.born = born
        self.state = "active"   # active -> tested -> filled
        self.tp_used = False


class ObTracker:
    """Per-symbol incremental state (call update() on every closed bar,
    strictly left to right). Emits events:
      ("bos", dir, box)                        structure broken, box created
      ("entry", dir, entry, sl, tp, box)       limit fill at block retest
      ("exit", px, reason)                     SL / TP / TIME for open pos
    """

    def __init__(self, p: ObParams):
        self.p = p
        self.pvh1_p, self.pvh1_t = [], []
        self.pvl1_p, self.pvl1_t = [], []
        self.htcmrll_p = None   # high that created most recent lower low
        self.ltcmrhh_p = None   # low that created most recent higher high
        self.boxes = []         # all boxes (active/tested first)
        self.pos = None         # open position dict
        self.pending = None     # armed setup waiting for retest

    # ---------------------------------------------------------------- utils

    def _unshift(self, arr, val, n=1000):
        arr.insert(0, val)
        del arr[n:]

    def _nearest_opp_box(self, d, entry):
        """nearest active opposite block usable as target"""
        best = None
        for b in self.boxes:
            if b.state == "filled" or b.dir == d:
                continue
            if d == 1:                      # long: target supply above
                px = b.bottom
                if px > entry and (best is None or px < best):
                    best = px
            else:                           # short: target demand below
                px = b.top
                if px < entry and (best is None or px > best):
                    best = px
        return best

    # ---------------------------------------------------------------- update

    def update(self, h, l, o, c, i):
        p = self.p
        events = []
        # --- 1st order pivots (pivot at bar i-1)
        pvh = h[i] < h[i - 1] and h[i - 1] > h[i - 2]
        pvl = l[i] > l[i - 1] and l[i - 1] < l[i - 2]
        th, tl = h[i], l[i]     # trigger: High and Low mode

        if pvh:
            self._unshift(self.pvh1_p, h[i - 1])
            self._unshift(self.pvh1_t, i - 1)
            if len(self.pvh1_p) > 2 and self.pvh1_p[0] > self.pvh1_p[1]:
                for t, px in zip(self.pvl1_t, self.pvl1_p):
                    if t < self.pvh1_t[0]:
                        self.ltcmrhh_p = px
                        break
        if pvl:
            self._unshift(self.pvl1_p, l[i - 1])
            self._unshift(self.pvl1_t, i - 1)
            if len(self.pvl1_p) > 2 and self.pvl1_p[0] < self.pvl1_p[1]:
                for t, px in zip(self.pvh1_t, self.pvh1_p):
                    if t < self.pvl1_t[0]:
                        self.htcmrll_p = px
                        break

        # --- market structure break -> new box
        if self.htcmrll_p is not None and th > self.htcmrll_p:
            born_t = self.pvl1_t[0]
            off = i - born_t
            top = min(h[i - off], h[i - off + 1])
            box = Box(1, top, self.pvl1_p[0], i)
            self.boxes.insert(0, box)
            del self.boxes[p.max_boxes:]
            self.htcmrll_p = None
            events.append(("bos", 1, box))
        if self.ltcmrhh_p is not None and tl < self.ltcmrhh_p:
            born_t = self.pvh1_t[0]
            off = i - born_t
            bottom = max(l[i - off], l[i - off + 1])
            box = Box(-1, self.pvh1_p[0], bottom, i)
            self.boxes.insert(0, box)
            del self.boxes[p.max_boxes:]
            self.ltcmrhh_p = None
            events.append(("bos", -1, box))

        # --- box test / fill lifecycle
        for b in self.boxes:
            if b.state == "filled":
                continue
            if b.dir == 1:
                if tl < b.top and b.born + p.test_delay < i:
                    b.state = "tested"
                if tl < b.bottom and b.born + p.fill_delay < i:
                    b.state = "filled"
            else:
                if th > b.bottom and b.born + p.test_delay < i:
                    b.state = "tested"
                if th > b.top and b.born + p.fill_delay < i:
                    b.state = "filled"

        # --- pending setup: retest with REACTION (entry at the confirming
        # close, so the signal is always actionable at current price)
        if self.pos is None and self.pending is not None:
            d, box = self.pending
            if box.state == "filled":
                self.pending = None
            elif i > box.born + 1:
                if d == 1:
                    touched = l[i] <= box.top
                    reacted = touched and c[i] >= box.top
                else:
                    touched = h[i] >= box.bottom
                    reacted = touched and c[i] <= box.bottom
                if reacted:
                    entry = float(c[i])
                    if d == 1:
                        sl = box.bottom * (1 - p.sl_buffer_pct / 100.0)
                    else:
                        sl = box.top * (1 + p.sl_buffer_pct / 100.0)
                    tp = self._nearest_opp_box(d, entry)
                    ok = tp and ((tp - entry) if d == 1 else (entry - tp)) \
                        >= p.min_rr * abs(entry - sl)
                    if ok:
                        self.pos = {"dir": d, "entry": entry, "sl": sl,
                                    "tp": tp, "box": box, "opened_i": i}
                        events.append(("entry", d, entry, sl, tp, box))
                    self.pending = None
        elif self.pos is not None and self.pending is not None:
            self.pending = None

        # arm new setup from fresh bos if flat
        if self.pos is None and self.pending is None:
            for name, d, box in events:
                if name == "bos":
                    self.pending = (d, box)

        # --- manage open position
        if self.pos is not None:
            pos = self.pos
            d = pos["dir"]
            hit_sl = (l[i] <= pos["sl"]) if d == 1 else (h[i] >= pos["sl"])
            hit_tp = (h[i] >= pos["tp"]) if d == 1 else (l[i] <= pos["tp"])
            if hit_sl and hit_tp:
                events.append(("exit", pos["sl"], "SL"))   # conservative
            elif hit_sl:
                events.append(("exit", pos["sl"], "SL"))
            elif hit_tp:
                events.append(("exit", pos["tp"], "TP"))
            elif i - pos["opened_i"] >= p.timeout_bars:
                events.append(("exit", float(c[i]), "TIME"))
            else:
                return events
            self.pos = None
        return events
