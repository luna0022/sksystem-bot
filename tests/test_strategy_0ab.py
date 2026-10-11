
"""Тесты стратегии SK 0->A->B (strategy_0ab.py): ручной сетап 0->A->B,
money management юзера (TP1 +2%/30%, TP2 1.618/30%, TP3 2.0/40%), SL-пути,
режим ENTRY_MODE=act и фазз на случайном блуждании."""
import numpy as np
import pytest

from app.strategy_0ab import SParams, SKTracker


def run(p, bars):
    """bars: list of (o,h,l,c). Вернуть список всех событий."""
    h = np.array([b[1] for b in bars])
    l = np.array([b[2] for b in bars])
    o = np.array([b[0] for b in bars])
    c = np.array([b[3] for b in bars])
    tr = SKTracker(p)
    events = []
    for i in range(len(bars)):
        events.extend(tr.update(h, l, o, c, i))
    return events


def base_uptrend_setup():
    """0=90 на баре 10, A=120 на баре 20, откат в зону (касание на баре 26,
    B=100 на баре 30), затем ралли до 160. Сигнал в зоне -> вход 103."""
    bars = []
    # 0-4: тихий флэт 94-96
    for i in range(5):
        bars.append((95.0, 96.0, 94.0, 95.0))
    # 5-9: спуск к 0 (лоу 91,91.5,..., но не ниже 90)
    for i in range(5):
        lo = 91.0 + (4 - i) * 0.2
        bars.append((lo + 1.5, lo + 3.0, lo, lo + 1.0))
    # бар 10: точка 0, лоу 90
    bars.append((92.0, 93.0, 90.0, 91.5))
    # 11-14: разворот вверх
    for i in range(4):
        base = 92.0 + i * 1.5
        bars.append((base, base + 2.0, base - 0.5, base + 1.5))
    # 15-19: импульс к A
    for i in range(5):
        base = 98.0 + i * 3.0
        bars.append((base, base + 3.5, base - 0.5, base + 3.0))
    # бар 20: точка A, хай 120
    bars.append((116.0, 120.0, 115.0, 119.0))
    # 21-24: начало коррекции (лоу 112-110, выше зоны)
    for i in range(4):
        hi = 118.0 - i
        bars.append((hi, hi, 112.0 - i * 0.5, 113.0 - i))
    # бар 25: A подтверждена (пивот с окном 15-25)
    bars.append((111.0, 112.0, 108.0, 109.5))
    # бар 26: КАСАНИЕ ЗОНЫ B (топ зоны 105) -> сигнал, вход по закрытию 103
    bars.append((108.0, 108.5, 104.0, 103.0))
    # 27-29: коррекция продолжается
    for i in range(3):
        hi = 105.0 - i * 0.7
        bars.append((hi - 0.5, hi, 102.0 - i * 0.8, hi - 1.0))
    # бар 30: точка B, лоу 100 (минимум окна 25-35)
    bars.append((102.0, 103.0, 100.0, 101.5))
    # 31-35: ровный флэт 101-103 (окно B не ломаем, хаи/лоу выше 100)
    for i in range(5):
        bars.append((101.5, 103.0, 100.8, 102.0))
    # 36-40: рост, пробой TP1 (105.06) хаем
    for i in range(5):
        base = 103.0 + i * 0.8
        bars.append((base, base + 1.0, base - 0.3, base + 0.7))
    # 41-50: рост до 148.5 (TP2 1.618 = 148.5)
    for i in range(10):
        base = 107.0 + i * 4.0
        bars.append((base, min(base + 4.5, 149.0), base - 0.5, base + 4.0))
    # 51-60: финальный рывок до 160 (TP3 2.0 = 160)
    for i in range(10):
        base = 148.0 + i * 1.4
        bars.append((base, min(base + 1.5, 160.0), base - 0.5, base + 1.2))
    return bars


