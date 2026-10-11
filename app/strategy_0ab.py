
"""SK System 0→A→B — порт индикатора TradingView (strategy_v6, Pine v6) на Python.

Структура (только ЛОНГ — торгуем спотом):
 - пивоты с допуском равенства: окно left+right+1 (по умолчанию 5+5),
   пивот подтверждается rightBars свечей спустя
 - точка 0: «липкая» — не смещается на внутренние откаты, пока сетап не
   отработал TP1; обновляется только вниз; страховка по возрасту max_origin_age
 - точка A: пивот-high, импульс 0→A >= min_impulse_atr * ATR(14); продлевается
   более высокими high
 - точка B: пивот-low в коррекционной зоне 0.5–0.786 от 0→A; глубже 0.786 ->
   новая точка 0; B мигрирует глубже внутри зоны
 - инвалидация: пробой точки 0 (low < p0) до активации
 - активация: закрытие выше A (state 4); после активации возврат закрытия
   ниже B = провал сетапа

Сигнал бота (режим entry_mode="zone", по умолчанию): цена вошла в зону B
(касание 0.5–0.786 от 0→A) — вход по закрытию сигнальной свечи.
Режим entry_mode="act": вход по закрытию свечи пробоя A (активация).

Money management (правила юзера, спот):
 - вход $100 с капитала $1000 (до 10 позиций, следит bot.py)
 - TP1: +2%  -> закрыть 30%
 - TP2: уровень 1.618 от B -> закрыть 100% позиции (остаток 70%)
 - SL: закрытие ниже B (провал сетапа), аварийный стоп чуть ниже точки 0
 - тайм-аут позиции: life_active свечей с момента входа

События update(h, l, o, c, i) (массивы целиком, i — текущая закрытая свеча):
 - ("sig", entry, info)  сигнал/вход, info — dict с уровнями для отчёта
 - ("part", px, frac, label)  частичная фиксация (TP1/TP2)
 - ("exit", px, reason)  полное закрытие: TP3 / SL / TIME
 - ("act",)              информационное: сетап активирован (пробой A)
 - ("cancel",)           информационное: сетап отменён/истёк
"""
from dataclasses import dataclass, field


@dataclass
class SParams:
    left: int = 5               # свечей слева от экстремума
    right: int = 5              # свечей справа (подтверждение пивота)
    atr_len: int = 14
    min_impulse_atr: float = 1.5    # мин. длина импульса 0->A в ATR
    zone_min: float = 0.5           # коррекция B: минимум (доля 0->A)
    zone_max: float = 0.786         # коррекция B: максимум
    life_forming: int = 1000        # срок жизни точки 0 без A
    max_origin_age: int = 3000      # макс. возраст точки 0
    life_pending: int = 100         # ожидание активации после подтверждения B
    life_active: int = 150          # жизнь активированного сетапа / позиции
    levels: tuple = (1.618, 1.809, 2.0)   # уровни «зелёного блока» целей
    entry_mode: str = "zone"        # zone | act — где берём сигнал
    entry_usd: float = 100.0        # фиксированный вход, $
    capital_usd: float = 1000.0
    tp1_pct: float = 2.0            # TP1: +2% ...
    tp1_frac: float = 0.30          # ... на 30% позиции
    # дальше всё: TP2 = уровень 1.618 закрывает 100% позиции
    sl_buffer_pct: float = 0.2      # аварийный стоп ниже точки 0, %
    fee_pct: float = 0.1            # спот Binance, обе стороны
    # --- ручной метод хозяина: слоистый сбор + безубыток + повторные входы ---
    be_after_tp1: bool = True       # после TP1 стоп в безубыток (цена входа)
    reentries: int = 3              # повторных входов при возврате в зону B


