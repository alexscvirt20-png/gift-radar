"""
Gift Radar Pro — backend (aiohttp + стандартная библиотека Python).

Функции:
  * Жёсткий платный вход (paywall): мастер-ключи, оплата в TON по живому курсу.
  * Живые курсы: TON/USD (OKX -> Bybit), USD/BYN (НБРБ), USD/RUB (ЦБ РФ).
  * Внутренний баланс, пополнение (TON Connect / прямой перевод с Memo).
  * Покупка лотов на сайте, инвентарь, вывод подарка за 0.6 TON.
  * Лента реальных лотов Portals (scanner.py).

Сторонние зависимости: только aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp
from aiohttp import web

import scanner as sc

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = Path(os.environ.get("DB_PATH") or (BASE_DIR / "gift_radar.db"))

# =========================== настройки проекта ============================= #

OWNER_WALLET: str = os.environ.get(
    "OWNER_WALLET", "UQBUSnHH_yILiNFJOBo6lhWZdOxYyM8ABxH6O8IF74_cZQud"
).strip()

DEPOSIT_WALLET: str = os.environ.get(
    "DEPOSIT_WALLET", "UQD3U-lkGDlbRCGM2C6fPnlolt2EnQdcr5Uc6p94LWj39qEX"
).strip()

ACCESS_PRICE_USD: float = 9.99
EXTEND_PRICE_USD: float = 4.99
ACCESS_DAYS: int = 30
BASE_DEVICES: int = 3
EXTRA_DEVICES: int = 3
MASTER_KEYS: Tuple[str, ...] = ("GIFT-OWNER-777", "FRIEND-FREE-2026")

WITHDRAW_FEE_TON: float = 0.6
MARKET_FEE: float = sc.MARKET_FEE          # 2 %
RATES_TTL: int = 30
KEY_GRANT_TTL: int = ACCESS_DAYS * 86400

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def log(msg: str) -> None:
    print(f"[gift-radar] {msg}", flush=True)


# ================================ SQLite =================================== #

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id    TEXT PRIMARY KEY,
                balance    REAL NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access(
                user_id    TEXT PRIMARY KEY,
                paid_until INTEGER NOT NULL,
                plan       TEXT NOT NULL DEFAULT 'paid',
                key_used   TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                lot_id       TEXT NOT NULL,
                collection   TEXT,
                number       TEXT,
                model        TEXT,
                backdrop     TEXT,
                symbol       TEXT,
                url          TEXT,
                image        TEXT,
                buy_price    REAL,
                rec_price    REAL,
                profit_ton   REAL,
                status       TEXT NOT NULL DEFAULT 'owned',
                withdraw_to  TEXT,
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                lot_id       TEXT NOT NULL,
                url          TEXT,
                max_price    REAL,
                target_price REAL,
                status       TEXT NOT NULL DEFAULT 'queued',
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS withdrawals(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                inventory_id  INTEGER NOT NULL,
                to_username   TEXT,
                fee           REAL,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments(
                memo       TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                amount     REAL,
                kind       TEXT,
                tx_hash    TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS license_keys(
                code       TEXT PRIMARY KEY,
                vip        INTEGER NOT NULL DEFAULT 0,
                devices_limit INTEGER NOT NULL DEFAULT 3,
                valid_until INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices(
                device_id  TEXT PRIMARY KEY,
                code       TEXT NOT NULL,
                user_agent TEXT,
                created_at INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                device_id  TEXT PRIMARY KEY,
                code       TEXT NOT NULL,
                token      TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memos(
                memo       TEXT PRIMARY KEY,
                device_id  TEXT NOT NULL,
                kind       TEXT NOT NULL,
                code       TEXT,
                required_ton REAL,
                created_at INTEGER NOT NULL
            );
            """
        )
    log(f"SQLite ready: {DB_PATH}")


# =============================== утилиты =================================== #

def json_ok(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status,
                             dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


def _norm_user_id(raw: Optional[str]) -> str:
    uid = re.sub(r"[^A-Za-z0-9_\-]", "", (raw or "").strip())[:48]
    return uid or "guest"


def user_id_of(request: web.Request) -> str:
    uid = _norm_user_id(request.query.get("user_id") or request.headers.get("X-User-Id"))
    ensure_user(uid)
    return uid


def ensure_user(user_id: str) -> None:
    with _db_lock, db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance, created_at) VALUES(?,0,?)",
                     (user_id, int(time.time())))


def get_balance(user_id: str) -> float:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return round(float(row["balance"]), 6) if row else 0.0


def add_balance(user_id: str, delta: float) -> float:
    ensure_user(user_id)
    with _db_lock, db() as conn:
        conn.execute("UPDATE users SET balance=ROUND(balance+?,6) WHERE user_id=?", (delta, user_id))
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return round(float(row["balance"]), 6) if row else 0.0


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ============================= живые курсы ================================= #
#  TON/USD: OKX (data[0].last) -> Bybit.
#  USD/BYN: НБРБ (Cur_OfficialRate / Cur_Scale).
#  USD/RUB: ЦБ РФ (Valute.USD.Value).
#  Кэш 30 с, при сбое сети — последнее успешное значение.
# =========================================================================== #

RATES: Dict[str, Any] = {
    "ton_usd": None,
    "usd_byn": None,
    "usd_rub": None,
    "ton_byn": None,
    "sources": {},
    "updated_at": 0.0,
}


