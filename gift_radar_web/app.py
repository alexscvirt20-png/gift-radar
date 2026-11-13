"""Gift Radar Pro — aiohttp backend (реальные данные, без фейков)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict

import aiohttp
from aiohttp import web

import scanner as sc

BASE_DIR = Path(__file__).parent
CART: list[dict] = []

# ---------- курсы: CoinGecko (TON/USD) + open.er-api (USD/BYN, USD/RUB), кэш 60 с ---------- #

RATES_TTL = 60
FALLBACK_RATES = {"ton_usd": 5.0, "usd_byn": 3.27, "usd_rub": 95.0, "ton_byn": 16.35}
RATES_CACHE: Dict[str, Any] = {"data": dict(FALLBACK_RATES), "updated_at": 0.0, "source": "fallback", "stale": False}
UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}


async def fetch_rates(session: aiohttp.ClientSession) -> Dict[str, Any]:
    data = dict(FALLBACK_RATES)
    src = []
    try:
        async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd",
                               headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            v = (d.get("the-open-network") or {}).get("usd")
            if v:
                data["ton_usd"] = float(v)
                src.append("coingecko")
    except Exception:
        pass
    try:
        async with session.get("https://open.er-api.com/v6/latest/USD",
                               headers=UA, timeout=aiohttp.ClientTimeout(total=8)) as r:
            d = await r.json(content_type=None)
            rates = d.get("rates") or {}
            if rates.get("BYN"):
                data["usd_byn"] = float(rates["BYN"])
            if rates.get("RUB"):
                data["usd_rub"] = float(rates["RUB"])
            if rates.get("BYN") or rates.get("RUB"):
                src.append("er-api")
    except Exception:
        pass
    data["ton_byn"] = round(data["ton_usd"] * data["usd_byn"], 4)
    return {"data": data, "source": "+".join(src) or "fallback"}


async def get_rates(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and RATES_CACHE["updated_at"] and now - RATES_CACHE["updated_at"] < RATES_TTL:
        return RATES_CACHE
    try:
        async with aiohttp.ClientSession() as s:
            RATES_CACHE.update(await fetch_rates(s))
        RATES_CACHE["updated_at"] = now
        RATES_CACHE["stale"] = False
    except Exception:
        RATES_CACHE["stale"] = True
        RATES_CACHE["updated_at"] = RATES_CACHE["updated_at"] or now
    return RATES_CACHE


def usd_rate() -> float:
    return float(RATES_CACHE["data"].get("ton_usd") or FALLBACK_RATES["ton_usd"])


def byn_rate() -> float:
    return float(RATES_CACHE["data"].get("ton_byn") or FALLBACK_RATES["ton_byn"])


def feed_rates() -> Dict[str, Any]:
    return {**RATES_CACHE["data"], "source": RATES_CACHE["source"], "updated_at": int(RATES_CACHE["updated_at"])}


def json_ok(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _num(v, default=None):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ------------------------------- страницы ------------------------------- #

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(BASE_DIR / "templates" / "index.html")


# ------------------------------- курсы ------------------------------- #

async def api_rates(request: web.Request) -> web.Response:
    c = await get_rates(force=request.query.get("refresh") == "1")
    return json_ok({"rates": c["data"], "updated_at": int(c["updated_at"]), "source": c["source"],
                    "stale": c.get("stale", False), "ttl": RATES_TTL})


# ------------------------------- лента ------------------------------- #

async def api_feed(request: web.Request) -> web.Response:
    usd, byn = usd_rate(), byn_rate()
    if sc.SCANNER.tokens_missing():
        return json_ok({"status": "waiting_tokens", "message": sc.STATUS_NO_TOKENS, "items": [], "total": 0,
                        "updated_at": int(sc.SCANNER.updated_at), "rates": feed_rates(),
                        "usd_rate": usd, "byn_rate": byn, "fee": sc.MARKET_FEE})
    p = request.query
    markets = [m for m in p.get("markets", "portals,tonnel,mrkt").split(",") if m]
    min_profit = _num(p.get("min_profit"), 0) or 0
    min_mono = _num(p.get("min_mono"), 0) or 0
    search = (p.get("search") or "").strip().lower()
    price_from, price_to = _num(p.get("price_from")), _num(p.get("price_to"))
    beauty = p.get("beauty") in ("1", "true", "on")

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
        items.append({**lot, "profit_usd": round(lot["profit_ton"] * usd, 2),
                      "profit_byn": round(lot["profit_ton"] * byn, 2)})

    return json_ok({"status": "ok", "items": items, "total": len(items), "errors": sc.SCANNER.errors,
                    "updated_at": int(sc.SCANNER.updated_at), "rates": feed_rates(),
                    "usd_rate": usd, "byn_rate": byn, "fee": sc.MARKET_FEE})


# --------------------- калькулятор по ссылке (t.me / fragment) --------------------- #

OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']*)', re.I)
OG_DESC = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']*)', re.I)
OG_IMAGE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']*)', re.I)
TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
ATTR_TAG = re.compile(r'data-[a-z-]*(?:model|backdrop|symbol|number)="([^"]*)"', re.I)


def clean(s) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


_MONO_GROUPS = {
    "dark": ("black", "onyx", "graphite", "charcoal", "dark", "mono", "night"),
    "gold": ("gold", "orange", "amber", "yellow", "bronze"),
    "blue": ("blue", "cyan", "aqua", "ice", "sky", "navy"),
    "pink": ("pink", "rose", "magenta", "purple", "violet", "lilac"),
    "green": ("green", "mint", "emerald", "teal"),
    "red": ("red", "crimson", "ruby", "coral"),
    "light": ("white", "snow", "silver", "pearl", "cream"),
    "earth": ("brown", "choco", "wood", "sand", "beige", "forest"),
}


def mono_pct(model: str, backdrop: str) -> int:
    b, m = (backdrop or "").lower(), (model or "").lower()
    if not b:
        return 0
    score = 0
    for words in _MONO_GROUPS.values():
        if any(w in b for w in words) and any(w in m for w in words):
            score = max(score, 88)
        elif any(w in b for w in words) and not m:
            score = max(score, 45)
    if b and m and (b in m or m in b):
        score = max(score, 96)
    if any(w in b for w in ("black", "onyx", "graphite", "charcoal")):
        score = max(score, 80)
    return int(max(0, min(100, score)))


def parse_link(url: str) -> tuple[str, str]:
    m = re.search(r"(?:t\.me|telegram\.me|fragment\.com/(?:t\.me/)?)/([^/?#]+)", url, re.I)
    if not m:
        return "", ""
    slug = m.group(1).strip().lower()
    parts = slug.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], parts[1]
    return slug, ""


async def api_calc(request: web.Request) -> web.Response:
    url = (request.query.get("url") or "").strip()
    price = _num(request.query.get("price"), 0) or 0
    if not url:
        return json_ok({"ok": False, "error": "Вставьте ссылку на подарок"}, 400)
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if not re.search(r"(t\.me|telegram\.me|fragment\.com)", url, re.I):
        return json_ok({"ok": False, "error": "Ссылка должна вести на t.me или fragment.com"}, 400)

    slug, num = parse_link(url)
    html, note = "", ""
    target = f"https://t.me/{slug}-{num}" if (slug and num) else url
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(target, headers={**UA, "accept-language": "en-US,en;q=0.9"},
                             timeout=aiohttp.ClientTimeout(total=12), allow_redirects=True) as r:
                html = await r.text(errors="ignore")
                if r.status != 200:
                    note = f"HTTP {r.status}"
    except Exception as e:
        note = f"Сеть недоступна: {type(e).__name__}"

    def grab(rx):
        m = rx.search(html)
        return clean(m.group(1)) if m else ""

    title, desc, og_image = grab(OG_TITLE) or grab(TITLE_TAG), grab(OG_DESC), grab(OG_IMAGE)
    image_full = f"https://fragment.com/{slug}-{num}.large.jpg" if (slug and num) else og_image
    image_medium = f"https://fragment.com/{slug}-{num}.medium.jpg" if (slug and num) else og_image
    lottie = f"https://fragment.com/{slug}-{num}.lottie.json" if (slug and num) else ""

    model = backdrop = symbol = ""
    for part in re.split(r"[|·•,;\n]", f"{title} | {desc}"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        kl, vl = k.strip().lower(), v.strip()
        if "model" in kl:
            model = vl
        elif "backdrop" in kl or "background" in kl:
            backdrop = vl
        elif "symbol" in kl:
            symbol = vl
        elif "number" in kl and not num:
            num = re.sub(r"\D", "", vl) or num
    if not model and not backdrop:
        tags = ATTR_TAG.findall(html)
        if len(tags) >= 3:
            model, backdrop, symbol = tags[0], tags[1], tags[2]

    profit = round(price * (1 - sc.MARKET_FEE), 2) - price
    return json_ok({
        "ok": True, "url": url,
        "name": title or f"{slug or 'Gift'} #{num}" if (slug or num) else (title or "Gift"),
        "collection": slug.title() if slug else "", "number": num,
        "image": image_full or og_image, "image_medium": image_medium, "og_image": og_image,
        "image_2k": image_full, "lottie": lottie,
        "model": model, "backdrop": backdrop, "symbol": symbol,
        "monochrome_pct": mono_pct(model, backdrop),
        "price": price, "fee_rate": sc.MARKET_FEE,
        "profit_ton": profit,
        "profit_usd": round(profit * usd_rate(), 2),
        "profit_byn": round(profit * byn_rate(), 2),
        "usd_rate": usd_rate(), "byn_rate": byn_rate(),
        "note": note,
    })


# ------------------------------- корзина ------------------------------- #

async def api_cart(request: web.Request) -> web.Response:
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        CART.clear()
        CART.extend(body.get("items", []))
        return json_ok({"ok": True, "count": len(CART)})
    total = round(sum(float(i.get("price", 0)) for i in CART), 2)
    profit = round(sum(float(i.get("profit_ton", 0)) for i in CART), 2)
    usd, byn = usd_rate(), byn_rate()
    return json_ok({"items": CART, "count": len(CART), "total_ton": total,
                    "total_usd": round(total * usd, 2), "total_byn": round(total * byn, 2),
                    "profit_ton": profit, "profit_usd": round(profit * usd, 2),
                    "profit_byn": round(profit * byn, 2), "fee": sc.MARKET_FEE, "rates": feed_rates()})


async def api_autobuy(request: web.Request) -> web.Response:
    task = {
        "version": 1,
        "created_at": int(time.time()),
        "fee_rate": sc.MARKET_FEE,
        "items": [{"id": i.get("id"), "url": i.get("url"), "market": i.get("market_label"),
                   "max_price": i.get("price"), "target_price": i.get("rec_price")} for i in CART],
    }
    lines = [f"{i.get('collection')} #{i.get('number')} | {i.get('market_label')} | {i.get('price')} TON | {i.get('url')}"
             for i in CART]
    return json_ok({"ok": True, "list": lines, "count": len(lines),
                    "json_template": json.dumps(task, ensure_ascii=False, indent=2)})


# ------------------------------ lifecycle ------------------------------ #

async def _rates_loop() -> None:
    while True:
        await asyncio.sleep(RATES_TTL)
        try:
            await get_rates(force=True)
        except Exception:
            pass


async def on_startup(app: web.Application) -> None:
    await get_rates(force=True)
    sc.RATES.data = dict(RATES_CACHE["data"])
    tasks = [asyncio.create_task(_rates_loop())]
    if not sc.SCANNER.tokens_missing():
        await sc.SCANNER.poll_once()
        tasks.append(asyncio.create_task(sc.SCANNER.run_forever()))
    app["tasks"] = tasks


async def on_cleanup(app: web.Application) -> None:
    for t in app.get("tasks", []):
        t.cancel()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/rates", api_rates)
    app.router.add_get("/api/feed", api_feed)
    app.router.add_get("/api/calc", api_calc)
    app.router.add_get("/api/cart", api_cart)
    app.router.add_post("/api/cart", api_cart)
    app.router.add_get("/api/autobuy", api_autobuy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(create_app(), host="0.0.0.0", port=port)



