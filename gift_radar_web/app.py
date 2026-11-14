"""Gift Radar Pro — aiohttp backend: радар, внутренний баланс, инвентарь, выводы."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from aiohttp import web

import scanner as sc

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "gift_radar.db"

# ----------------------------- настройки проекта ----------------------------- #

DEPOSIT_WALLET: str = os.environ.get(
    "DEPOSIT_WALLET", "UQD3U-lkGDlbRCGM2C6fPnlolt2EnQdcr5Uc6p94LWj39qEX"
).strip()
WITHDRAW_FEE_TON: float = 0.6
MARKET_FEE: float = sc.MARKET_FEE          # 2 %
RATES_TTL = 30
UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}

# ------------------------------ SQLite -------------------------------------- #

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            lot_id TEXT NOT NULL,
            collection TEXT, number TEXT, model TEXT, backdrop TEXT, symbol TEXT,
            market TEXT, market_label TEXT, url TEXT, image TEXT,
            buy_price REAL, rec_price REAL, profit_ton REAL,
            status TEXT DEFAULT 'owned', withdraw_to TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, lot_id TEXT NOT NULL, url TEXT,
            max_price REAL, target_price REAL, status TEXT DEFAULT 'queued',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, inventory_id INTEGER NOT NULL,
            to_username TEXT, fee REAL, status TEXT DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments(
            user_id TEXT NOT NULL, memo TEXT PRIMARY KEY,
            amount REAL, tx_hash TEXT, created_at INTEGER NOT NULL
        );
        """)


def ensure_user(user_id: str) -> None:
    with _db_lock, db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, balance, created_at) VALUES(?,0,?)",
                  (user_id, int(time.time())))


def get_balance(user_id: str) -> float:
    with _db_lock, db() as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return round(float(row["balance"]), 6) if row else 0.0


def add_balance(user_id: str, delta: float) -> float:
    ensure_user(user_id)
    with _db_lock, db() as c:
        c.execute("UPDATE users SET balance=ROUND(balance+?,6) WHERE user_id=?", (delta, user_id))
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return round(float(row["balance"]), 6)


# ------------------------------ курсы --------------------------------------- #
#  TON/USD — KuCoin, USD/BYN — НБРБ (Cur_OfficialRate / 10000), USD/RUB — CBR.
#  Кэш 30 с, при сбое сети — последнее успешное значение.
# ---------------------------------------------------------------------------- #

RATES: Dict[str, Any] = {
    "data": {"ton_usd": None, "usd_byn": None, "usd_rub": None, "ton_byn": None},
    "updated_at": 0.0,
    "source": {},
}