async def _okx_ton_usd(session: aiohttp.ClientSession) -> Optional[float]:
    url = "https://www.okx.com/api/v5/market/ticker?instId=TON-USDT"
    try:
        async with session.get(url, headers=UA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        rows = (data or {}).get("data") or []
        if rows:
            last = _to_float(rows[0].get("last"))
            if last and last > 0:
                return last
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    return None


async def _bybit_ton_usd(session: aiohttp.ClientSession) -> Optional[float]:
    url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=TONUSDT"
    try:
        async with session.get(url, headers=UA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        rows = ((data or {}).get("result") or {}).get("list") or []
        if rows:
            last = _to_float(rows[0].get("lastPrice"))
            if last and last > 0:
                return last
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    return None


async def _nbrb_usd_byn(session: aiohttp.ClientSession) -> Optional[float]:
    for url in ("https://api.nbrb.by/exrates/rates/USD?parammode=2",
                "https://www.nbrb.by/api/exrates/rates/145"):
        try:
            async with session.get(url, headers=UA_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
            rate = _to_float((data or {}).get("Cur_OfficialRate"))
            scale = _to_float((data or {}).get("Cur_Scale"), 1.0) or 1.0
            if rate and rate > 0:
                return round(rate / scale, 6)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
    return None


async def _cbr_usd_rub(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get("https://www.cbr-xml-daily.ru/daily_json.js",
                               headers=UA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        value = _to_float((((data or {}).get("Valute") or {}).get("USD") or {}).get("Value"))
        if value and value > 0:
            return value
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    return None


async def refresh_rates(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and RATES["updated_at"] and now - RATES["updated_at"] < RATES_TTL:
        return RATES

    try:
        async with aiohttp.ClientSession() as session:
            ton_usd, usd_byn, usd_rub = await asyncio.gather(
                _okx_ton_usd(session), _nbrb_usd_byn(session), _cbr_usd_rub(session)
            )
            sources = dict(RATES["sources"])
            if ton_usd:
                sources["ton_usd"] = "okx.com"
            else:
                ton_usd = await _bybit_ton_usd(session)
                if ton_usd:
                    sources["ton_usd"] = "bybit.com"

        if ton_usd:
            RATES["ton_usd"] = ton_usd
        if usd_byn:
            RATES["usd_byn"] = usd_byn
            sources["usd_byn"] = "nbrb.by"
        if usd_rub:
            RATES["usd_rub"] = usd_rub
            sources["usd_rub"] = "cbr-xml-daily.ru"
        if RATES["ton_usd"] and RATES["usd_byn"]:
            RATES["ton_byn"] = round(RATES["ton_usd"] * RATES["usd_byn"], 4)
            sources["ton_byn"] = "рассчитано"

        RATES["sources"] = sources
        if ton_usd or usd_byn or usd_rub:
            RATES["updated_at"] = now
    except Exception as exc:              # сеть недоступна — оставляем прошлые значения
        log(f"rates error: {type(exc).__name__}")
    return RATES


def ton_usd() -> float:
    return float(RATES["ton_usd"] or 0)


def ton_byn() -> float:
    return float(RATES["ton_byn"] or 0)


def rates_payload() -> Dict[str, Any]:
    return {
        "ton_usd": round(RATES["ton_usd"], 4) if RATES["ton_usd"] else None,
        "usd_byn": round(RATES["usd_byn"], 4) if RATES["usd_byn"] else None,
        "ton_byn": round(RATES["ton_byn"], 4) if RATES["ton_byn"] else None,
        "usd_rub": round(RATES["usd_rub"], 4) if RATES["usd_rub"] else None,
        "sources": RATES["sources"],
        "updated_at": int(RATES["updated_at"] or 0),
        "ttl": RATES_TTL,
    }


# ============================ платный доступ =============================== #

def access_until(user_id: str) -> int:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT paid_until FROM access WHERE user_id=?", (user_id,)).fetchone()
    return int(row["paid_until"]) if row else 0


def access_plan(user_id: str) -> str:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT plan FROM access WHERE user_id=?", (user_id,)).fetchone()
    return str(row["plan"]) if row else ""


def access_active(user_id: str) -> bool:
    return access_until(user_id) > int(time.time())


def grant_access(user_id: str, seconds: int, plan: str, key_used: str = "") -> int:
    until = int(time.time()) + int(seconds)
    with _db_lock, db() as conn:
        conn.execute(
            """INSERT INTO access(user_id, paid_until, plan, key_used, created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 paid_until = MAX(access.paid_until, excluded.paid_until),
                 plan       = excluded.plan,
                 key_used   = excluded.key_used""",
            (user_id, until, plan, key_used, int(time.time())),
        )
    return until


def access_state(user_id: str) -> Dict[str, Any]:
    now = int(time.time())
    until = access_until(user_id)
    active = until > now
    price_ton = round(ACCESS_PRICE_USD / ton_usd(), 2) if ton_usd() else None
    return {
        "active": active,
        "paid_until": until,
        "days_left": max(0, (until - now + 86399) // 86400) if active else 0,
        "plan": access_plan(user_id) if active else "",
        "price_usd": ACCESS_PRICE_USD,
        "price_ton": price_ton,
        "access_days": ACCESS_DAYS,
        "owner_wallet": OWNER_WALLET,
        "deposit_wallet": DEPOSIT_WALLET,
        "market_fee": MARKET_FEE,
        "withdraw_fee": WITHDRAW_FEE_TON,
    }


def new_pay_memo(user_id: str) -> str:
    return f"PAY-{secrets.randbelow(90000) + 10000}-{user_id[-4:].upper() or 'USER'}"


# ==================== лицензионные коды и устройства ======================= #

FOREVER = 4102444800          # 2100-01-01
VIP_CODES = {k.upper() for k in MASTER_KEYS}


def gen_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    block = lambda: "".join(secrets.choice(alphabet) for _ in range(4))
    return f"GIFT-{block()}-{block()}"


def gen_memo(prefix: str, digits: int = 5) -> str:
    return f"{prefix}-{secrets.randbelow(10 ** digits - 10 ** (digits - 1)) + 10 ** (digits - 1)}"


def key_info(code: str) -> Optional[Dict[str, Any]]:
    norm = (code or "").strip().upper()
    if not norm:
        return None
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM license_keys WHERE code=?", (norm,)).fetchone()
    if row:
        return dict(row)
    if norm in VIP_CODES:
        return {"code": norm, "vip": 1, "devices_limit": 10 ** 6, "valid_until": FOREVER,
                "created_at": 0}
    return None


def key_devices(code: str) -> List[Dict[str, Any]]:
    with _db_lock, db() as conn:
        rows = conn.execute(
            "SELECT device_id, created_at, last_seen, user_agent FROM devices WHERE code=? "
            "ORDER BY created_at ASC", (code.strip().upper(),))
    return [dict(r) for r in rows]


def key_devices_count(code: str) -> int:
    with _db_lock, db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM devices WHERE code=?",
                           (code.strip().upper(),)).fetchone()
    return int(row["n"]) if row else 0


def bind_device(code: str, device_id: str, ua: str = "") -> Dict[str, Any]:
    code = code.strip().upper()
    now = int(time.time())
    with _db_lock, db() as conn:
        if conn.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone():
            conn.execute("UPDATE devices SET last_seen=?, code=? WHERE device_id=?",
                         (now, code, device_id))
        else:
            conn.execute("INSERT INTO devices(device_id, code, user_agent, created_at, last_seen)"
                         " VALUES(?,?,?,?,?)", (device_id, code, ua[:200], now, now))
    return {"ok": True}


def session_for(device_id: str) -> Optional[Dict[str, Any]]:
    now = int(time.time())
    with _db_lock, db() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE device_id=?", (device_id,)).fetchone()
        if not session:
            return None
        row = conn.execute("SELECT * FROM license_keys WHERE code=?", (session["code"],)).fetchone()
        conn.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))
    info = dict(row) if row else {"code": session["code"], "vip": 1,
                                  "devices_limit": 10 ** 6, "valid_until": FOREVER}
    if not info["vip"] and int(info["valid_until"]) <= now:
        return {"expired": True, "code": session["code"]}
    info["token"] = session["token"]
    return info


def revoke_device(device_id: str, code: str) -> None:
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM sessions WHERE device_id=? AND code=?", (device_id, code))
        conn.execute("DELETE FROM devices WHERE device_id=? AND code=?", (device_id, code))


def drop_session(device_id: str, code: Optional[str] = None) -> None:
    with _db_lock, db() as conn:
        if code:
            conn.execute("DELETE FROM sessions WHERE device_id=? AND code=?", (device_id, code))
        else:
            conn.execute("DELETE FROM sessions WHERE device_id=?", (device_id,))


def make_session(code: str, device_id: str) -> str:
    token = secrets.token_urlsafe(24)
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO sessions(device_id, code, token, created_at) VALUES(?,?,?,?)"
                     " ON CONFLICT(device_id) DO UPDATE SET code=excluded.code, token=excluded.token",
                     (device_id, code.strip().upper(), token, int(time.time())))
    return token


def session_payload(info: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    code = str(info.get("code") or "")
    now = int(time.time())
    until = int(info.get("valid_until") or 0)
    vip = bool(info.get("vip"))
    limit = int(info.get("devices_limit") or BASE_DEVICES)
    return {
        "active": True,
        "code": code,
        "vip": vip,
        "paid_until": until,
        "days_left": 99999 if (vip or until >= FOREVER) else max(0, (until - now + 86399) // 86400),
        "devices_limit": limit,
        "devices_used": key_devices_count(code),
        "device_id": device_id,
        "price_usd": ACCESS_PRICE_USD,
        "extend_usd": EXTEND_PRICE_USD,
        "extra_devices": EXTRA_DEVICES,
        "owner_wallet": OWNER_WALLET,
        "deposit_wallet": DEPOSIT_WALLET,
    }


async def require_access(request: web.Request) -> Tuple[Optional[str], Optional[web.Response]]:
    """Проверка device_id + токена сессии. Возвращает (device_id, error_response)."""
    device_id = (request.query.get("device_id") or request.headers.get("X-Device-Id") or "").strip()
    token = (request.query.get("token") or request.headers.get("X-Access-Token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", device_id or ""):
        return None, json_ok({"ok": False, "paywall": True, "error": "Нет device_id"}, 401)
    info = session_for(device_id)
    if not info:
        return None, json_ok({"ok": False, "paywall": True, "error": "Сессия не найдена"}, 401)
    if info.get("expired"):
        return None, json_ok({"ok": False, "paywall": True, "expired": True,
                              "error": "Срок подписки истёк"}, 403)
    if not token or token != info.get("token"):
        return None, json_ok({"ok": False, "paywall": True, "error": "Неверный токен сессии"}, 401)
    return device_id, None


# ============================ API: сессия ================================== #

async def api_session(request: web.Request) -> web.Response:
    device_id = (request.query.get("device_id") or "").strip()
    token = (request.query.get("token") or "").strip()
    info = session_for(device_id) if device_id else None
    if not info or info.get("expired") or info.get("token") != token:
        return json_ok({"ok": True, "active": False,
                        "reason": "expired" if info and info.get("expired") else ""})
    return json_ok({"ok": True, **session_payload(info, device_id)})


async def api_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or "").strip()
    code = str(body.get("code") or "").strip().upper()
    ua = str(body.get("ua") or request.headers.get("User-Agent") or "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", device_id):
        return json_ok({"ok": False, "error": "Некорректный device_id"}, 400)
    info = key_info(code)
    if not info:
        return json_ok({"ok": False, "error": "Неверный код доступа"}, 403)
    if not info["vip"] and int(info["valid_until"]) <= int(time.time()):
        return json_ok({"ok": False, "error": "Срок действия кода истёк"}, 403)

    bound_codes = {d["code"] for d in key_devices(code)}
    if not info["vip"] and device_id not in [d["device_id"] for d in key_devices(code)]:
        if len(bound_codes) >= int(info["devices_limit"]):
            return json_ok({"ok": False, "limit": True,
                            "error": f"Лимит устройств для кода достигнут ({info['devices_limit']}). "
                                     f"Докупите +{EXTRA_DEVICES} устройства за ${EXTEND_PRICE_USD}",
                            "extend_usd": EXTEND_PRICE_USD, "price_usd": ACCESS_PRICE_USD,
                            "extra_devices": EXTRA_DEVICES,
                            "extend_ton": round(EXTEND_PRICE_USD / ton_usd(), 2) if ton_usd() else None,
                            "owner_wallet": OWNER_WALLET}, 409)
    bind_device(code, device_id, ua)
    token = make_session(code, device_id)
    log(f"login ok: device={device_id} code={code[:9]}…")
    return json_ok({"ok": True, "token": token, **session_payload(key_info(code), device_id)})


def _device_codes(device_id: str) -> set:
    with _db_lock, db() as conn:
        return {r["code"] for r in conn.execute("SELECT code FROM devices WHERE device_id=?",
                                                (device_id,))}


async def api_logout(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or "").strip()
    if device_id:
        drop_session(device_id)
    return json_ok({"ok": True})


async def api_register_memo(request: web.Request) -> web.Response:
    """Memo для оплаты подписки (kind=sub) или доп. устройств (kind=ext)."""
    device_id = (request.query.get("device_id") or "").strip()
    kind = "ext" if (request.query.get("kind") == "ext") else "sub"
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", device_id):
        return json_ok({"ok": False, "error": "Некорректный device_id"}, 400)
    await refresh_rates()
    base = ton_usd()
    if not base:
        return json_ok({"ok": False, "error": "Курс TON недоступен, повторите позже"}, 503)
    usd = EXTEND_PRICE_USD if kind == "ext" else ACCESS_PRICE_USD
    required_ton = round(usd / base, 2)
    memo = gen_memo("EXT" if kind == "ext" else "SUB")
    with _db_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO memos(memo, device_id, kind, required_ton, created_at)"
                     " VALUES(?,?,?,?,?)", (memo, device_id, kind, required_ton, int(time.time())))
    return json_ok({"ok": True, "memo": memo, "kind": kind, "price_usd": usd,
                    "required_ton": required_ton, "ton_usd": round(base, 4),
                    "owner_wallet": OWNER_WALLET, "days": ACCESS_DAYS,
                    "extra_devices": EXTRA_DEVICES})


async def api_register_check(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or "").strip()
    memo = re.sub(r"[^A-Za-z0-9\-]", "", str(body.get("memo") or ""))[:32].upper()
    if not device_id or not memo:
        return json_ok({"ok": False, "error": "Нет device_id или Memo"}, 400)
    with _db_lock, db() as conn:
        row = conn.execute("SELECT * FROM memos WHERE memo=?", (memo,)).fetchone()
    if not row or row["device_id"] != device_id:
        return json_ok({"ok": False, "error": "Memo не найден для этого устройства"}, 404)
    await refresh_rates()

    with _db_lock, db() as conn:
        paid = conn.execute("SELECT 1 FROM payments WHERE memo=?", (memo,)).fetchone()
    if not paid:
        try:
            events = await fetch_ton_events(OWNER_WALLET, 20)
        except Exception as exc:
            return json_ok({"ok": False, "error": f"tonapi.io недоступен: {type(exc).__name__}"}, 502)
        amount, tx_hash = find_ton_payment(events, memo, OWNER_WALLET)
        required = float(row["required_ton"] or 0)
        if amount <= 0:
            return json_ok({"ok": True, "opened": False, "required_ton": required,
                            "message": "Перевод с этим Memo пока не найден"})
        if amount + 0.01 < required:
            return json_ok({"ok": True, "opened": False, "required_ton": required,
                            "paid_ton": amount,
                            "message": f"Оплачено {amount:g} TON, требуется {required:g} TON"})
        with _db_lock, db() as conn:
            conn.execute("INSERT OR IGNORE INTO payments(memo,user_id,amount,kind,tx_hash,created_at)"
                         " VALUES(?,?,?,?,?,?)",
                         (memo, device_id, amount, str(row["kind"]), tx_hash, int(time.time())))

    now = int(time.time())
    extra_devices = 0
    code = ""
    first_activation = False
    if row["kind"] == "ext":
        code = str(row["code"] or "").upper()
        if not code:
            return json_ok({"ok": False, "error": "Код не привязан к оплате"}, 400)
        info = key_info(code)
        if not info:
            return json_ok({"ok": False, "error": "Код не найден"}, 404)
        new_limit = int(info["devices_limit"]) + EXTRA_DEVICES
        with _db_lock, db() as conn:
            conn.execute("UPDATE license_keys SET devices_limit=? WHERE code=?", (new_limit, code))
        extra_devices = EXTRA_DEVICES
    else:
        code = gen_code()
        while key_info(code):
            code = gen_code()
        valid_until = now + ACCESS_DAYS * 86400
        with _db_lock, db() as conn:
            conn.execute("INSERT INTO license_keys(code, vip, devices_limit, valid_until, created_at)"
                         " VALUES(?,0,?,?,?)", (code, BASE_DEVICES, valid_until, now))
        first_activation = True

    bind_device(code, device_id)
    token = make_session(code, device_id)
    return json_ok({"ok": True, "opened": True, "code": code, "token": token,
                    "extra_devices": extra_devices, "first_activation": first_activation,
                    **session_payload(key_info(code), device_id)})


async def api_register_extend_for(request: web.Request) -> web.Response:
    """Memo для доп. устройств по существующему коду (если лимит исчерпан)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or "").strip()
    code = str(body.get("code") or "").strip().upper()
    info = key_info(code)
    if not info or info["vip"]:
        return json_ok({"ok": False, "error": "Расширение недоступно для этого кода"}, 400)
    await refresh_rates()
    if not ton_usd():
        return json_ok({"ok": False, "error": "Курс TON недоступен"}, 503)
    required_ton = round(EXTEND_PRICE_USD / ton_usd(), 2)
    memo = gen_memo("EXT")
    with _db_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO memos(memo, device_id, kind, code, required_ton, created_at)"
                     " VALUES(?,?,?,?,?,?)", (memo, device_id, "ext", code, required_ton,
                                              int(time.time())))
    return json_ok({"ok": True, "memo": memo, "code": code, "price_usd": EXTEND_PRICE_USD,
                    "required_ton": required_ton, "ton_usd": round(ton_usd(), 4),
                    "owner_wallet": OWNER_WALLET, "extra_devices": EXTRA_DEVICES})


async def api_devices(request: web.Request) -> web.Response:
    device_id, err = await require_access(request)
    if err:
        return err
    info = session_for(device_id)
    code = str(info["code"])
    devices = key_devices(code)
    return json_ok({"ok": True, "code": code,
                    "devices": [{"device_id": d["device_id"],
                                 "short": d["device_id"][:6] + "…" + d["device_id"][-4:],
                                 "current": d["device_id"] == device_id,
                                 "created_at": d["created_at"],
                                 "last_seen": d["last_seen"],
                                 "user_agent": d["user_agent"] or ""} for d in devices],
                    "limit": int(info.get("devices_limit") or BASE_DEVICES),
                    "vip": bool(info.get("vip"))})


async def api_device_revoke(request: web.Request) -> web.Response:
    device_id, err = await require_access(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = str(body.get("target_device") or "").strip()
    if not target or target == device_id:
        return json_ok({"ok": False, "error": "Нельзя отвязать текущее устройство"}, 400)
    info = session_for(device_id)
    revoke_device(target, str(info["code"]))
    return json_ok({"ok": True})


# ------------------------------------------------------------------------ #


def new_deposit_memo(user_id: str) -> str:
    return f"GR-{secrets.token_hex(3).upper()}-{user_id[-5:].upper() or 'USER'}"


async def fetch_ton_events(wallet: str, limit: int = 20) -> List[Dict[str, Any]]:
    url = f"https://tonapi.io/v2/accounts/{quote(wallet, safe='')}/events?limit={int(limit)}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=UA_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=12)) as resp:
            data = await resp.json(content_type=None)
    return list((data or {}).get("events") or [])


def find_ton_payment(events: List[Dict[str, Any]], memo: str,
                     wallet: str) -> Tuple[float, str]:
    """Ищет входящие TonTransfer с указанным Memo. Возвращает (сумма TON, хэш)."""
    total_nano = 0
    tx_hash = ""
    target = memo.strip().upper()
    for event in events or []:
        if event.get("in_progress"):
            continue
        for action in (event.get("actions") or []):
            status = str(action.get("status") or "").lower()
            if status not in ("ok", "success", ""):
                continue
            transfer = action.get("TonTransfer")
            if not isinstance(transfer, dict):
                continue
            comment = str(transfer.get("message") or transfer.get("comment") or "").strip()
            if comment.upper() != target:
                continue
            recipient = str(((transfer.get("recipient") or {}).get("address")) or "")
            if recipient and wallet and recipient != wallet:
                continue
            amount = _to_float(transfer.get("amount"), 0.0) or 0.0
            if amount > 0:
                total_nano += amount
                tx_hash = tx_hash or str(event.get("event_id") or action.get("trace_id") or "")
    return round(total_nano / 1e9, 6), tx_hash


# ================================ страницы ================================= #

async def index(request: web.Request) -> web.Response:
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as fh:
        html = fh.read()
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Gift-Radar-Version": "2026.11.14-landing",
        },
    )


async def manifest(request: web.Request) -> web.Response:
    """TON Connect manifest с динамическим роутом под текущий хост."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme or "http").split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host") or request.host
    base = f"{scheme}://{host}"
    body = {
        "url": base,
        "name": "Gift Radar Pro",
        "iconUrl": f"{base}/icon.svg",
        "termsOfUseUrl": f"{base}/",
        "privacyPolicyUrl": f"{base}/",
    }
    return web.json_response(body, headers={"Cache-Control": "no-store"})


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 120 120">
<defs><linearGradient id="g" x1="20" y1="20" x2="100" y2="105" gradientUnits="userSpaceOnUse">
<stop stop-color="#FFE066"/><stop offset="1" stop-color="#FFB300"/></linearGradient></defs>
<rect width="120" height="120" rx="26" fill="#090B10"/>
<path d="M30 66c-8 2-14 10-14 22 0 8 4 12 10 12h8V66z" fill="#F5A400"/>
<path d="M38 52c0-16 12-28 27-28s27 12 27 28v30c0 12-10 20-25 20H58c-13 0-20-8-20-20V52z" fill="url(#g)"/>
<path d="M66 30c-3-8 2-15 10-16 7-1 12 3 12 9 0 5-4 8-9 8h-2" stroke="#F5A400" stroke-width="6" stroke-linecap="round" fill="none"/>
<circle cx="48" cy="56" r="15" fill="#0B1622" stroke="#22C3FF" stroke-width="3"/>
<circle cx="72" cy="56" r="15" fill="#0B1622" stroke="#22C3FF" stroke-width="3"/>
<circle cx="48" cy="56" r="2" fill="#7CFFCB"/><circle cx="72" cy="56" r="2" fill="#7CFFCB"/>
</svg>"""


async def icon(request: web.Request) -> web.Response:
    return web.Response(text=ICON_SVG, content_type="image/svg+xml")


# ============================== API: курсы ================================= #

async def api_rates(request: web.Request) -> web.Response:
    try:
        await refresh_rates(force=request.query.get("refresh") == "1")
    except Exception as exc:
        log(f"api_rates: {type(exc).__name__}")
    return json_ok({"ok": True, "rates": rates_payload()})


# ============================ API: доступ ================================== #

async def api_access(request: web.Request) -> web.Response:
    await refresh_rates()
    return json_ok({"ok": True, **access_state(_norm_user_id(request.query.get("user_id")))})


async def api_access_memo(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    await refresh_rates()
    return json_ok({"ok": True, "memo": new_pay_memo(uid), **access_state(uid)})


async def api_access_unlock(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = str(body.get("key") or request.query.get("key") or "").strip()
    if not key:
        return json_ok({"ok": False, "error": "Введите ключ"}, 400)
    if key.upper() not in {k.upper() for k in MASTER_KEYS}:
        return json_ok({"ok": False, "error": "Неверный ключ доступа"}, 403)
    log(f"access granted by key: {uid} -> {key[:12]}")
    grant_access(uid, KEY_GRANT_TTL, "vip", key.upper())
    return json_ok({"ok": True, "opened": True, "key": key.upper(),
                    "paid_until": access_until(uid), **access_state(uid)})


async def api_access_check(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    await refresh_rates()
    memo = str(request.query.get("memo") or "").strip().upper()
    if not memo:
        return json_ok({"ok": False, "error": "Нет Memo"}, 400)
    if not ton_usd():
        return json_ok({"ok": False, "error": "Курс TON временно недоступен"}, 503)

    need = round(ACCESS_PRICE_USD / ton_usd(), 2)
    with _db_lock, db() as conn:
        paid_row = conn.execute("SELECT memo FROM payments WHERE memo=?", (memo,)).fetchone()
    if paid_row:
        grant_access(uid, KEY_GRANT_TTL, "paid", memo)
        return json_ok({"ok": True, "opened": True, "key": memo,
                        "paid_until": access_until(uid), **access_state(uid)})

    try:
        events = await fetch_ton_events(OWNER_WALLET, 20)
    except Exception as exc:
        return json_ok({"ok": False, "error": f"tonapi.io недоступен: {type(exc).__name__}"}, 502)

    paid, tx_hash = find_ton_payment(events, memo, OWNER_WALLET)
    if paid <= 0:
        return json_ok({"ok": True, "opened": False, "need_ton": need,
                        "message": "Перевод с этим Memo пока не найден"}, 200)
    if paid + 0.02 < need:
        return json_ok({"ok": True, "opened": False, "need_ton": need, "paid_ton": paid,
                        "message": f"Оплачено {paid:g} TON, требуется {need:g} TON"}, 200)

    with _db_lock, db() as conn:
        conn.execute("INSERT OR IGNORE INTO payments(memo,user_id,amount,kind,tx_hash,created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (memo, uid, paid, "access", tx_hash, int(time.time())))
    grant_access(uid, KEY_GRANT_TTL, "paid", memo)
    return json_ok({"ok": True, "opened": True, "paid_ton": paid, "key": memo,
                    "paid_until": access_until(uid), **access_state(uid)})


async def api_donate(request: web.Request) -> web.Response:
    await refresh_rates()
    return json_ok({"ok": True, "owner_wallet": OWNER_WALLET,
                    "ton_usd": round(ton_usd(), 4), "ton_byn": round(ton_byn(), 4)})


# ============================ API: баланс ================================== #

def balance_payload(user_id: str) -> Dict[str, Any]:
    balance = get_balance(user_id)
    return {
        "user_id": user_id,
        "balance_ton": balance,
        "balance_usd": round(balance * ton_usd(), 2),
        "balance_byn": round(balance * ton_byn(), 2),
        "deposit_wallet": DEPOSIT_WALLET,
        "withdraw_fee": WITHDRAW_FEE_TON,
        "market_fee": MARKET_FEE,
    }


async def api_balance(request: web.Request) -> web.Response:
    device_id = (request.query.get("device_id") or "").strip()
    await refresh_rates()
    return json_ok({"ok": True, "user_id": device_id, **balance_payload(device_id or "guest")})


async def api_topup_memo(request: web.Request) -> web.Response:
    device_id = (request.query.get("device_id") or "").strip() or "guest"
    return json_ok({"ok": True, "memo": new_deposit_memo(device_id),
                    "deposit_wallet": DEPOSIT_WALLET})


async def api_topup_check(request: web.Request) -> web.Response:
    uid = (request.query.get("device_id") or "").strip() or "guest"
    await refresh_rates()
    memo = str(request.query.get("memo") or "").strip()
    if not memo:
        return json_ok({"ok": False, "error": "Нет Memo"}, 400)

    with _db_lock, db() as conn:
        done = conn.execute("SELECT amount FROM payments WHERE memo=? AND kind='deposit'",
                            (memo,)).fetchone()
    if done:
        return json_ok({"ok": True, "credited": False, "amount": done["amount"],
                        "message": "Этот платёж уже зачислен", **balance_payload(uid)})

    try:
        events = await fetch_ton_events(DEPOSIT_WALLET, 30)
    except Exception as exc:
        return json_ok({"ok": False, "error": f"tonapi.io недоступен: {type(exc).__name__}"}, 502)

    amount, tx_hash = find_ton_payment(events, memo, DEPOSIT_WALLET)
    if amount <= 0:
        return json_ok({"ok": True, "credited": False,
                        "message": "Перевод с этим Memo пока не найден"})

    with _db_lock, db() as conn:
        conn.execute("INSERT OR IGNORE INTO payments(memo,user_id,amount,kind,tx_hash,created_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (memo, uid, amount, "deposit", tx_hash, int(time.time())))
    new_balance = add_balance(uid, amount)
    return json_ok({"ok": True, "credited": True, "amount": amount,
                    "message": f"Зачислено {amount:g} TON", **balance_payload(uid),
                    "balance_ton": new_balance})


# ============================= API: лента ================================== #

async def api_feed(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    await refresh_rates()

    with _db_lock, db() as conn:
        taken = {row["lot_id"] for row in conn.execute(
            "SELECT lot_id FROM inventory WHERE user_id=?", (uid,))}

    usd, byn = ton_usd(), ton_byn()
    items: List[Dict[str, Any]] = []
    for lot in sc.SCANNER.lots:
        if lot["id"] in taken:
            continue
        items.append({**lot,
                      "profit_usd": round(lot["profit_ton"] * usd, 2),
                      "profit_byn": round(lot["profit_ton"] * byn, 2)})

    return json_ok({
        "ok": True,
        "status": "ok" if items else ("waiting" if not sc.SCANNER.lots else "empty"),
        "message": (sc.STATUS_NO_TOKENS if sc.SCANNER.tokens_missing()
                    else "Лента пуста — Portals не вернул лоты"),
        "items": items,
        "total": len(items),
        "errors": sc.SCANNER.errors,
        "updated_at": int(sc.SCANNER.updated_at or 0),
        "rates": rates_payload(),
        "fee": MARKET_FEE,
        "balance_ton": get_balance(uid),
    })


# ========================== API: покупка на сайте ========================== #

async def api_buy(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    await refresh_rates()
    try:
        body = await request.json()
    except Exception:
        body = {}

    lot_id = str(body.get("lot_id") or "")
    lot = next((item for item in sc.SCANNER.lots if item["id"] == lot_id), None)
    if not lot:
        return json_ok({"ok": False, "error": "Лот уже недоступен в ленте"}, 404)

    price = round(float(lot["price"]), 6)
    balance = get_balance(uid)
    if balance + 1e-9 < price:
        return json_ok({"ok": False, "error": f"Недостаточно средств: нужно {price:.2f} TON, "
                                              f"на балансе {balance:.2f} TON",
                        **balance_payload(uid)}, 402)

    with _db_lock, db() as conn:
        dup = conn.execute("SELECT id FROM inventory WHERE user_id=? AND lot_id=?",
                           (uid, lot_id)).fetchone()
        if dup:
            return json_ok({"ok": False, "error": "Подарок уже куплен", **balance_payload(uid)}, 409)
        cursor = conn.execute(
            """INSERT INTO inventory(user_id, lot_id, collection, number, model, backdrop, symbol,
                                     url, image, buy_price, rec_price, profit_ton, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'owned', ?)""",
            (uid, lot_id, lot["collection"], lot["number"], lot["model"], lot["backdrop"],
             lot["symbol"], lot["url"], lot["image"], price, lot["rec_price"],
             lot["profit_ton"], int(time.time())),
        )
        inventory_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO orders(user_id, lot_id, url, max_price, target_price, status, created_at)
               VALUES(?,?,?,?,?, 'queued', ?)""",
            (uid, lot_id, lot["url"], price, lot["rec_price"], int(time.time())),
        )

    new_balance = add_balance(uid, -price)
    sc.SCANNER.lots = [item for item in sc.SCANNER.lots if item["id"] != lot_id]
    return json_ok({"ok": True, "inventory_id": inventory_id,
                    "bought": lot["collection"], "number": lot["number"], "price": price,
                    **balance_payload(uid), "balance_ton": new_balance})


# ============================ API: инвентарь =============================== #

async def api_inventory(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    await refresh_rates()
    with _db_lock, db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM inventory WHERE user_id=? ORDER BY created_at DESC", (uid,))]
    return json_ok({"ok": True, "items": rows, "count": len(rows), **balance_payload(uid)})


async def api_withdraw(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    await refresh_rates()
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        inventory_id = int(body.get("inventory_id") or 0)
    except (TypeError, ValueError):
        inventory_id = 0
    to_username = str(body.get("to") or "").strip()

    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", to_username):
        return json_ok({"ok": False, "error": "Укажите получателя в формате @username"}, 400)

    with _db_lock, db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE id=? AND user_id=?",
                            (inventory_id, uid)).fetchone()
    if not item:
        return json_ok({"ok": False, "error": "Подарок не найден в инвентаре"}, 404)
    if item["status"] != "owned":
        return json_ok({"ok": False, "error": "По этому подарку уже создана заявка"}, 409)

    balance = get_balance(uid)
    if balance + 1e-9 < WITHDRAW_FEE_TON:
        return json_ok({"ok": False,
                        "error": f"Нужно {WITHDRAW_FEE_TON:g} TON на балансе для комиссии "
                                 f"(у вас {balance:.2f} TON)",
                        **balance_payload(uid)}, 402)

    status = f"Заявка на вывод для {to_username} создана"
    with _db_lock, db() as conn:
        conn.execute("UPDATE inventory SET status=?, withdraw_to=? WHERE id=?",
                     (status, to_username, inventory_id))
        conn.execute("INSERT INTO withdrawals(user_id, inventory_id, to_username, fee, status, created_at)"
                     " VALUES(?,?,?,?, 'pending', ?)",
                     (uid, inventory_id, to_username, WITHDRAW_FEE_TON, int(time.time())))

    new_balance = add_balance(uid, -WITHDRAW_FEE_TON)
    return json_ok({"ok": True, "status": status, "fee": WITHDRAW_FEE_TON,
                    **balance_payload(uid), "balance_ton": new_balance})


# ======================== API: корзина / заказы ============================ #

async def api_orders(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    with _db_lock, db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (uid,))]
    return json_ok({"ok": True, "orders": rows, "count": len(rows)})


async def api_withdrawals(request: web.Request) -> web.Response:
    uid, err = await require_access(request)
    if err:
        return err
    with _db_lock, db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (uid,))]
    return json_ok({"ok": True, "withdrawals": rows, "count": len(rows)})