class SKTracker:
    """Инкрементальный детектор 0->A->B + менеджер позиции. update() строго
    слева направо по закрытым свечам. Не внутритехнический (pivots подтверждены
    с задержкой rightBars — как в Pine, это не перерисовка)."""

    def __init__(self, p: SParams):
        self.p = p
        self.h, self.l, self.o, self.c = [], [], [], []
        self.tr, self.atr = [], []
        # --- детектор ---
        self.state = 0            # 0 поиск, 1 есть 0, 2 есть 0 и A, 3 B ждёт пробоя, 4 активирован
        self.p0 = self.t0 = None
        self.pA = self.tA = None
        self.pB = self.tB = None
        self.t_born = None        # бар подтверждения B
        self.act_bar = None
        self.seq_done = False     # с точки 0 отработали TP1 — можно новую 0 выше
        self.sig_sent = False     # сигнал по текущей паре (t0, tA) уже был
        self.reentry_count = 0    # сколько повторных входов отработали
        self._last_exit_be = False  # последний выход был в безубыток (можно входить снова)
        self._last_exit_i = -1
        self.armed = True         # False во время прогона истории — детектор
                                  # строит состояние, но позиции не открывает
        # --- позиция ---
        self.pos = None

    # ---------------------------------------------------------------- utils

    def _push(self, arr, v):
        arr.append(float(v))

    def _pivots(self, i):
        """Подтверждённые на баре i пивоты (центр окна = бар i-right)."""
        p = self.p
        if i < p.left + p.right:
            return None, None
        j = i - p.right
        lo = min(self.l[j - p.left: j + p.right + 1])
        hi = max(self.h[j - p.left: j + p.right + 1])
        pL = self.l[j] if self.l[j] == lo else None
        pH = self.h[j] if self.h[j] == hi else None
        return pL, pH

    def _atr_at(self, i):
        return self.atr[i] if 0 <= i < len(self.atr) else None

    def _zone(self):
        """Коррекционная зона B: (верх 0.5, низ 0.786) для лонга."""
        rng = self.pA - self.p0
        return (self.pA - self.p.zone_min * rng,
                self.pA - self.p.zone_max * rng)

    def _levels_from(self, pB):
        rng = self.pA - self.p0
        return [(lv, pB + lv * rng) for lv in self.p.levels]

    # ---------------------------------------------------------------- update

    def update(self, h, l, o, c, i):
        p = self.p
        self._push(self.h, h[i]); self._push(self.l, l[i])
        self._push(self.o, o[i]); self._push(self.c, c[i])
        i = len(self.c) - 1                       # свой индекс по своим барам
        events = []

        # --- ATR (Wilder): сид — SMA TR на баре atr_len, далее сглаживание ---
        tr = (self.h[i] - self.l[i]) if i == 0 else max(
            self.h[i] - self.l[i],
            abs(self.h[i] - self.c[i - 1]),
            abs(self.l[i] - self.c[i - 1]))
        self.tr.append(tr)
        if i < p.atr_len:
            self.atr.append(None)
        elif i == p.atr_len:
            self.atr.append(sum(self.tr[1:p.atr_len + 1]) / p.atr_len)
        else:
            self.atr.append((self.atr[-1] * (p.atr_len - 1) + tr) / p.atr_len)

        cl, lo, hi = self.c[i], self.l[i], self.h[i]

        # --- инвалидация: пробой точки 0 до активации ---
        if self.state in (2, 3) and self.p0 is not None and lo < self.p0:
            if self.state == 3:
                events.append(("cancel",))
            self.state = 0
            self.sig_sent = False

        # --- смерть незавершённой структуры ---
        if self.state in (1, 2) and self.t0 is not None and i - self.t0 > p.life_forming:
            self.state = 0
            self.sig_sent = False

        # --- СИГНАЛ: касание зоны B (вход по закрытию) ---
        if (p.entry_mode == "zone" and self.armed and self.state == 2
                and self.pos is None and not self.sig_sent
                and self.pA is not None):
            z_top, z_bot = self._zone()
            if lo <= z_top and cl > self.p0:
                entry = cl
                pB_eff = min(max(lo, z_bot), z_top)   # оценка B по факту касания
                lv = self._levels_from(pB_eff)
                self.pos = {
                    "entry": entry, "opened_i": i, "p0": self.p0, "pA": self.pA,
                    "pB": pB_eff, "fail_px": z_bot, "cat_sl": self.p0 * (1 - p.sl_buffer_pct / 100),
                    "tp1": entry * (1 + p.tp1_pct / 100),
                    "lv": lv, "tp1_done": False, "tp2_done": False,
                    "left_frac": 1.0,
                }
                self.sig_sent = True
                events.append(("sig", entry, dict(self.pos)))

        # --- ПОВТОРНЫЙ ВХОД: цена вернулась в зону B после выхода в безубыток ---
        # (ручной метод хозяина: забрали 30% на отскоке, остаток вышел в БУ,
        # цена снова в точке B (0.5) — входим снова, глубже, от ~0.618)
        if (p.reentries > 0 and self.armed and self.pos is None
                and self._last_exit_be and i > self._last_exit_i
                and self.reentry_count < p.reentries
                and self.pA is not None and self.p0 is not None
                and cl > self.p0):
            z_top, z_bot = self._zone()
            if lo <= z_top:
                entry = cl
                pB_eff = min(max(lo, z_bot), z_top)
                lv = self._levels_from(pB_eff)
                self.pos = {
                    "entry": entry, "opened_i": i, "p0": self.p0, "pA": self.pA,
                    "pB": pB_eff, "fail_px": pB_eff, "cat_sl": self.p0 * (1 - p.sl_buffer_pct / 100),
                    "tp1": entry * (1 + p.tp1_pct / 100),
                    "lv": lv, "tp1_done": False, "tp2_done": False,
                    "left_frac": 1.0, "reentry": self.reentry_count + 1,
                }
                self.reentry_count += 1
                events.append(("sig", entry, dict(self.pos)))

        # --- активация: закрытие выше A ---
        if self.state == 3 and self.pA is not None and cl > self.pA:
            self.state = 4
            self.act_bar = i
            events.append(("act",))
            if (p.entry_mode == "act" and self.armed and self.pos is None
                    and not self.sig_sent):
                entry = cl
                lv = self._levels_from(self.pB)
                self.pos = {
                    "entry": entry, "opened_i": i, "p0": self.p0, "pA": self.pA,
                    "pB": self.pB, "fail_px": self.pB,
                    "cat_sl": self.p0 * (1 - p.sl_buffer_pct / 100),
                    "tp1": entry * (1 + p.tp1_pct / 100),
                    "lv": lv, "tp1_done": False, "tp2_done": False,
                    "left_frac": 1.0,
                }
                self.sig_sent = True
                events.append(("sig", entry, dict(self.pos)))

        # --- пивоты ---
        pL, pH = self._pivots(i)
        if pL is not None:
            j = i - p.right
            if self.state == 0 or pL < (self.p0 if self.p0 is not None else float("inf")):
                stale = self.t0 is not None and i - self.t0 > p.max_origin_age
                if self.p0 is None or pL < self.p0 or self.seq_done or stale:
                    self.p0, self.t0 = pL, j
                    self.state = 1
                    self.sig_sent = False
                    self.seq_done = False
                    self.reentry_count = 0
                    self._last_exit_be = False
            elif self.state == 2:
                rng = self.pA - self.p0
                r = (self.pA - pL) / rng if rng > 0 else 0
                if pL < self.p0:
                    self.p0, self.t0 = pL, j
                    self.state = 1
                    self.sig_sent = False
                elif p.zone_min <= r <= p.zone_max:
                    self.pB, self.tB = pL, j
                    self.t_born = i
                    self.state = 3
                    if self.pos is not None:
                        self.pos["pB"] = pL
                        self.pos["fail_px"] = pL          # провал = закрытие ниже B
                        self.pos["lv"] = self._levels_from(pL)
                elif r > p.zone_max:
                    self.p0, self.t0 = pL, j
                    self.state = 1
                    self.sig_sent = False
            elif self.state in (3, 4):
                rng = self.pA - self.p0
                r3 = (self.pA - pL) / rng if rng > 0 else 0
                if pL > self.p0 and (self.pB is None or (pL < self.pB and p.zone_min <= r3 <= p.zone_max)):
                    self.pB, self.tB = pL, j
                    self.t_born = i
                    if self.pos is not None:
                        self.pos["pB"] = pL
                        self.pos["fail_px"] = pL
                        self.pos["lv"] = self._levels_from(pL)

        if pH is not None:
            j = i - p.right
            if self.state == 1 and self.p0 is not None:
                atr_j = self._atr_at(j) or self.atr[-1]
                if atr_j and pH - self.p0 >= p.min_impulse_atr * atr_j:
                    self.pA, self.tA = pH, j
                    self.state = 2
            elif self.state == 2 and self.pA is not None:
                if pH > self.pA and j > self.tA:
                    self.pA, self.tA = pH, j

        # --- провал активированного сетапа / тайминги детектора ---
        if self.state == 3 and self.t_born is not None and i - self.t_born > p.life_pending:
            self.state = 0
            self.p0 = None
            self.sig_sent = False
            events.append(("cancel",))
        if self.state == 4 and self.act_bar is not None and i - self.act_bar > p.life_active:
            self.state = 0
            events.append(("cancel",))

        # --- менеджер позиции ---
        if self.pos is not None:
            events.extend(self._manage(i))
        return events

    # ---------------------------------------------------------------- manage

    def _manage(self, i):
        p, pos, events = self.p, self.pos, []
        cl, lo, hi = self.c[i], self.l[i], self.h[i]
        entry = pos["entry"]

        # катастрофа: ушли за точку 0 (стоп-рынок)
        if lo <= pos["cat_sl"]:
            events.append(("exit", pos["cat_sl"], "SL"))
            self._finish(i, "SL")
            return events
        # провал: закрытие ниже B
        if cl < pos["fail_px"]:
            events.append(("exit", cl, "SL"))
            self._finish(i, "SL")
            return events
        # тайм-аут
        if i - pos["opened_i"] > p.life_active:
            events.append(("exit", cl, "TIME"))
            self._finish(i, "TIME")
            return events
        # тейки: TP1 +2% на 30%, далее 1.618 закрывает 100% позиции
        if not pos["tp1_done"] and hi >= pos["tp1"]:
            pos["tp1_done"] = True
            pos["left_frac"] -= p.tp1_frac
            events.append(("part", pos["tp1"], p.tp1_frac, "TP1 +2%"))
            self.seq_done = True
            if self.state == 4:
                self.state = 0
        if pos["lv"] and hi >= pos["lv"][0][1]:
            events.append(("exit", pos["lv"][0][1], "TP2 1.618"))
            self._finish(i, "TP2")
            return events
        # безубыток: после TP1 остаток закрываем по цене входа
        if p.be_after_tp1 and pos["tp1_done"] and cl <= entry:
            events.append(("exit", entry, "BE"))
            self._finish(i, "BE")
            return events
        return events

    def _finish(self, i, reason=None):
        self._last_exit_be = (reason == "BE")
        self._last_exit_i = i
        self.pos = None