async def _kucoin_ton_usd(s: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with s.get("https://api.kucoin.com/api/v1/market/stats?symbol=TON-USDT",
                         headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            last = ((d or {}).get("data") or {}).get("last")
            return float(last) if last else None
    except Exception:
        return None


async def _nbrb_usd_byn(s: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with s.get("https://api.nbrb.by/exrates/rates/USD?parammode=2",
                         headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            rate = d.get("Cur_OfficialRate")
            if rate:
                scale = d.get("Cur_Scale") or 1
                return round(float(rate) / float(scale), 6)
    except Exception:
        pass
    try:  # резервный официальный фид НБРБ
        async with s.get("https://www.nbrb.by/api/exrates/rates/145",
                         headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            rate = d.get("Cur_OfficialRate")
            if rate:
                return round(float(rate) / float(d.get("Cur_Scale") or 1), 6)
    except Exception:
        pass
    return None


async def _cbr_usd_rub(s: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with s.get("https://www.cbr-xml-daily.ru/daily_json.js",
                         headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            v = (((d or {}).get("Valute") or {}).get("USD") or {}).get("Value")
            return float(v) if v else None
    except Exception:
        return None


async def refresh_rates(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and RATES["updated_at"] and now - RATES["updated_at"] < RATES_TTL:
        return RATES
    async with aiohttp.ClientSession() as s:
        ton_usd, usd_byn, usd_rub = await asyncio.gather(
            _kucoin_ton_usd(s), _nbrb_usd_byn(s), _cbr_usd_rub(s)
        )
    d = RATES["data"]
    src = RATES["source"]
    if ton_usd:
        d["ton_usd"], src["ton_usd"] = ton_usd, "kucoin"
    if usd_byn:
        d["usd_byn"], src["usd_byn"] = usd_byn, "nbrb.by"
    if usd_rub:
        d["usd_rub"], src["usd_rub"] = usd_rub, "cbr-xml-daily.ru"
    if d["ton_usd"] and d["usd_byn"]:
        d["ton_byn"] = round(d["ton_usd"] * d["usd_byn"], 4)
        src["ton_byn"] = "рассчитано"
    RATES["updated_at"] = now
    return RATES


def ton_usd() -> float:
    return float(RATES["data"]["ton_usd"] or 0)


def ton_byn() -> float:
    return float(RATES["data"]["ton_byn"] or 0)


# ------------------------------ утилиты ------------------------------------- #

def json_ok(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def user_id_of(request: web.Request) -> str:
    uid = (request.query.get("user_id") or request.headers.get("X-User-Id") or "").strip()
    uid = re.sub(r"[^A-Za-z0-9_\-]", "", uid)[:48]
    if not uid:
        uid = "guest"
    ensure_user(uid)
    return uid


def _num(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def balance_payload(user_id: str) -> Dict[str, Any]:
    bal = get_balance(user_id)
    return {"user_id": user_id, "balance_ton": bal,
            "balance_usd": round(bal * ton_usd(), 2),
            "balance_byn": round(bal * ton_byn(), 2),
            "usd_rate": ton_usd(), "byn_rate": ton_byn(),
            "deposit_wallet": DEPOSIT_WALLET, "withdraw_fee": WITHDRAW_FEE_TON}


def make_memo(user_id: str) -> str:
    return f"GR-{user_id[-10:].upper()}-{int(time.time()) % 100000}"


# ------------------------------ страницы ------------------------------------ #

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(BASE_DIR / "templates" / "index.html")


# ------------------------------ API: курсы ---------------------------------- #

async def api_rates(request: web.Request) -> web.Response:
    r = await refresh_rates(force=request.query.get("refresh") == "1")
    return json_ok({"rates": r["data"], "source": r["source"],
                    "updated_at": int(r["updated_at"] or 0), "ttl": RATES_TTL})


# ------------------------------ API: баланс --------------------------------- #

async def api_balance(request: web.Request) -> web.Response:
    return json_ok(balance_payload(user_id_of(request)))


async def api_topup_memo(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    return json_ok({"ok": True, "memo": make_memo(uid), "deposit_wallet": DEPOSIT_WALLET})


async def api_topup_check(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    memo = (request.query.get("memo") or "").strip().upper()
    if not DEPOSIT_WALLET:
        return json_ok({"ok": False, "error": "DEPOSIT_WALLET не задан в app.py / переменных окружения"}, 400)
    if not memo:
        return json_ok({"ok": False, "error": "Нет Memo"}, 400)

    # уже зачислено?
    with _db_lock, db() as c:
        row = c.execute("SELECT amount, tx_hash FROM payments WHERE memo=? AND user_id=?", (memo, uid)).fetchone()
    if row:
        return json_ok({"ok": True, "credited": False, "amount": row["amount"],
                        "message": "Этот платёж уже зачислен", **balance_payload(uid)})

    total_nano = 0
    tx_hash = ""
    try:
        async with aiohttp.ClientSession() as s:
            url = f"https://tonapi.io/v2/accounts/{quote(DEPOSIT_WALLET, safe='')}/events?limit=50"
            async with s.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=12)) as r:
                data = await r.json(content_type=None)
                events = (data or {}).get("events") or []
                for ev in events:
                    if ev.get("in_progress"):
                        continue
                    for act in (ev.get("actions") or []):
                        status = (act.get("status") or "").lower()
                        if status not in ("ok", "success", ""):
                            continue
                        if act.get("type") != "TonTransfer":
                            # встречается и вложенный формат {"TonTransfer": {...}}
                            if "TonTransfer" not in act:
                                continue
                        tt = act.get("TonTransfer") or {}
                        recipient = ((tt.get("recipient") or {}).get("address") or "")
                        if recipient and recipient != DEPOSIT_WALLET:
                            continue
                        body_text = (tt.get("message") or tt.get("comment") or "").strip()
                        if body_text.upper() != memo:
                            continue
                        amount = float(tt.get("amount") or 0)
                        if amount > 0:
                            total_nano += amount
                            tx_hash = tx_hash or str(ev.get("event_id") or act.get("trace_id") or "")
    except Exception as e:
        return json_ok({"ok": False, "error": f"tonapi.io недоступен: {type(e).__name__}"}, 502)

    if total_nano <= 0:
        return json_ok({"ok": True, "credited": False, "message": "Транзакция с этим Memo пока не найдена"})

    amount_ton = round(total_nano / 1e9, 6)
    with _db_lock, db() as c:
        c.execute("INSERT OR IGNORE INTO payments(user_id, memo, amount, tx_hash, created_at) VALUES(?,?,?,?,?)",
                  (uid, memo, amount_ton, tx_hash, int(time.time())))
    new_bal = add_balance(uid, amount_ton)
    return json_ok({"ok": True, "credited": True, "amount": amount_ton,
                    "message": f"Зачислено {amount_ton:.4f} TON", **balance_payload(uid)})


# ------------------------------ API: лента ---------------------------------- #

async def api_feed(request: web.Request) -> web.Response:
    usd, byn = ton_usd(), ton_byn()
    if sc.SCANNER.tokens_missing():
        return json_ok({"status": "waiting_tokens", "message": sc.STATUS_NO_TOKENS,
                        "items": [], "total": 0, "fee": MARKET_FEE,
                        "rates": RATES["data"], "usd_rate": usd, "byn_rate": byn})

    p = request.query
    markets = [m for m in p.get("markets", "portals,tonnel,mrkt").split(",") if m]
    min_profit = _num(p.get("min_profit"), 0) or 0
    min_mono = _num(p.get("min_mono"), 0) or 0
    search = (p.get("search") or "").strip().lower()
    price_from, price_to = _num(p.get("price_from")), _num(p.get("price_to"))
    beauty = p.get("beauty") in ("1", "true", "on")
    uid = user_id_of(request)

    with _db_lock, db() as c:
        bought = {r["lot_id"] for r in c.execute("SELECT lot_id FROM inventory WHERE user_id=?", (uid,))}
        queued = {r["lot_id"] for r in c.execute("SELECT lot_id FROM orders WHERE user_id=?", (uid,))}

    items = []
    for lot in sc.SCANNER.lots:
        if lot["market"] not in markets or lot["floor_discount"] < min_profit or lot["monochrome_pct"] < min_mono:
            continue
        if price_from is not None and lot["price"] < price_from:
            continue
        if price_to is not None and lot["price"] > price_to:
            continue
        if beauty and not lot["beautiful"]:
            continue
        if search:
            hay = " ".join([lot["collection"], lot["model"], lot["backdrop"], lot["symbol"], lot["number"]]).lower()
            if search not in hay:
                continue
        if lot["id"] in bought or lot["id"] in queued:
            continue
        items.append({**lot, "profit_usd": round(lot["profit_ton"] * usd, 2),
                      "profit_byn": round(lot["profit_ton"] * byn, 2)})

    return json_ok({"status": "ok", "items": items, "total": len(items),
                    "fee": MARKET_FEE, "rates": RATES["data"],
                    "usd_rate": usd, "byn_rate": byn,
                    "balance_ton": get_balance(uid)})


# ------------------------------ API: покупка на сайте ----------------------- #

async def api_buy(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lot_id = str(body.get("lot_id") or "")
    lot = next((l for l in sc.SCANNER.lots if l["id"] == lot_id), None)
    if not lot:
        return json_ok({"ok": False, "error": "Лот уже неактуален в ленте"}, 404)

    bal = get_balance(uid)
    price = round(float(lot["price"]), 6)
    if bal < price:
        return json_ok({"ok": False, "error": f"Недостаточно средств: {price:.2f} TON требуется, на балансе {bal:.2f} TON",
                        **balance_payload(uid)}, 402)

    with _db_lock, db() as c:
        dup = c.execute("SELECT id FROM inventory WHERE user_id=? AND lot_id=?", (uid, lot_id)).fetchone()
        if dup:
            return json_ok({"ok": False, "error": "Подарок уже куплен", **balance_payload(uid)}, 409)
        c.execute("""INSERT INTO inventory(user_id, lot_id, collection, number, model, backdrop, symbol,
                     market, market_label, url, image, buy_price, rec_price, profit_ton, status, created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'owned', ?)""",
                  (uid, lot_id, lot["collection"], lot["number"], lot["model"], lot["backdrop"], lot["symbol"],
                   lot["market"], lot["market_label"], lot["url"], lot["image"], price,
                   lot["rec_price"], lot["profit_ton"], int(time.time())))
        inv_id = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        c.execute("""INSERT INTO orders(user_id, lot_id, url, max_price, target_price, status, created_at)
                     VALUES(?,?,?,?,?, 'queued', ?)""",
                  (uid, lot_id, lot["url"], price, lot["rec_price"], int(time.time())))
    new_bal = add_balance(uid, -price)
    sc.SCANNER.lots = [l for l in sc.SCANNER.lots if l["id"] != lot_id]
    return json_ok({"ok": True, "inventory_id": inv_id, "bought": lot["collection"],
                    "number": lot["number"], "price": price,
                    **balance_payload(uid), "balance_ton": new_bal})


# ------------------------------ API: инвентарь ------------------------------ #

async def api_inventory(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM inventory WHERE user_id=? ORDER BY created_at DESC", (uid,))]
    return json_ok({"ok": True, "items": rows, "count": len(rows),
                    "withdraw_fee": WITHDRAW_FEE_TON, **balance_payload(uid)})


async def api_withdraw(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    inv_id = int(body.get("inventory_id") or 0)
    to = str(body.get("to") or "").strip()
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", to):
        return json_ok({"ok": False, "error": "Укажите получателя в формате @username"}, 400)

    with _db_lock, db() as c:
        item = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (inv_id, uid)).fetchone()
        if not item:
            return json_ok({"ok": False, "error": "Подарок не найден"}, 404)
        if item["status"] != "owned":
            return json_ok({"ok": False, "error": "По этому подарку уже создана заявка"}, 409)

    bal = get_balance(uid)
    if bal < WITHDRAW_FEE_TON:
        return json_ok({"ok": False, "error": f"Нужно {WITHDRAW_FEE_TON} TON на балансе для комиссии (у вас {bal:.2f})",
                        **balance_payload(uid)}, 402)

    status = f"Заявка на вывод для {to} создана"
    with _db_lock, db() as c:
        c.execute("UPDATE inventory SET status=?, withdraw_to=? WHERE id=?", (status, to, inv_id))
        c.execute("""INSERT INTO withdrawals(user_id, inventory_id, to_username, fee, status, created_at)
                     VALUES(?,?,?,?, 'pending', ?)""", (uid, inv_id, to, WITHDRAW_FEE_TON, int(time.time())))
        c.execute("UPDATE inventory SET status=? WHERE id=?", (status, inv_id))
    new_bal = add_balance(uid, -WITHDRAW_FEE_TON)
    return json_ok({"ok": True, "status": status, "fee": WITHDRAW_FEE_TON,
                    **balance_payload(uid), "balance_ton": new_bal})


# ------------------------------ API: корзина / заказы ----------------------- #

async def api_orders(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (uid,))]
    return json_ok({"ok": True, "orders": rows, "count": len(rows)})


async def api_withdrawals(request: web.Request) -> web.Response:
    uid = user_id_of(request)
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM withdrawals WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (uid,))]
    return json_ok({"ok": True, "withdrawals": rows, "count": len(rows)})


# ------------------------------ lifecycle ----------------------------------- #

async def _rates_loop() -> None:
    while True:
        await asyncio.sleep(RATES_TTL)
        try:
            await refresh_rates(force=True)
        except Exception:
            pass


async def on_startup(app: web.Application) -> None:
    init_db()
    await refresh_rates(force=True)
    tasks = [asyncio.create_task(_rates_loop())]
    if not sc.SCANNER.tokens_missing():
        await sc.SCANNER.poll_once()
        tasks.append(asyncio.create_task(sc.SCANNER.run_forever()))
    app["tasks"] = tasks


async def on_cleanup(app: web.Application) -> None:
    for t in app.get("tasks", []):
        t.cancel()


def create_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 512)
    app.router.add_get("/", index)
    app.router.add_get("/api/rates", api_rates)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(create_app(), host="0.0.0.0", port=port)

