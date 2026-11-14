"""
Gift Radar Pro — реальный парсер NFT-подарков Telegram (Portals).

Принципы:
  * Никаких фейковых лотов и случайных чисел. Ошибка сети — пустой список.
  * Токен Portals читается из os.environ (PORTALS_INIT_DATA), есть дефолт.
  * Новые лоты встают строго наверх ленты, исчезнувшие (купленные) удаляются.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

# =========================================================================== #
#  Токен авторизации Portals
# =========================================================================== #

DEFAULT_PORTALS_INIT_DATA = (
    "tma query_id=AAHwk5EPBAAAAPCTkQ_LmrRV&user=%7B%22id%22%3A8851133424%2C%22first_name%22%3A%22Koder_botov%22%2C"
    "%22last_name%22%3A%22I_igr%22%2C%22username%22%3A%22stive_tw_me%22%2C%22language_code%22%3A%22ru%22%2C"
    "%22allows_write_to_pm%22%3Atrue%2C%22photo_url%22%3A%22https%3A%5C%2F%5C%2Ft.me%5C%2Fi%5C%2Fuserpic%5C%2F320%5C%2F"
    "RDd3MY68ATOMBAg2BdqqwN63ZTm5fm5nw5PBAPRCiTwQ41gg01u6tx2bygdPjmGn.svg%22%7D&auth_date=1794655463&"
    "signature=VoV2Ts_-hNtWprxNSj5zVGPmI-mPEBmVmLdTzmLsUCGrAexWCPeQWKfvGvXqbdkjC-fuTtQ73gGxfFeQZ15vDQ&"
    "hash=15741605c51bf0c39026319eaae03e6ca4e98018591137e46818f8c3f308bc58"
)


def portals_init_data() -> str:
    return (os.environ.get("PORTALS_INIT_DATA") or "").strip() or DEFAULT_PORTALS_INIT_DATA


PORTALS_INIT_DATA = portals_init_data()

# =========================================================================== #
#  Константы
# =========================================================================== #

MARKET_FEE = 0.02          # комиссия маркета 2 %
SCAN_INTERVAL = 3.0        # сек
PORTALS_BASE = "https://portal-market.com"
FRAGMENT_BASE = "https://fragment.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

# Эндпоинты Portals опрашиваются по очереди — первый отдавшийJSON с лотами побеждает.
PORTALS_ENDPOINTS: Sequence[str] = (
    f"{PORTALS_BASE}/api/v1/market/items",
    f"{PORTALS_BASE}/api/v1/items",
    f"{PORTALS_BASE}/api/nft/list",
    f"{PORTALS_BASE}/api/v1/gifts",
    f"{PORTALS_BASE}/api/market/items",
)

STATUS_NO_TOKENS = "Ожидание токена Portals — демо-фейки отключены"

# =========================================================================== #
#  Утилиты
# =========================================================================== #

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z]+")


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", str(value))).strip()


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return s or "gift"


def is_beautiful_number(number: str) -> bool:
    s = re.sub(r"\D", "", str(number or ""))
    if not s:
        return False
    if len(s) >= 3 and len(set(s)) == 1:
        return True
    if len(s) >= 3 and s == s[::-1]:
        return True
    for pat in ("123", "321", "777", "888", "999", "000", "420", "1000", "666", "100"):
        if pat in s:
            return True
    return s in {"1", "7", "42", "69"}


_MONO_GROUPS: Dict[str, Sequence[str]] = {
    "dark": ("black", "onyx", "graphite", "charcoal", "dark", "mono", "night", "obsidian"),
    "gold": ("gold", "orange", "amber", "yellow", "bronze", "honey"),
    "blue": ("blue", "cyan", "aqua", "ice", "sky", "navy", "azure"),
    "pink": ("pink", "rose", "magenta", "purple", "violet", "lilac", "orchid"),
    "green": ("green", "mint", "emerald", "teal", "lime", "forest"),
    "red": ("red", "crimson", "ruby", "coral", "scarlet"),
    "light": ("white", "snow", "silver", "pearl", "cream", "frost"),
    "earth": ("brown", "choco", "wood", "sand", "beige", "clay"),
}


def monochrome_pct(model: str, backdrop: str, symbol: str = "") -> int:
    """0–100 %: совпадение тёмных/однотонных оттенков модели и фона."""
    b = (backdrop or "").lower()
    m = f"{model or ''} {symbol or ''}".lower().strip()
    if not b:
        return 0
    score = 0.0
    groups_in_backdrop = [g for g, words in _MONO_GROUPS.items() if any(w in b for w in words)]
    for g in groups_in_backdrop:
        if any(w in m for w in _MONO_GROUPS[g]):
            score += 55
            break
    if set(_WORD_RE.findall(b)) & set(_WORD_RE.findall(m)):
        score += 30
    if not model and not symbol:
        score += 10
    if any(w in b for w in ("black", "onyx", "graphite", "charcoal", "obsidian")):
        dark_model = ("black" in m) or ("onyx" in m) or ("dark" in m)
        score = max(score, 85 if (dark_model or not m) else 65)
    return int(max(0, min(100, round(score))))


def build_lot(*, gift_id: str, collection: str, number: str, model: str, backdrop: str,
              symbol: str, price: float, floor: float, image: str, url: str,
              created_at: float) -> Dict[str, Any]:
    price = round(float(price), 4)
    floor = round(float(floor or price), 4)
    discount = round((1 - price / floor) * 100, 2) if floor > 0 else 0.0
    rec = round(max(floor, price * 1.04), 4)
    profit = round(rec * (1 - MARKET_FEE) - price, 4)
    return {
        "id": str(gift_id),
        "market": "portals",
        "market_label": "Portals",
        "market_url": "https://t.me/portals",
        "collection": collection or "Gift",
        "number": str(number or ""),
        "model": model or "",
        "backdrop": backdrop or "",
        "symbol": symbol or "",
        "price": price,
        "floor": floor,
        "floor_discount": discount,          # % выгоды от флора
        "rec_price": rec,
        "fee": round(rec * MARKET_FEE, 4),
        "profit_ton": profit,                # чистый профит после 2 %
        "profit_pct": round(profit / price * 100, 1) if price else 0.0,
        "monochrome_pct": monochrome_pct(model, backdrop, symbol),
        "beautiful": is_beautiful_number(number),
        "image": image or "",
        "url": url or "https://t.me/portals",
        "found_at": created_at,
        "status": "live",
    }


# =========================================================================== #
#  Парсер Portals
# =========================================================================== #

class PortalsParser:
    """Реальный GET-запрос к API Portals с Authorization: {PORTALS_INIT_DATA}."""

    def __init__(self) -> None:
        self.errors: List[str] = []

    @property
    def token(self) -> str:
        return portals_init_data()

    def enabled(self) -> bool:
        return bool(self.token)

    def headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Authorization": self.token,
            "Origin": PORTALS_BASE,
            "Referer": PORTALS_BASE + "/",
            "User-Agent": UA,
        }

    async def fetch_raw(self, session: aiohttp.ClientSession) -> Any:
        self.errors = []
        if not self.enabled():
            return None
        for url in PORTALS_ENDPOINTS:
            try:
                async with session.get(url, headers=self.headers(),
                                       timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        self.errors.append(f"{url} -> HTTP {resp.status}")
                        continue
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.errors.append(f"{url} -> {type(exc).__name__}")
                continue
            except ValueError:
                self.errors.append(f"{url} -> bad json")
                continue
            if self.extract_items(payload):
                return payload
            self.errors.append(f"{url} -> empty")
        return None

    @staticmethod
    def extract_items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("items", "gifts", "results", "data", "nfts", "list", "offers"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                for sub in ("items", "gifts", "results", "list", "data"):
                    if isinstance(value.get(sub), list):
                        return [x for x in value[sub] if isinstance(x, dict)]
        return []

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            gift = item.get("gift") if isinstance(item.get("gift"), dict) else {}
            nft = item.get("nft") if isinstance(item.get("nft"), dict) else {}
            src: Dict[str, Any] = {**nft, **gift, **item}

            gift_id = str(src.get("id") or src.get("giftId") or src.get("gift_id")
                          or src.get("nftId") or src.get("nft_id") or src.get("tokenId") or "").strip()
            if not gift_id:
                return None

            price = src.get("price") or src.get("priceTon") or src.get("price_ton") \
                or src.get("amount") or src.get("value") or 0
            price = float(price)
            if price <= 0:
                return None

            collection = src.get("collection") if isinstance(src.get("collection"), str) else ""
            collection = (collection or src.get("collectionName") or src.get("collection_name")
                          or src.get("title") or src.get("name") or "").strip()
            collection = clean_text(collection) or "Gift"

            number = str(src.get("number") or src.get("num") or src.get("index")
                         or src.get("giftNumber") or "").strip()

            attrs = src.get("attributes") or src.get("traits") or []
            model = backdrop = symbol = ""
            if isinstance(attrs, (list, tuple)):
                for attr in attrs:
                    if not isinstance(attr, dict):
                        continue
                    key = str(attr.get("type") or attr.get("trait_type") or attr.get("name") or "").lower()
                    val = clean_text(str(attr.get("value") or ""))
                    if "model" in key:
                        model = val
                    elif "backdrop" in key or "background" in key:
                        backdrop = val
                    elif "symbol" in key:
                        symbol = val
            model = model or clean_text(str(src.get("model") or ""))
            backdrop = backdrop or clean_text(str(src.get("backdrop") or src.get("background") or ""))
            symbol = symbol or clean_text(str(src.get("symbol") or ""))

            floor = src.get("floor_price") or src.get("floorPrice") or src.get("floor") \
                or src.get("collectionFloor") or 0
            floor = float(floor)

            slug = slugify(collection)
            image = str(src.get("image") or src.get("preview_url") or src.get("photo") or "")
            if not image.startswith("http") and number:
                image = f"{FRAGMENT_BASE}/{slug}-{number}.large.jpg"

            url = str(src.get("url") or src.get("link") or "")
            if not url.startswith("http"):
                url = f"https://t.me/{slug}-{number}" if number else f"https://t.me/{gift_id}"

            created = src.get("created_at") or src.get("createdAt") or src.get("listed_at")
            found_at = time.time()
            if isinstance(created, (int, float)) and created > 0:
                found_at = float(created) / (1000.0 if created > 10_000_000_000 else 1.0)

            return build_lot(gift_id=gift_id, collection=collection, number=number,
                             model=model, backdrop=backdrop, symbol=symbol,
                             price=price, floor=floor, image=image, url=url,
                             created_at=found_at)
        except (TypeError, ValueError):
            return None

    async def fetch(self, session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
        payload = await self.fetch_raw(session)
        if payload is None:
            return []
        out: List[Dict[str, Any]] = []
        for raw in self.extract_items(payload):
            lot = self.normalize(raw)
            if lot:
                out.append(lot)
        return out


PORTALS = PortalsParser()


# =========================================================================== #
#  Сканер
# =========================================================================== #

class GiftScanner:
    """Держит актуальную ленту: новые сверху, исчезнувшие удаляются."""

    def __init__(self) -> None:
        self.lots: List[Dict[str, Any]] = []
        self.updated_at: float = 0.0
        self.errors: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def tokens_missing(self) -> bool:
        return not PORTALS.enabled()

    async def poll_once(self) -> List[Dict[str, Any]]:
        async with self._lock:
            async with aiohttp.ClientSession() as session:
                try:
                    fresh = await PORTALS.fetch(session)
                    self.errors = {"portals": PORTALS.errors} if PORTALS.errors and not fresh else {}
                except Exception as exc:            # никогда не падаем из-за сети
                    fresh = []
                    self.errors = {"portals": [type(exc).__name__]}
                self.lots = fresh
                self.updated_at = time.time()
                return fresh

    async def run_forever(self, interval: float = SCAN_INTERVAL) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                pass
            await asyncio.sleep(interval)


SCANNER = GiftScanner()
