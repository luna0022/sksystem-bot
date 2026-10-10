# -*- coding: utf-8 -*-
"""Капча: число прописью на картинке, пользователь вводит цифры.
Единая с ботами shopbot (discord/lolka) — тот же алгоритм и вид.

Число генерируется случайно (1000-9999) при КАЖДОМ запросе картинки,
язык тоже случайный: русский или английский. Слова пишутся слитно,
например "восемьтысячвосемьсотпятьдесятвосемь" -> пользователь вводит 8858.
"""
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "captcha_font.ttf"),
    "/app/app/assets/captcha_font.ttf",                     # путь внутри контейнера sksystem
    "C:\\Windows\\Fonts\\segoesc.ttf",                      # Segoe Script (локальная отладка)
    "C:\\Windows\\Fonts\\comic.ttf",                        # Comic Sans (локальная отладка)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # запасной в контейнере
]

RU_UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
RU_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
            "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
RU_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
           "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
RU_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
               "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _ru_thousand(d: int) -> str:
    if d == 1:
        return "однатысяча"
    if d == 2:
        return "дветысячи"
    if d in (3, 4):
        return RU_UNITS[d] + "тысячи"
    return RU_UNITS[d] + "тысяч"


def _ru_hundred_part(x: int) -> str:
    """0-999 без пробелов."""
    if x == 0:
        return ""
    parts = []
    h, rem = divmod(x, 100)
    if h:
        parts.append(RU_HUNDREDS[h])
    if rem >= 20:
        t, u = divmod(rem, 10)
        parts.append(RU_TENS[t])
        if u:
            parts.append(RU_UNITS[u])
    elif rem >= 10:
        parts.append(RU_TEENS[rem - 10])
    elif rem:
        parts.append(RU_UNITS[rem])
    return "".join(parts)


def num_to_words_ru(n: int) -> str:
    th, rem = divmod(n, 1000)
    return _ru_thousand(th) + _ru_hundred_part(rem)


EN_UNITS = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
EN_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty",
           "sixty", "seventy", "eighty", "ninety"]


def _en_hundred_part(x: int) -> str:
    if x == 0:
        return ""
    parts = []
    h, rem = divmod(x, 100)
    if h:
        parts.append(EN_UNITS[h] + "hundred")
    if rem >= 20:
        t, u = divmod(rem, 10)
        parts.append(EN_TENS[t])
        if u:
            parts.append(EN_UNITS[u])
    elif rem >= 10:
        parts.append(EN_TEENS[rem - 10])
    elif rem:
        parts.append(EN_UNITS[rem])
    return "".join(parts)


def num_to_words_en(n: int) -> str:
    th, rem = divmod(n, 1000)
    return EN_UNITS[th] + "thousand" + _en_hundred_part(rem)


def make_challenge(lang: str | None = None) -> tuple:
    """Новый случайный пример. Вернуть (число-строка '8858', текст для картинки, язык).

    lang: 'ru' | 'en' | None — None = случайный язык.
    """
    n = random.randint(1000, 9999)
    if lang not in ("ru", "en"):
        lang = random.choice(("ru", "en"))
    text = (num_to_words_ru if lang == "ru" else num_to_words_en)(n)
    return str(n), text, lang


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def render_png(text: str, max_width: int = 720, height: int = 180) -> bytes:
    """Нарисовать 'рукописный' текст с искажениями на светлой бумаге."""
    # светлая «бумага»
    img = Image.new("RGB", (max_width, height), (246, 241, 230))
    d = ImageDraw.Draw(img)
    for _ in range(900):
        x, y = random.randint(0, max_width - 1), random.randint(0, height - 1)
        g = random.randint(210, 240)
        d.point((x, y), fill=(g, g - 5, g - 15))

    # подбираем размер шрифта, чтобы слово влезло в холст
    size = 46
    font = None
    while size > 22:
        font = _load_font(size)
        total = int(sum(font.getlength(ch) for ch in text) * 1.05) + 7 * (size // 2) + 80
        if total <= max_width:
            break
        size -= 3
    width = min(max_width, total)
    if width < max_width:  # центрируем «бумагу» под текст
        paper = Image.new("RGB", (max_width, height), (246, 241, 230))
        d = ImageDraw.Draw(paper)
        for _ in range(900):
            x, y = random.randint(0, max_width - 1), random.randint(0, height - 1)
            g = random.randint(210, 240)
            d.point((x, y), fill=(g, g - 5, g - 15))
        img = paper
        d = ImageDraw.Draw(img)

    ink = random.choice([(28, 42, 96), (20, 20, 24), (84, 22, 40), (18, 70, 54)])

    # несколько тонких каракулей на фоне
    for _ in range(3):
        pts = []
        x0 = random.randint(-20, width // 2)
        y0 = random.randint(20, height - 20)
        for i in range(6):
            pts.append((x0 + i * width // 5, y0 + random.randint(-14, 14)))
        d.line(pts, fill=(200, 195, 185), width=1)

    # раскладка кусками по 4-8 букв: связи скриптового шрифта сохраняются,
    # каждый кусок наклонён и сдвинут по волне — «почерк»
    pad = size // 2
    chunks = []
    i = 0
    while i < len(text):
        step = min(len(text) - i, random.randint(4, 8))
        chunks.append(text[i:i + step])
        i += step
    tw = int(sum(font.getlength(c) for c in chunks) * 1.05) + pad * len(chunks) + 30
    layer = Image.new("RGBA", (tw, height), (0, 0, 0, 0))
    mid_y = height // 2
    x = 12
    for k, chunk in enumerate(chunks):
        adv = int(font.getlength(chunk))
        tile = Image.new("RGBA", (adv + pad * 2, size * 3), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        td.text((pad, size), chunk, font=font, fill=ink + (255,))
        tile = tile.rotate(random.uniform(-7, 7), expand=True, resample=Image.BICUBIC)
        wave = int(9 * math.sin(k * 1.1))
        y = mid_y - tile.height // 2 + wave + random.randint(-4, 4)
        layer.paste(tile, (x, y), tile)
        x += adv + pad + random.randint(-2, 2)
    img.paste(layer, ((width - layer.width) // 2, 0), layer)

    # пара штрихов поверх, как будто рука дёрнулась
    for _ in range(2):
        x0 = random.randint(0, max(1, width - 60))
        y0 = random.randint(30, height - 30)
        d.arc((x0, y0, x0 + random.randint(40, 90), y0 + random.randint(10, 30)),
              start=random.randint(0, 180), end=random.randint(180, 360),
              fill=(120, 118, 112), width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