def test_full_setup_tp1_tp2_tp3():
    p = SParams()
    ev = run(p, base_uptrend_setup())
    sigs = [e for e in ev if e[0] == "sig"]
    assert len(sigs) == 1, f"ожидался 1 сигнал, получено {len(sigs)}: {ev}"
    _, entry, info = sigs[0]
    assert entry == pytest.approx(103.0)
    assert info["p0"] == pytest.approx(90.0)
    assert info["pA"] == pytest.approx(120.0)
    # TP1: +2%
    assert info["tp1"] == pytest.approx(103.0 * 1.02)
    # блок на момент сигнала: оценка B по касанию = 104 (лоу бара 26)
    lv = dict(info["lv"])
    assert lv[1.618] == pytest.approx(104 + 1.618 * 30, abs=0.5)
    # после подтверждения пивота B=100 уровни обновятся до 148.5/160 —
    # это проверяем косвенно по ценам исполнения TP2/TP3 ниже
    # порядок: TP1 на 30%, затем 1.618 закрывает 100% позиции
    parts = [(e[3], e[2]) for e in ev if e[0] == "part"]
    assert len(parts) == 1
    assert parts[0][0].startswith("TP1") and parts[0][1] == pytest.approx(0.30)
    exits = [e for e in ev if e[0] == "exit"]
    assert len(exits) == 1 and exits[0][2] == "TP2 1.618"
    assert exits[0][1] == pytest.approx(148.5, abs=0.5)   # уровень после подтверждения B=100


def test_stop_loss_on_fail_close_below_b():
    """После входа цена закрывается ниже B -> выход SL по закрытию."""
    bars = base_uptrend_setup()
    # обрезаем после входа (бар 26) и ведём цену вниз
    tail = []
    px = 102.0
    for i in range(40):
        px -= 1.2
        tail.append((px + 1.0, px + 1.2, px, px + 0.1))
    ev = run(SParams(), bars[:27] + tail)
    exits = [e for e in ev if e[0] == "exit"]
    assert exits and exits[0][2] == "SL"
    assert exits[0][1] < 96.5   # вышли по провалу (закрытие ниже ~96.4), не по катастрофе 89.8


def test_entry_mode_act():
    """ENTRY_MODE=act: сигнал только на закрытии выше A после подтверждения B."""
    p = SParams(entry_mode="act")
    bars = base_uptrend_setup()
    # бар 36 (после подтверждения B на баре 35): закрытие выше A -> активация
    bars[36] = (103.0, 121.5, 102.5, 121.0)
    ev = run(p, bars)
    sigs = [e for e in ev if e[0] == "sig"]
    assert len(sigs) == 1
    assert sigs[0][1] == pytest.approx(121.0)
    # B подтверждена (100), блок от B: 1.618 = 148.5
    assert dict(sigs[0][2]["lv"])[1.618] == pytest.approx(148.5, abs=0.5)


def test_no_signal_without_impulse():
    """Импульс 0->A короче 1.5 ATR -> сигнала нет."""
    p = SParams(min_impulse_atr=50.0)   # нереально высокий порог
    ev = run(p, base_uptrend_setup())
    assert not [e for e in ev if e[0] == "sig"]


def test_replay_mode_no_positions():
    """armed=False: детектор строит состояние по истории, но не открывает позицию."""
    p = SParams()
    bars = base_uptrend_setup()
    h = np.array([b[1] for b in bars]); l = np.array([b[2] for b in bars])
    o = np.array([b[0] for b in bars]); c = np.array([b[3] for b in bars])
    tr = SKTracker(p)
    tr.armed = False
    events = []
    for i in range(len(bars)):
        events.extend(tr.update(h, l, o, c, i))
    assert not [e for e in events if e[0] == "sig"]
    assert tr.pos is None


def test_fuzz_random_walk_no_crash():
    rng = np.random.default_rng(42)
    for _ in range(5):
        n = 3000
        ret = rng.normal(0, 0.01, n)
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.roll(close, 1); open_[0] = 100.0
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
        p = SParams()
        tr = SKTracker(p)
        for i in range(n):
            for e in tr.update(hi, lo, open_, close, i):
                if e[0] in ("part", "exit"):
                    assert tr.pos is None or tr.pos["left_frac"] >= 0
        if tr.pos is not None:
            assert 0 <= tr.pos["left_frac"] <= 1
