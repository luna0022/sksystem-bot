"""Тесты MACD-DCA стратегии (strategy_macd.py): инварианты событий на
случайном блуждании, уважение max_lots, сброс лотов при sell_all, armed."""
import numpy as np

from app.strategy_macd import MParams, MacdTracker


def walk(n=4000, seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.02, n).cumsum()
    close = 100 * np.exp(ret - ret[0] + 0.0003 * np.arange(n))
    open_ = np.roll(close, 1); open_[0] = close[0]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    return hi, lo, open_, close


def run(p, n=4000, seed=7):
    h, l, o, c = walk(n, seed)
    tr = MacdTracker(p)
    ev = []
    for i in range(n):
        ev.extend(tr.update(h, l, o, c, i))
    return ev


def test_events_macd_zones():
    """Покупки только на минимумах MACD < 0, продажи только на максимумах > 0."""
    ev = run(MParams())
    buys = [e for e in ev if e[0] == "buy"]
    sells = [e for e in ev if e[0] == "sell_all"]
    assert buys, "покупок не случилось вообще"
    assert all(e[2]["macd"] < 0 for e in buys)
    assert all(e[2]["macd"] > 0 for e in sells)


def test_max_lots_respected():
    p = MParams(max_lots=3)
    h, l, o, c = walk()
    tr = MacdTracker(p)
    consec = 0
    for i in range(len(c)):
        for e in tr.update(h, l, o, c, i):
            if e[0] == "buy":
                consec += 1
                assert consec <= 3
                assert e[2]["n_lot"] == consec
            else:
                consec = 0
    assert consec <= 3


def test_sell_all_resets_lots():
    ev = run(MParams())
    lots = 0
    for e in ev:
        if e[0] == "buy":
            lots += 1
        else:
            assert lots > 0          # продать можно только имея лоты
            lots = 0


def test_armed_false_no_events():
    h, l, o, c = walk(3000)
    tr = MacdTracker(MParams())
    tr.armed = False
    for i in range(len(c)):
        assert not tr.update(h, l, o, c, i)


def test_cash_never_negative():
    """Спотовый учёт по циклам: в каждом цикле не больше max_lots покупок,
    после sell_all кэш возвращается."""
    p = MParams(lot_usd=300.0, max_lots=6)
    cash = 2000.0
    h, l, o, c = walk()
    tr = MacdTracker(p)
    lots = []
    for i in range(len(c)):
        tr.can_buy = cash >= p.lot_usd
        for e in tr.update(h, l, o, c, i):
            if e[0] == "buy":
                cash -= e[2]["lot_usd"]
                assert cash >= 0, "капитала хватило ровно на 6 лотов"
                lots.append(e[1])
            else:
                assert lots
                cash += sum(300.0 * e[1] / px for px in lots)
                lots = []
    assert cash > 0
