"""Клиент toolbox-service (Roblox Creator Store)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import constants as C

log = logging.getLogger(__name__)


def parse_ts(raw: str | None) -> datetime | None:
    """Roblox отдаёт доли секунды переменной длины ('...:37.02Z'), которые
    datetime.fromisoformat не всегда принимает. Нормализуем до 6 знаков."""
    if not raw:
        return None
    s = raw.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac.ljust(6, '0')[:6]}"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(slots=True)
class Asset:
    asset_id: int
    name: str
    description: str
    category: str
    asset_type_id: int
    creator_name: str
    creator_id: int
    create_time: datetime | None
    update_time: datetime | None
    price: float
    currency: str
    purchasable: bool
    category_path: str
    up_votes: int
    down_votes: int
    up_vote_percent: int

    @property
    def vote_count(self) -> int:
        return self.up_votes + self.down_votes

    @property
    def store_url(self) -> str:
        return C.STORE_URL.format(asset_id=self.asset_id)

    @property
    def is_free(self) -> bool:
        return self.price <= 0


class RobloxError(RuntimeError):
    """Ошибка API, пригодная для показа пользователю."""


class ToolboxClient:
    """Тонкая обёртка над toolbox-service.

    Поиск работает без авторизации, но всё равно требует X-CSRF-TOKEN.
    Закладки требуют Open Cloud API-ключ (x-api-key); OAuth2 на этих ручках
    отвечает 403, поэтому поддерживаем только ключ.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._csrf: str | None = None
        self._csrf_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
        )
        # Какой targetType принимает ручка saves, выясняем на первом успехе.
        self._save_target_type: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def _headers(self, *, with_csrf: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        if with_csrf and self._csrf:
            headers["X-CSRF-TOKEN"] = self._csrf
        return headers

    async def _refresh_csrf(self) -> None:
        """Токен приезжает в заголовке ответа на «пустой» POST (обычно 403/400)."""
        async with self._csrf_lock:
            resp = await self._client.post(
                C.TOOLBOX_BASE + C.SEARCH_PATH, json={}, headers=self._headers()
            )
            token = resp.headers.get("x-csrf-token")
            if token:
                self._csrf = token
                log.debug("csrf token обновлён")

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Запрос с обновлением CSRF и бэкоффом на 429/5xx."""
        needs_csrf = method in ("POST", "DELETE", "PATCH")
        if needs_csrf and not self._csrf:
            await self._refresh_csrf()

        delay = 1.0
        for attempt in range(5):
            headers = {**self._headers(with_csrf=needs_csrf), **kwargs.pop("headers", {})}
            resp = await self._client.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 403 and resp.headers.get("x-csrf-token"):
                # Токен протух — Roblox кладёт свежий прямо в этот ответ.
                self._csrf = resp.headers["x-csrf-token"]
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 4:
                    break
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                log.warning("%s %s -> %s, жду %.1fs", method, url, resp.status_code, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 60.0)
                continue

            return resp

        return resp

    # ---------------------------------------------------------------- поиск

    async def search(
        self,
        *,
        category: str | None = None,
        category_path: str | None = None,
        query: str = "",
        sort_category: str = "Trending",
        sort_direction: str = "Ascending",
        page_token: str | None = None,
        page_size: int = C.MAX_PAGE_SIZE,
    ) -> tuple[list[Asset], str | None]:
        """Возвращает (ассеты, токен следующей страницы)."""
        body: dict[str, object] = {
            "maxPageSize": min(page_size, C.MAX_PAGE_SIZE),
            "sortCategory": sort_category,
            "sortDirection": sort_direction,
        }
        # API требует ровно одно из двух: searchCategoryType или categoryPath.
        if category_path:
            body["categoryPath"] = category_path
        elif category:
            body["searchCategoryType"] = category
        else:
            raise ValueError("нужен category или category_path")
        if query:
            body["query"] = query
        if page_token:
            body["pageToken"] = page_token

        resp = await self._request("POST", C.TOOLBOX_BASE + C.SEARCH_PATH, json=body)
        if resp.status_code != 200:
            raise RobloxError(f"поиск вернул {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        assets = [
            _parse_asset(entry, fallback_category=category or "Model")
            for entry in data.get("creatorStoreAssets", [])
        ]
        return [a for a in assets if a is not None], data.get("nextPageToken")

    async def category_paths(self) -> list[str]:
        """Полное дерево категорий. Требует API-ключ; без него — фолбэк-список."""
        if not self._api_key:
            return list(C.FALLBACK_CATEGORY_PATHS)
        try:
            resp = await self._request("GET", C.TOOLBOX_BASE + C.CATEGORIES_PATH)
            if resp.status_code != 200:
                log.info("categories вернул %s, беру фолбэк-список", resp.status_code)
                return list(C.FALLBACK_CATEGORY_PATHS)
            paths = sorted(_collect_paths(resp.json()))
            return paths or list(C.FALLBACK_CATEGORY_PATHS)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("categories не ответил (%s), беру фолбэк-список", exc)
            return list(C.FALLBACK_CATEGORY_PATHS)

    # ------------------------------------------------------------- закладки

    async def save_asset(self, asset_id: int, category: str) -> None:
        """Кладёт ассет в Saved аккаунта — владельца API-ключа.

        409 (уже сохранён) считаем успехом: для пользователя результат тот же.
        """
        if not self._api_key:
            raise RobloxError("не задан ROBLOX_API_KEY — закладки недоступны")

        # Ручка не документирует допустимые targetType; пробуем категорию поиска,
        # затем родовое "Asset". Удачный вариант запоминаем.
        candidates = [self._save_target_type] if self._save_target_type else [category, "Asset"]
        last_error = ""
        for target_type in candidates:
            resp = await self._request(
                "POST",
                C.TOOLBOX_BASE + C.SAVES_PATH,
                json={"targetType": target_type, "targetId": asset_id},
            )
            if resp.status_code in (200, 201, 204, 409):
                self._save_target_type = target_type
                return
            last_error = f"{resp.status_code}: {resp.text[:200]}"
            if resp.status_code == 401:
                raise RobloxError("API-ключ отвергнут (401) — проверь скоупы и IP-ограничение")
            if resp.status_code != 400:
                break
        raise RobloxError(f"не удалось сохранить ({last_error})")

    async def unsave_asset(self, asset_id: int, category: str) -> None:
        target_type = self._save_target_type or category
        resp = await self._request(
            "DELETE",
            C.TOOLBOX_BASE + C.SAVES_PATH,
            params={"targetType": target_type, "targetId": asset_id},
        )
        if resp.status_code not in (200, 204, 404):
            raise RobloxError(f"не удалось удалить из закладок ({resp.status_code})")

    async def list_saves(self, limit: int = 20, page: int = 1) -> list[dict]:
        if not self._api_key:
            raise RobloxError("не задан ROBLOX_API_KEY")
        resp = await self._request(
            "GET", C.TOOLBOX_BASE + C.SAVES_PATH, params={"limit": limit, "page": page}
        )
        if resp.status_code != 200:
            raise RobloxError(f"список закладок вернул {resp.status_code}")
        return resp.json().get("saves", [])

    # ------------------------------------------------------------- превьюшки

    async def thumbnails(self, asset_ids: list[int]) -> dict[int, str]:
        """asset_id -> URL картинки. Поиск превью не отдаёт, берём отдельной ручкой."""
        result: dict[int, str] = {}
        for chunk_start in range(0, len(asset_ids), 50):
            chunk = asset_ids[chunk_start : chunk_start + 50]
            try:
                resp = await self._request(
                    "GET",
                    C.THUMBNAILS_URL,
                    params={
                        "assetIds": ",".join(str(i) for i in chunk),
                        "size": "420x420",
                        "format": "Png",
                        "isCircular": "false",
                    },
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json().get("data", []):
                    if item.get("state") == "Completed" and item.get("imageUrl"):
                        result[int(item["targetId"])] = item["imageUrl"]
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("превьюшки не пришли: %s", exc)
        return result


def _parse_asset(entry: dict, fallback_category: str) -> Asset | None:
    asset = entry.get("asset") or {}
    asset_id = asset.get("id")
    if not asset_id:
        return None

    creator = entry.get("creator") or {}
    product = entry.get("creatorStoreProduct") or {}
    price_obj = (product.get("purchasePrice") or {}).get("quantity") or {}
    # Цена приходит как significand * 10^exponent (999 * 10^-2 = 9.99).
    significand = price_obj.get("significand", 0) or 0
    exponent = price_obj.get("exponent", 0) or 0
    price = float(significand) * (10.0**exponent)

    voting = entry.get("voting") or {}

    return Asset(
        asset_id=int(asset_id),
        name=asset.get("name") or "без названия",
        description=asset.get("description") or "",
        category=fallback_category,
        asset_type_id=int(asset.get("assetTypeId") or 0),
        creator_name=creator.get("name") or "неизвестен",
        creator_id=int(creator.get("userId") or 0),
        create_time=parse_ts(asset.get("createTime")),
        update_time=parse_ts(asset.get("updateTime")),
        price=price,
        currency=((product.get("purchasePrice") or {}).get("currencyCode")) or "USD",
        purchasable=bool(product.get("purchasable", True)),
        category_path=asset.get("categoryPath") or "",
        up_votes=int(voting.get("upVotes") or 0),
        down_votes=int(voting.get("downVotes") or 0),
        # Без единого голоса Roblox всё равно присылает 100 — считать это
        # рейтингом нельзя, поэтому обнуляем и решаем по vote_count.
        up_vote_percent=int(voting.get("upVotePercent") or 0)
        if (voting.get("upVotes") or voting.get("downVotes"))
        else 0,
    )


def _collect_paths(node: object, acc: set[str] | None = None) -> set[str]:
    """Рекурсивно вытаскивает все значения categoryPath из дерева категорий."""
    if acc is None:
        acc = set()
    if isinstance(node, dict):
        for key in ("path", "categoryPath"):
            value = node.get(key)
            if isinstance(value, str) and value:
                acc.add(value)
        for value in node.values():
            _collect_paths(value, acc)
    elif isinstance(node, list):
        for value in node:
            _collect_paths(value, acc)
    return acc