# =============================== lifecycle ================================= #

async def _rates_loop() -> None:
    while True:
        await asyncio.sleep(RATES_TTL)
        try:
            await refresh_rates(force=True)
        except Exception:
            pass


async def on_startup(app: web.Application) -> None:
    init_db()
    try:
        await refresh_rates(force=True)
    except Exception as exc:
        log(f"startup rates error: {type(exc).__name__}")
    try:
        await sc.SCANNER.poll_once()
    except Exception as exc:
        log(f"startup scan error: {type(exc).__name__}")
    tasks = [
        asyncio.create_task(_rates_loop()),
        asyncio.create_task(sc.SCANNER.run_forever(sc.SCAN_INTERVAL)),
    ]
    app["tasks"] = tasks
    log(f"Started. Portals token: {'set' if sc.PORTALS.enabled() else 'missing'}")


async def on_cleanup(app: web.Application) -> None:
    for task in app.get("tasks", []):
        task.cancel()


def create_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 512)
    app.router.add_get("/", index)
    app.router.add_get("/icon.svg", icon)
    app.router.add_get("/tonconnect-manifest.json", manifest)

    app.router.add_get("/api/rates", api_rates)

    app.router.add_get("/api/access", api_access)
    app.router.add_get("/api/access/memo", api_access_memo)
    app.router.add_get("/api/access/check", api_access_check)
    app.router.add_post("/api/access/unlock", api_access_unlock)
    app.router.add_get("/api/donate", api_donate)

    # сессии по device_id / лицензионные коды
    app.router.add_get("/api/session", api_session)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/register/memo", api_register_memo)
    app.router.add_post("/api/register/check", api_register_check)
    app.router.add_post("/api/register/extend", api_register_extend_for)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/devices/revoke", api_device_revoke)

    app.router.add_get("/api/balance", api_balance)
    app.router.add_get("/api/topup/memo", api_topup_memo)
    app.router.add_get("/api/topup/check", api_topup_check)

    app.router.add_get("/api/feed", api_feed)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_get("/api/inventory", api_inventory)
    app.router.add_post("/api/withdraw", api_withdraw)
    app.router.add_get("/api/orders", api_orders)
    app.router.add_get("/api/withdrawals", api_withdrawals)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


port = int(os.environ.get("PORT", 8080))

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=port)
