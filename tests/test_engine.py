
import numpy as np
import pandas as pd

from app.engine import Params, Tracker, manage_position, scan_wave, atr_from_arrays


def synth_impulse_then_pullback(n=200):
    """Uptrend impulse, then pullback into the 0.5-0.786 zone, then recovery."""
    price = np.full(n, 100.0)
    price[10:40] = np.linspace(100, 130, 30)          # impulse +30
    price[40:52] = np.linspace(130, 113, 12)          # pullback to ~0.57 retr
    price[52:] = np.linspace(113, 122, n - 52)        # recovery through 0.559
    ot = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"open_time": ot, "open": price, "close": price,
                       "high": price * 1.002, "low": price * 0.998,
                       "volume": np.full(n, 1.0)})
    return df


def test_impulse_scan_detects_wave():
    df = synth_impulse_then_pullback()
    hv, lv = df["high"].values, df["low"].values
    ov, cv = df["open"].values, df["close"].values
    atr_v = atr_from_arrays(hv, lv, cv, 14)
    p = Params(lookback=120)
    w = scan_wave(hv, lv, ov, cv, atr_v, 60, 1, p)
    assert w is not None and w["valid"]
    assert w["origin"] <= 100.5 and w["end"] >= 129.0


def test_tracker_full_path_entry():
    df = synth_impulse_then_pullback()
    hv, lv = df["high"].values, df["low"].values
    ov, cv = df["open"].values, df["close"].values
    atr_v = atr_from_arrays(hv, lv, cv, 14)
    p = Params(lookback=120, quality_filter=False)
    tr = Tracker()
    entry = None
    for i in range(45, len(df)):
        ev, lvl = tr.update(hv, lv, ov, cv, atr_v, i, p)
        if ev == "entry":
            entry = (i, lvl)
            break
    assert entry is not None, "zone touch -> trigger confirm must fire"
    i, lvl = entry
    assert lvl["dir"] == 1
    assert lvl["stop"] < lvl["zone_far"]


def test_trailing_and_tps():
    p = Params()
    pos = {"dir": 1, "entry": 100.0, "sl": 95.0}
    ev1 = manage_position(pos, 102.0, 101.0, 101.5, p)   # below thresholds
    assert ev1 == [] and pos["sl"] == 95.0
    # +5% close -> SL to +1%
    ev2 = manage_position(pos, 106.0, 104.0, 105.2, p)
    assert ("sl_move", 101.0) in ev2
    # +8% close -> SL to +2%
    ev3 = manage_position(pos, 109.0, 107.0, 108.1, p)
    assert ("sl_move", 102.0) in ev3
    # TP1 / TP2 by high
    pos2 = {"dir": 1, "entry": 100.0, "sl": 95.0}
    ev4 = manage_position(pos2, 102.6, 100.1, 102.0, p)
    assert ("tp1", 102.5) in ev4
    pos2["tp1_done"] = True
    ev5 = manage_position(pos2, 105.1, 103.0, 104.0, p)
    assert ("tp2", 105.0) in ev5
    # SL has priority inside one bar
    pos3 = {"dir": 1, "entry": 100.0, "sl": 95.0}
    ev6 = manage_position(pos3, 102.6, 94.0, 96.0, p)
    assert ev6[-1][0] == "close" and ev6[-1][2] == "SL"


def test_short_direction_symmetry():
    df = synth_impulse_then_pullback()
    price = 300 - (df["close"].values - 90)   # mirror: downtrend + pullback
    ot = df["open_time"]
    df2 = pd.DataFrame({"open_time": ot, "open": price, "close": price,
                        "high": price * 1.002, "low": price * 0.998,
                        "volume": np.full(len(price), 1.0)})
    hv, lv = df2["high"].values, df2["low"].values
    ov, cv = df2["open"].values, df2["close"].values
    atr_v = atr_from_arrays(hv, lv, cv, 14)
    p = Params(lookback=120, quality_filter=False)
    tr = Tracker()
    got = None
    for i in range(45, len(df2)):
        ev, lvl = tr.update(hv, lv, ov, cv, atr_v, i, p)
        if ev == "entry":
            got = lvl
            break
    assert got is not None and got["dir"] == -1
