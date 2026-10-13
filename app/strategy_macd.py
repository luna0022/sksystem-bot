
"""SK System MACD-DCA (спот, лонг-онли).

Стратегия хозяина:
 - MACD(12, 26, 9) по свечам (по умолчанию 4ч)
 - Линия MACD сформировала локальный минимум НИЖЕ НУЛЯ («нижняя точка»)
   -> покупаем лот lot_usd (по умолчанию $300). Каждый следующий подтверждённый
   минимум ниже нуля -> докупаем ещё лот (мартингейл/DCA), не более max_lots.
 - Линия MACD сформировала локальный максимум ВЫШЕ НУЛЯ («высокая точка»)
   -> продаём ВСЁ сразу.
 - Чистый спот: плеча нет, ликвидаций не существует. Стопа нет: если монета
   падает — держим мешок и докупаем, пока не кончатся лоты или не придёт
   верхняя точка MACD.

Точность: пивот подтверждается pivot барами справа (как в Pine) — событие
приходит на баре подтверждения, покупка по его закрытию. Перерисовки нет.

События update(h, l, o, c, i):
 - ("buy", close, info)        докупка лота; info: lot_usd, n_lot, macd, signal
 - ("sell_all", close, info)   продажа всей позиции; info: macd, signal
"""
from dataclasses import dataclass


@dataclass
class MParams:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    pivot: int = 2            # окно пивота (2 => фрактал 5 баров)
    lot_usd: float = 300.0    # размер одной докупки
    max_lots: int = 6         # мартингейл-ограничение (капитал)
    fee_pct: float = 0.1      # спот, обе стороны


class MacdTracker:
    """Инкрементальный MACD + детектор пивотов на линии MACD. update() строго
    слева направо по закрытым свечам. armed=False во время прогрева истории —
    детектор строит состояние, но события не эмитит."""

    def __init__(self, p: MParams):
        self.p = p
        self.armed = True
        # --- цены ---
        self.c = []
        # --- EMA ---
        self.ema_f = None
        self.ema_s = None
        self.macd = []            # линия MACD по барам (None на прогреве)
        self.sig = []             # сигнальная линия
        self.ema_sig = None
        self.n = 0
        # --- позиция (лоты) ---
        self.lots = 0             # сколько лотов куплено в текущем цикле
        self.can_buy = True       # бот выключает, когда кэша меньше лота

    # ---------------------------------------------------------------- utils

    @staticmethod
    def _ema(prev, x, n):
        k = 2.0 / (n + 1)
        return x if prev is None else prev + k * (x - prev)

    def _macd_at(self, j):
        return self.macd[j] if 0 <= j < len(self.macd) else None

    def _pivot_low(self, j):
        """Локальный минимум линии MACD на баре j (окно pivot в обе стороны)."""
        p = self.p.pivot
        m = self.macd
        if j < p or j + p >= len(m):
            return False
        v = m[j]
        if v is None:
            return False
        return all(m[j + d] is not None and v <= m[j + d] for d in range(-p, p + 1))

    def _pivot_high(self, j):
        p = self.p.pivot
        m = self.macd
        if j < p or j + p >= len(m):
            return False
        v = m[j]
        if v is None:
            return False
        return all(m[j + d] is not None and v >= m[j + d] for d in range(-p, p + 1))

    # ---------------------------------------------------------------- update

    def update(self, h, l, o, c, i):
        p = self.p
        self._push(self.c, c[i])
        cl = self.c[-1]
        self.n += 1
        events = []

        # --- MACD ---
        self.ema_f = self._ema(self.ema_f, cl, p.fast)
        self.ema_s = self._ema(self.ema_s, cl, p.slow)
        m = self.ema_f - self.ema_s
        ready = self.n >= p.slow          # до slow баров MACD неустойчив
        self.macd.append(m if ready else None)
        if ready:
            self.ema_sig = self._ema(self.ema_sig, m, p.signal)
            self.sig.append(self.ema_sig)
        else:
            self.sig.append(None)

        i_loc = len(self.macd) - 1
        j = i_loc - p.pivot               # центр только что подтверждённого пивота

        # --- ПОКУПКА: подтверждённый минимум MACD ниже нуля, есть свободный лот ---
        if (self.armed and self.can_buy and self.lots < p.max_lots and j >= 0
                and self._pivot_low(j) and self.macd[j] is not None
                and self.macd[j] < 0):
            self.lots += 1
            events.append(("buy", cl, {"lot_usd": p.lot_usd, "n_lot": self.lots,
                                       "macd": self.macd[j], "signal": self.sig[j]}))
        # --- ПРОДАЖА ВСЕГО: подтверждённый максимум MACD выше нуля, есть что продавать ---
        elif (self.armed and self.lots > 0 and j >= 0
                and self._pivot_high(j) and self.macd[j] is not None
                and self.macd[j] > 0):
            self.lots = 0
            events.append(("sell_all", cl, {"macd": self.macd[j],
                                            "signal": self.sig[j]}))
        return events

    @staticmethod
    def _push(arr, v):
        arr.append(float(v))
