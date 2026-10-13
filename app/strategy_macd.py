"""SK System MACD-DCA (спот, лонг-онли) — версия на ПЕРЕСЕЧЕНИЯХ.

Стратегия хозяина:
 - MACD(12, 26, 9) по свечам (по умолчанию 4ч)
 - ПЕРЕСЕЧЕНИЕ ВВЕРХ: линия MACD пересекла сигнальную снизу вверх, при этом
   сама линия MACD ниже нуля -> покупаем лот lot_usd (по умолчанию $300).
   Каждое следующее такое пересечение ниже нуля -> докупаем ещё лот
   (мартингейл/DCA), не более max_lots.
 - ПЕРЕСЕЧЕНИЕ ВНИЗ: линия MACD пересекла сигнальную сверху вниз, при этом
   сама линия MACD выше нуля -> продаём ВСЁ сразу.
 - Чистый спот: плеча нет, ликвидаций не существует. Стопа нет: если монета
   падает — держим мешок и докупаем, пока не кончатся лоты или не придёт
   пересечение вниз выше нуля.

Точность: пересечение фиксируется на закрытии бара (как в Pine) — перерисовки
нет. Сигнальная линия начинает считаться только после прогрева (n >= slow).

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
    lot_usd: float = 500.0    # размер одной докупки
    max_lots: int = 6         # мартингейл-ограничение (капитал)
    fee_pct: float = 0.1      # спот, обе стороны


class MacdTracker:
    """Инкрементальный MACD + детектор пересечений MACD/сигнальной. update()
    строго слева направо по закрытым свечам. armed=False во время прогрева
    истории — трекер строит состояние, но события не эмитит."""

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

    # ---------------------------------------------------------------- update

    def update(self, h, l, o, c, i):
        p = self.p
        self.c.append(float(c[i]))
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

        # --- пересечение: сравниваем текущий и предыдущий закрытые бары ---
        i_loc = len(self.macd) - 1
        warmed = self.n >= p.slow + p.signal   # сигнальная набралась
        prev_m = self.macd[i_loc - 1] if i_loc >= 1 else None
        prev_s = self.sig[i_loc - 1] if i_loc >= 1 else None
        cross_up = (warmed and prev_m is not None and prev_s is not None
                    and prev_m <= prev_s and m > self.sig[i_loc])
        cross_dn = (warmed and prev_m is not None and prev_s is not None
                    and prev_m >= prev_s and m < self.sig[i_loc])

        # --- ПОКУПКА: пересечение вверх, линия MACD ниже нуля, есть лот ---
        if (self.armed and self.can_buy and self.lots < p.max_lots
                and cross_up and m < 0):
            self.lots += 1
            events.append(("buy", cl, {"lot_usd": p.lot_usd, "n_lot": self.lots,
                                       "macd": m, "signal": self.sig[i_loc]}))
        # --- ПРОДАЖА ВСЕГО: пересечение вниз, линия MACD выше нуля, есть что ---
        elif (self.armed and self.lots > 0 and cross_dn and m > 0):
            self.lots = 0
            events.append(("sell_all", cl, {"macd": m,
                                            "signal": self.sig[i_loc]}))
        return events
