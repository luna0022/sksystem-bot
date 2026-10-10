
#!/usr/bin/env python3
"""SK System web dashboard with login security.

Auth: email + password (PBKDF2-hashed in data/users.json) AND a math image
captcha on the login form (same style as the "lolka" bot). Sessions are
HMAC-signed cookies. Without a domain the mail channel is unavailable, so
there is no email flow - password changes happen via the CLI:

    python -m app.web adduser you@example.com yourpassword

First boot: set ADMIN_EMAIL and ADMIN_PASSWORD in .env and the account is
created automatically.
"""
import base64
import hashlib
import hmac
import io
import json
import os
import random
import secrets
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

DATA = os.environ.get("DATA_DIR", "/app/data")
SECRET = os.environ.get("WEB_TOKEN", "change-me").encode()
STATE_FILE = os.path.join(DATA, "state.json")
CMD_FILE = os.path.join(DATA, "cmd.json")
CONFIG_FILE = os.path.join(DATA, "config.json")
TRADES_FILE = os.path.join(DATA, "trades.json")
USERS_FILE = os.path.join(DATA, "users.json")
SESSION_TTL = 7 * 86400
CAPTCHA_TTL = 600

app = FastAPI(title="SK System dashboard")
os.makedirs(DATA, exist_ok=True)


# ------------------------------------------------------------------ crypto

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return raw + "." + sig


def unsign(token: str) -> dict | None:
    try:
        raw, sig = token.rsplit(".", 1)
        exp = hmac.new(SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, exp):
            return None
        return json.loads(_unb64(raw))
    except Exception:
        return None


