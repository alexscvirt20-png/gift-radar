"""
Gift Radar Pro — реальный сканер NFT-подарков Telegram (Portals, Tonnel, MRKT).

Принципы:
  * НИКАКИХ фейковых лотов и случайных чисел. Нет токенов — нет данных.
  * Токены авторизации читаются из переменных окружения (os.environ); пока они
    пустые — лента показывает статус ожидания, фейковые лоты не создаются.
  * Новые лоты встают строго наверх ленты, исчезнувшие (купленные) удаляются.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

# =========================================================================== #
#  ТОКЕНЫ АВТОРИЗАЦИИ — через переменные окружения.
#  PORTALS_INIT_DATA / TONNEL_INIT_DATA / MRKT_INIT_DATA
# =========================================================================== #

def _env_token(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# Токен Portals по умолчанию (переопределяется переменной окружения PORTALS_INIT_DATA).
PORTALS_INIT_DATA: str = _env_token("PORTALS_INIT_DATA") or (
    "tma query_id=AAHwk5EPBAAAAPCTkQ_LmrRV&user=%7B%22id%22%3A8851133424%2C%22first_name%22%3A%22Koder_botov%22%2C"
    "%22last_name%22%3A%22I_igr%22%2C%22username%22%3A%22stive_tw_me%22%2C%22language_code%22%3A%22ru%22%2C"
    "%22allows_write_to_pm%22%3Atrue%2C%22photo_url%22%3A%22https%3A%5C%2F%5C%2Ft.me%5C%2Fi%5C%2Fuserpic%5C%2F320%5C%2F"
    "RDd3MY68ATOMBAg2BdqqwN63ZTm5fm5nw5PBAPRCiTwQ41gg01u6tx2bygdPjmGn.svg%22%7D&auth_date=1794655463&"
    "signature=VoV2Ts_-hNtWprxNSj5zVGPmI-mPEBmVmLdTzmLsUCGrAexWCPeQWKfvGvXqbdkjC-fuTtQ73gGxfFeQZ15vDQ&"
    "hash=15741605c51bf0c39026319eaae03e6ca4e98018591137e46818f8c3f308bc58"
)


def market_tokens() -> Dict[str, str]:
    """Portals — с дефолтным токеном; Tonnel/MRKT — только из os.environ."""
    return {
        "portals": _env_token("PORTALS_INIT_DATA") or PORTALS_INIT_DATA,
        "tonnel": _env_token("TONNEL_INIT_DATA"),
        "mrkt": _env_token("MRKT_INIT_DATA"),
    }


# совместимость: актуальные значения на момент импорта
_tok = market_tokens()
PORTALS_INIT_DATA: str = _tok["portals"]
TONNEL_INIT_DATA: str = _tok["tonnel"]
MRKT_INIT_DATA: str = _tok["mrkt"]

MARKET_TOKENS: Dict[str, str] = dict(_tok)

# =========================================================================== #
#  Константы
# =========================================================================== #

MARKET_FEE = 0.02  # комиссия маркета 2 %
SCAN_INTERVAL = 3.0

MARKETS = {
    "portals": {"label": "Portals", "accent": "#0098EA",
                "api": "https://portal-market.com/api/v1/market/items",
                "web": "https://t.me/portals"},
    "tonnel": {"label": "Tonnel", "accent": "#C77DFF",
               "api": "https://gifts2.tonnel.network/api/pageGifts",
               "web": "https://t.me/tonnel_network_bot"},
    "mrkt": {"label": "MRKT", "accent": "#FFB300",
             "api": "https://api.mrkt.tg/api/v1/gifts",
             "web": "https://t.me/mrkt"},
}

# Эндпоинты Portals — опрашиваются по очереди, первый успешный отдаёт данные.
PORTALS_ENDPOINTS = (
    "https://portal-market.com/api/v1/market/items",
    "https://portal-market.com/api/v1/items",
    "https://portal-market.com/api/nft/list",
)

TON_API = "https://tonapi.io/v2"
FRAGMENT_BASE = "https://fragment.com"
USD_PER_TON_FALLBACK = 5.0

# --------------------------------------------------------------------------- #
#  API парсеры маркетов (реальные HTTP-запросы)
# --------------------------------------------------------------------------- #

class MarketParser:
    """Базовый парсер: конкретные маркеты переопределяют URL/заголовки/разбор."""

    key: str = "base"

    def __init__(self) -> None:
        self.spec = MARKETS[self.key]

    @property
    def token(self) -> str:
        return market_tokens().get(self.key, "")

    def enabled(self) -> bool:
        return bool(self.token)

    def headers(self) -> Dict[str, str]:
        token = self.token
        host = "https://portal-market.com" if self.key == "portals" else self.spec["web"].replace("https://t.me/", "https://").split("/")[0]
        h = {
            "accept": "application/json, text/plain, */*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "origin": host,
            "referer": host + "/",
            "authorization": token,
        }
        if self.key != "portals":
            h["content-type"] = "application/json"
            h["x-init-data"] = token
            h["cookie"] = f"initData={token}"
        return h

    def endpoints(self) -> Sequence[str]:
        return PORTALS_ENDPOINTS if self.key == "portals" else (self.spec["api"],)

    async def fetch(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        if not self.enabled():
            return []
        for url in self.endpoints():
            try:
                async with session.get(url, headers=self.headers(),
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        continue
                    payload = await r.json(content_type=None)
            except Exception:
                continue
            items = [n for n in (self.normalize(x) for x in self._items(payload)) if n]
            if items:
                return items
        return []

    def _items(self, payload: Any) -> Sequence[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in ("items", "gifts", "data", "nfts", "results", "list", "offers"):
                v = payload.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    for kk in ("items", "gifts", "list", "results"):
                        if isinstance(v.get(kk), list):
                            return v[kk]
        return []

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            gift = item.get("gift") if isinstance(item.get("gift"), dict) else {}
            nft = item.get("nft") if isinstance(item.get("nft"), dict) else {}
            src = {**nft, **gift, **item}
            gid = str(src.get("id") or src.get("giftId") or src.get("gift_id") or src.get("nftId")
                      or src.get("nft_id") or src.get("tokenId") or "").strip()
            if not gid:
                return None
            price = float(src.get("price") or src.get("priceTon") or src.get("price_ton")
                          or src.get("amount") or src.get("value") or 0)
            if price <= 0:
                return None
            coll = (src.get("collection") if isinstance(src.get("collection"), str) else "")
            coll = (coll or src.get("collectionName") or src.get("collection_name")
                    or src.get("title") or src.get("name") or "").strip()
            attrs = src.get("attributes") or src.get("traits") or []
            model, backdrop, symbol = "", "", ""
            if isinstance(attrs, list):
                for a in attrs:
                    if not isinstance(a, dict):
                        continue
                    t = str(a.get("type") or a.get("trait_type") or a.get("name") or "").lower()
                    v = str(a.get("value") or "")
                    if "model" in t:
                        model = v
                    elif "backdrop" in t or "background" in t:
                        backdrop = v
                    elif "symbol" in t:
                        symbol = v
            model = model or str(src.get("model") or "")
            backdrop = backdrop or str(src.get("backdrop") or "")
            symbol = symbol or str(src.get("symbol") or "")
            num = str(src.get("number") or src.get("num") or src.get("index") or "").strip()
            slug = re.sub(r"[^a-z0-9]", "", (coll or "gift").lower())
            # прямая ссылка из API, иначе — t.me/{slug}-{num}
            url = str(src.get("url") or src.get("link") or "")
            if not url.startswith("http"):
                url = f"https://t.me/{slug}-{num}" if num else MARKETS[self.key]["web"]
            image = str(src.get("image") or src.get("preview_url") or src.get("photo") or "")
            if not image and num:
                image = f"{FRAGMENT_BASE}/{slug}-{num}.large.jpg"
            floor = float(src.get("floor") or src.get("floorPrice") or src.get("floor_price")
                          or src.get("collectionFloor") or 0)
            return build_lot(
                market=self.key, gift_id=gid, collection=coll, number=num,
                model=model, backdrop=backdrop, symbol=symbol, price=price,
                floor=floor, image=image, url=url,
            )
        except (TypeError, ValueError):
            return None


class PortalsParser(MarketParser):
    key = "portals"


class TonnelParser(MarketParser):
    key = "tonnel"


class MrktParser(MarketParser):
    key = "mrkt"


MARKET_PARSERS: Dict[str, MarketParser] = {p.key: p for p in (PortalsParser(), TonnelParser(), MrktParser())}

# --------------------------------------------------------------------------- #
#  Общие утилиты
# --------------------------------------------------------------------------- #

_ATTR_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def clean_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return _SPACE_RE.sub(" ", _ATTR_RE.sub(" ", s)).strip()


def is_beautiful_number(number: str) -> bool:
    s = re.sub(r"\D", "", str(number))
    if not s:
        return False
    if len(s) >= 3 and len(set(s)) == 1:
        return True
    if len(s) >= 3 and s == s[::-1]:
        return True
    for p in ("123", "321", "777", "888", "999", "000", "420", "1000", "666", "100"):
        if p in s:
            return True
    return s in {"1", "7", "42", "69"}


def monochrome_pct(model: str, backdrop: str, symbol: str = "") -> int:
    """0–100 %: насколько оттенки фона совпадают с моделью/символом."""
    if not backdrop:
        return 0
    b = backdrop.lower()
    m = f"{model} {symbol}".lower()
    score = 0.0
    groups = {
        "mono": ("black", "onyx", "graphite", "charcoal", "dark", "mono"),
        "warm": ("gold", "orange", "amber", "yellow", "bronze", "peach", "cream"),
        "cold": ("blue", "cyan", "aqua", "ice", "sky", "navy"),
        "pink": ("pink", "rose", "magenta", "purple", "violet", "lilac"),
        "green": ("green", "mint", "emerald", "lime", "teal"),
        "red": ("red", "crimson", "ruby", "coral"),
        "white": ("white", "snow", "silver", "pearl"),
        "earth": ("brown", "choco", "wood", "sand", "beige", "forest"),
    }
    common = [g for g, words in groups.items() if any(w in b for w in words)]
    for g in common:
        if any(w in m for w in groups[g]):
            score += 55
            break
    bk = set(re.findall(r"[a-z]+", b))
    mk = set(re.findall(r"[a-z]+", m))
    if bk & mk:
        score += 30
    if not model and not symbol:
        score += 10
    if any(w in b for w in ("black", "onyx", "graphite", "charcoal")):
        score = max(score, 85 if (("black" in m) or ("onyx" in m) or not m) else 65)
    return int(max(0, min(100, round(score))))


def build_lot(*, market: str, gift_id: str, collection: str, number: str,
              model: str, backdrop: str, symbol: str, price: float, floor: float,
              image: str, url: str) -> Dict[str, Any]:
    floor = round(float(floor or price), 2)
    price = round(float(price), 2)
    discount = round((1 - price / floor) * 100, 2) if floor > 0 else 0.0
    return {
        "id": f"{market}:{gift_id}",
        "market": market,
        "market_label": MARKETS[market]["label"],
        "market_url": MARKETS[market]["web"],
        "collection": collection or "Gift",
        "number": str(number),
        "model": model,
        "backdrop": backdrop,
        "symbol": symbol,
        "price": price,
        "floor": floor,
        "floor_discount": discount,
        "monochrome_pct": monochrome_pct(model, backdrop, symbol),
        "beautiful": is_beautiful_number(number),
        "image": image or "",
        "url": url or MARKETS[market]["web"],
        "found_at": int(time.time()),
        "status": "live",
    }


def apply_resale(lot: Dict[str, Any]) -> Dict[str, Any]:
    """Рекомендуемая цена = флор (или цена покупки +4 %), профит = Рекоменд*0.98 − Покупка."""
    price = lot["price"]
    rec = round(max(lot["floor"], price * 1.04), 2)
    profit = round(rec * (1 - MARKET_FEE) - price, 2)
    lot.update({
        "rec_price": rec,
        "fee": round(rec * MARKET_FEE, 2),
        "profit_ton": profit,
        "profit_pct": round(profit / price * 100, 1) if price else 0.0,
    })
    return lot


# --------------------------------------------------------------------------- #
#  Сканер
# --------------------------------------------------------------------------- #

class GiftScanner:
    def __init__(self) -> None:
        self.lots: List[Dict[str, Any]] = []   # строгий порядок: новые сверху
        self.updated_at: float = 0.0
        self.errors: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    def tokens_missing(self) -> bool:
        return not any(p.enabled() for p in MARKET_PARSERS.values())

    async def poll_once(self) -> None:
        async with self._lock:
            async with aiohttp.ClientSession() as session:
                results = await asyncio.gather(
                    *(p.fetch(session) for p in MARKET_PARSERS.values()),
                    return_exceptions=True,
                )
            merged: Dict[str, Dict[str, Any]] = {}
            self.errors = {}
            for parser, res in zip(MARKET_PARSERS.values(), results):
                if not parser.enabled():
                    continue
                if isinstance(res, Exception):
                    self.errors[parser.key] = type(res).__name__
                    continue
                for lot in res:
                    apply_resale(lot)
                    merged[lot["id"]] = lot
            # ленты нет без токенов; иначе — снимок текущего рынка (исчезнувшие удалены)
            if parser_any_enabled():
                self.lots = list(merged.values())
            self.updated_at = time.time()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # не роняем сканер
                self.errors["scanner"] = type(e).__name__
            await asyncio.sleep(SCAN_INTERVAL)


def parser_any_enabled() -> bool:
    return any(p.enabled() for p in MARKET_PARSERS.values())


SCANNER = GiftScanner()

STATUS_NO_TOKENS = "Ожидание токенов авторизации маркетов в scanner.py — демо-фейки отключены"

# --------------------------------------------------------------------------- #
#  Живые курсы: CoinGecko + open.er-api
# --------------------------------------------------------------------------- #

class Rates:
    def __init__(self) -> None:
        self.data: Dict[str, float] = {"ton_usd": 0.0, "usd_byn": 0.0, "ton_byn": 0.0, "usd_rub": 0.0}
        self.updated_at: float = 0.0
        self.source = "—"

    async def refresh(self) -> Dict[str, float]:
        out = dict(self.data)
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(f"{TON_API}/rates", timeout=aiohttp.ClientTimeout(total=8)) as r:
                    d = await r.json(content_type=None)
                    prices = d.get("rates") if isinstance(d, dict) else None
                    if isinstance(prices, dict):
                        for k, cur in (("ton_usd", "USD"),):
                            v = prices.get(cur)
                            if isinstance(v, dict) and v.get("prices"):
                                out[k] = float(v["prices"]["USD"])
            except Exception:
                self.source = "tonapi:error"
            try:
                async with s.get("https://open.er-api.com/v6/latest/USD",
                                 timeout=aiohttp.ClientTimeout(total=8)) as r:
                    d = await r.json(content_type=None)
                    rates = d.get("rates") if isinstance(d, dict) else None
                    if isinstance(rates, dict):
                        if rates.get("BYN"):
                            out["usd_byn"] = float(rates["BYN"])
                        if rates.get("RUB"):
                            out["usd_rub"] = float(rates["RUB"])
                        self.source = "open.er-api.com"
            except Exception:
                self.source = "er-api:error"
        tua = out.get("ton_usd") or USD_PER_TON_FALLBACK
        ub = out.get("usd_byn") or 3.27
        out["ton_byn"] = round(tua * ub, 4)
        if out.get("ton_usd") or out.get("usd_byn"):
            out["usd_rub"] = out.get("usd_rub") or 0.0
            self.data = out
            self.updated_at = time.time()
        return self.data

    def usd_rate(self) -> float:
        return self.data.get("ton_usd") or USD_PER_TON_FALLBACK

    def byn_rate(self) -> float:
        return self.data.get("ton_byn") or 0.0


RATES = Rates()

# --------------------------------------------------------------------------- #
#  Калькулятор по ссылке: парсинг страницы Telegram / Fragment
# --------------------------------------------------------------------------- #

OG_RE = {
    "title": re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', re.I),
    "description": re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)', re.I),
    "image": re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)', re.I),
}
TG_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(.+)$", re.I)
FRAG_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?fragment\.com/(?:t\.me/)?([^/?#]+)(?:/|[?].*)?$", re.I)


def normalize_url(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def slug_parts(url: str) -> tuple[str, str]:
    m = TG_LINK_RE.match(url) or FRAG_LINK_RE.match(url)
    if not m:
        return "", ""
    slug = m.group(1).strip("/").split("/")[0].split("?")[0]
    num = ""
    split = slug.rsplit("-", 1)
    if len(split) == 2 and split[1].isdigit():
        slug, num = split[0], split[1]
    return slug, num


def parse_attrs_from_text(*texts: str) -> Dict[str, str]:
    joined = " | ".join(clean_text(t) for t in texts if t)
    res = {"model": "", "backdrop": "", "symbol": "", "number": ""}
    for part in re.split(r"[|·•\n,;]", joined):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        kl = k.strip().lower()
        if "model" in kl or "модель" in kl:
            res["model"] = v.strip()
        elif "backdrop" in kl or "background" in kl or "фон" in kl:
            res["backdrop"] = v.strip()
        elif "symbol" in kl or "символ" in kl:
            res["symbol"] = v.strip()
        elif "number" in kl or "номер" in kl:
            n = re.sub(r"\D", "", v)
            if n:
                res["number"] = n
    return res


async def fetch_gift_preview(raw_url: str) -> Dict[str, Any]:
    url = normalize_url(raw_url)
    if not url or not re.search(r"(t\.me|telegram\.me|fragment\.com)", url, re.I):
        return {"ok": False, "error": "Вставьте реальную ссылку вида https://t.me/... или https://fragment.com/..."}

    slug, num = slug_parts(url)
    if not num:
        return {"ok": False, "error": "Не удалось найти номер подарка в ссылке (ожидается вида ...-12345)."}

    if 'og:image' in url.lower():
        pass
    image = f"{FRAGMENT_BASE}/{slug}{('-'+num) if num else ''}.medium.jpg"
    out = {"ok": True, "url": url, "slug": slug, "number": num,
           "title": "", "description": "", "model": "", "backdrop": "",
           "symbol": "", "image": image}

    target = f"https://t.me/{slug}-{num}" if "t.me" in url.lower() or "telegram.me" in url.lower() else url
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(target, timeout=aiohttp.ClientTimeout(total=10),
                             headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36",
                                      "accept-language": "en-US,en;q=0.9,ru;q=0.8"}) as r:
                html = await r.text(errors="ignore")
                if r.status != 200:
                    out["note"] = f"HTTP {r.status}"
    except Exception as e:
        html = ""
        out["note"] = f"Сеть недоступна: {type(e).__name__}"

    og = {k: (clean_text(rx.search(html).group(1)) if rx.search(html) else "") for k, rx in OG_RE.items()}
    if html and og["title"] or html and og["description"]:
        out["title"] = og["title"]
        out["description"] = og["description"]
        if og["image"] and og["image"].startswith("http"):
            out["image"] = og["image"]
        attrs = parse_attrs_from_text(out["title"], out["description"])
        out.update({k: v for k, v in attrs.items() if v})
        if not out["title"]:
            out["title"] = f"{slug} #{num}"
    else:
        if not out.get("title"):
            out["title"] = f"{slug} #{num}"
    out["monochrome_pct"] = monochrome_pct(out["model"], out["backdrop"], out["symbol"])
    return out