def hash_password(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return salt, dk.hex()


def check_password(password: str, salt: str, hexhash: str) -> bool:
    _, dk = hash_password(password, salt)
    return hmac.compare_digest(dk, hexhash)


def _read(path, default):
    try:
        if os.path.exists(path):
            return json.load(open(path))
    except Exception:
        pass
    return default


def users() -> dict:
    return _read(USERS_FILE, {})


def save_users(u: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(u, f)


def add_user(email: str, password: str):
    u = users()
    salt, dk = hash_password(password)
    u[email.lower()] = {"salt": salt, "hash": dk}
    save_users(u)


# first-boot admin from env
_admin = os.environ.get("ADMIN_EMAIL")
_admin_pw = os.environ.get("ADMIN_PASSWORD")
if _admin and _admin_pw and not os.path.exists(USERS_FILE):
    add_user(_admin, _admin_pw)
    print(f"[web] admin account created for {_admin}")


def current_user(request: Request) -> str | None:
    tok = request.cookies.get("sk_session")
    if not tok:
        return None
    data = unsign(tok)
    if not data or data.get("exp", 0) < time.time():
        return None
    return data.get("email")


def require_user(request: Request):
    email = current_user(request)
    if not email or email not in users():
        raise HTTPException(status_code=401, detail="login required")
    return email


# ------------------------------------------------------------------ captcha

_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
          "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
         "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def _two_digits(n: int, feminine: bool = False) -> str:
    if n == 0:
        return ""
    if n < 10:
        if feminine and n == 1:
            return "одна"
        if feminine and n == 2:
            return "две"
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    t, u = divmod(n, 10)
    return _TENS[t] + (" " + _UNITS[u] if u else "")


def num_to_words_ru(n: int) -> str:
    """0..9999 -> Russian words (the captcha text)."""
    if n == 0:
        return "ноль"
    parts = []
    th, rest = divmod(n, 1000)
    if th:
        parts.append(_two_digits(th, feminine=True))
        if th == 1:
            parts.append("тысяча")
        elif 2 <= th <= 4:
            parts.append("тысячи")
        else:
            parts.append("тысяч")
    h, rest2 = divmod(rest, 100)
    if h:
        parts.append(_HUNDREDS[h])
    tail = _two_digits(rest2)
    if tail:
        parts.append(tail)
    return " ".join(parts)


def _captcha_png(text: str) -> bytes:
    """Handwritten-number captcha (lolka-bot style): the number is drawn as
    Russian words in a script font on a paper background with noise lines."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    for cand in ("/app/fonts/MarckScript-Regular.ttf", "fonts/MarckScript-Regular.ttf",
                 os.path.join(os.path.dirname(__file__), "..", "fonts", "MarckScript-Regular.ttf")):
        if os.path.exists(cand):
            font_manager.fontManager.addfont(cand)
            hand = font_manager.FontProperties(fname=cand).get_name()
            break
    else:
        hand = "DejaVu Sans"
    fig, ax = plt.subplots(figsize=(4.6, 1.35), dpi=110)
    fig.patch.set_facecolor("#f7f1e3")
    ax.set_facecolor("#f7f1e3")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 30)
    ax.axis("off")
    for _ in range(7):
        ax.plot([secrets.randbelow(100), secrets.randbelow(100)],
                [secrets.randbelow(30), secrets.randbelow(30)],
                color="#b9ad94", lw=random.uniform(0.5, 1.2), alpha=0.7)
    ax.text(50 + random.uniform(-3, 3), 15 + random.uniform(-2, 2), text,
            fontproperties=font_manager.FontProperties(family=hand, size=26),
            color="#23282e", ha="center", va="center",
            rotation=random.uniform(-4, 4))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def new_captcha() -> tuple[str, str]:
    """Number (0..9999) rendered as Russian words; user types the digits."""
    answer = secrets.randbelow(10000)
    token = sign({"cap": str(answer), "exp": time.time() + CAPTCHA_TTL,
                  "nonce": secrets.token_hex(8)})
    return token, num_to_words_ru(answer)


# ------------------------------------------------------------------ pages

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>SK System — sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:'Segoe UI',system-ui,Arial;color:#e6edf3;
background:radial-gradient(1200px 600px at 20% -10%,#12325e33,transparent),#0b0f14}
.card{width:340px;padding:36px 32px;border-radius:16px;
background:linear-gradient(180deg,#161d27,#10151c);border:1px solid #232b36;
box-shadow:0 24px 70px #00000088,inset 0 1px 0 #ffffff10}
.logo{font-size:34px;text-align:center}
h1{font-size:19px;text-align:center;margin:8px 0 2px;font-weight:600}
p.tag{text-align:center;color:#7d8590;font-size:12px;margin:0 0 22px}
label{display:block;font-size:11px;color:#8b949e;margin:12px 0 4px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid #2b3542;background:#0b0f14;color:#e6edf3;font-size:14px;outline:none;transition:border .15s}
input:focus{border-color:#1f6feb}
.cap{margin-top:2px}.cap img{display:block;width:100%;height:auto;border-radius:8px;border:1px solid #2b3542}
.cap img{border-radius:8px;border:1px solid #2b3542;height:48px;background:#0b0f14}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;font-size:14px;font-weight:600;
background:linear-gradient(180deg,#2d7df0,#1f6feb);color:#fff;cursor:pointer;transition:filter .15s}
button:hover{filter:brightness(1.12)}
.err{color:#f85149;font-size:12px;min-height:18px;margin-top:8px;text-align:center}
.foot{text-align:center;color:#56616e;font-size:11px;margin-top:18px}</style></head><body>
<div class="card"><div class="logo">🤖</div><h1>SK System</h1><p class="tag">signal bot · impulse + Fibonacci</p>
<form method="post" action="/login">
<label>EMAIL</label><input name="email" type="email" required autocomplete="username">
<label>PASSWORD</label><input name="password" type="password" required autocomplete="current-password">
<label>Проверка: число на картинке словами</label><div class="cap"><img src="/captcha" alt="captcha"><input name="captcha" inputmode="numeric" placeholder="например: 8858" required style="margin-top:10px"></div>
<div class="err">{{ERR}}</div>
<button type="submit">Sign in</button></form>
<div class="foot">SK System · Binance spot signals</div></div></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/")
    return LOGIN_PAGE.replace("{{ERR}}", "")


@app.get("/captcha")
def captcha_image():
    token, text = new_captcha()
    png = _captcha_png(text)
    resp = Response(png, media_type="image/png")
    resp.set_cookie("sk_cap", token, max_age=CAPTCHA_TTL, httponly=True, samesite="lax")
    return resp


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email = (form.get("email") or "").lower().strip()
    password = form.get("password") or ""
    cap_in = (form.get("captcha") or "").strip()
    cap_tok = request.cookies.get("sk_cap", "")
    cap_data = unsign(cap_tok) if cap_tok else None
    fail = lambda msg: HTMLResponse(LOGIN_PAGE.replace("{{ERR}}", msg), status_code=401)
    if not cap_data or cap_data.get("exp", 0) < time.time():
        return fail("captcha expired, retry")
    try:
        if int(cap_in) != int(cap_data.get("cap")):
            return fail("wrong captcha")
    except (TypeError, ValueError):
        return fail("wrong captcha")
    u = users().get(email)
    if not u or not check_password(password, u["salt"], u["hash"]):
        return fail("wrong email or password")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sk_cap", "", max_age=0)
    resp.set_cookie("sk_session", sign({"email": email, "exp": time.time() + SESSION_TTL}),
                    max_age=SESSION_TTL, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.set_cookie("sk_session", "", max_age=0)
    return resp


# ------------------------------------------------------------------ api

def _cmd(name: str):
    with open(CMD_FILE, "w") as f:
        json.dump({"cmd": name, "ts": datetime.now(timezone.utc).isoformat()}, f)
    return {"ok": True, "cmd": name}


@app.get("/api/state")
def state(request: Request):
    require_user(request)
    st = _read(STATE_FILE, {})
    tr = _read(TRADES_FILE, {})
    st["per_pair"] = tr.get("per_pair", {})
    st["daily"] = tr.get("daily", {})
    st["config"] = _read(CONFIG_FILE, {})
    st["server_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    return JSONResponse(st)


@app.post("/api/{action}")
def action(action: str, request: Request):
    require_user(request)
    if action in ("pause", "resume", "reset", "restart"):
        return _cmd(action)
    raise HTTPException(status_code=404)


@app.post("/api/config")
async def set_config(request: Request):
    require_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400)
    cfg = _read(CONFIG_FILE, {})
    cfg.update({k: v for k, v in body.items() if v is not None})
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True, "config": cfg}


PAGE = open(os.path.join(os.path.dirname(__file__), "dashboard.html")).read() \
    if os.path.exists(os.path.join(os.path.dirname(__file__), "dashboard.html")) else ""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not current_user(request):
        return RedirectResponse("/login")
    return PAGE


# ------------------------------------------------------------------ cli

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "adduser":
        add_user(sys.argv[2], sys.argv[3])
        print("user added:", sys.argv[2])
    else:
        print("usage: python -m app.web adduser EMAIL PASSWORD")
