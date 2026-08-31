"""Фоновый сбор ассетов в очередь.

Индекс Creator Store не отдаёт честную ленту «самое новое»: на любой запрос
приходит не больше 1000 результатов, а сортировка работает внутри этой урезанной
выборки (CreateTime/Descending, например, выдаёт ассеты 2021 года). Поэтому
свежесть добывается перебором: поллер крутит по кругу связки
«категория × сортировка × запрос × страница» и «categoryPath × сортировка»,
каждый раз вскрывая новый срез индекса. Всё невиданное падает в очередь,
а порядок выдачи по createTime наводится уже в БД.
"""

from __future__ import annotations

import asyncio
import logging

from . import constants as C
from .config import Settings
from .db import Database
from .roblox import RobloxError, ToolboxClient

log = logging.getLogger(__name__)

# Сколько разных срезов индекса щупать за один цикл и как глубоко листать.
PROBES_PER_CYCLE = 6
PAGES_PER_PROBE = 3
CURSOR_KEY = "harvest_cursor"

# Сортировка, которая работает для всех категорий без исключений.
FALLBACK_SORT = ("Relevance", "Descending")


def _probe(index: int, categories: list[str], paths: list[str]) -> dict[str, object]:
    """Разворачивает счётчик в конкретный срез индекса.

    Каждый третий шаг уходит в categoryPath — это отдельная ветка индекса,
    вместе с обычным поиском она даёт заметно больше уникальных ассетов.
    Остальные шаги идут по searchCategoryType, причём категория — самая
    быстрая координата: иначе за цикл успевают опроситься одни модели,
    а меши с плагинами не набираются вовсе.
    """
    if paths and index % 3 == 2:
        step = index // 3
        sort_category, sort_direction = C.HARVEST_SORTS[step % len(C.HARVEST_SORTS)]
        path = paths[(step // len(C.HARVEST_SORTS)) % len(paths)]
        return {
            "category_path": path,
            "query": "",
            "sort_category": sort_category,
            "sort_direction": sort_direction,
            "label": f"path={path} {sort_category}/{sort_direction}",
        }

    # Одометр от быстрой координаты к медленной: категория -> сортировка -> запрос.
    step = index - index // 3 if paths else index
    category = categories[step % len(categories)]
    rest = step // len(categories)
    sort_category, sort_direction = C.HARVEST_SORTS[rest % len(C.HARVEST_SORTS)]
    query = C.HARVEST_QUERIES[(rest // len(C.HARVEST_SORTS)) % len(C.HARVEST_QUERIES)]
    return {
        "category": category,
        "query": query,
        "sort_category": sort_category,
        "sort_direction": sort_direction,
        "label": f"{category} q={query!r} {sort_category}/{sort_direction}",
    }


async def run_one_cycle(cfg: Settings, db: Database, client: ToolboxClient, paths: list[str]) -> int:
    """Один проход сбора. Возвращает число новых ассетов в очереди."""
    enabled = (await db.get_setting("categories")).split(",")
    categories = [c for c in enabled if c in C.SEARCH_CATEGORIES] or list(cfg.categories)

    cursor = int(await db.get_setting(CURSOR_KEY, "0") or 0)
    added = 0

    for offset in range(PROBES_PER_CYCLE):
        probe = _probe(cursor + offset, categories, paths)
        page_token: str | None = None
        page_index = 0

        while page_index < PAGES_PER_PROBE:
            try:
                assets, page_token = await client.search(
                    category=probe.get("category"),  # type: ignore[arg-type]
                    category_path=probe.get("category_path"),  # type: ignore[arg-type]
                    query=probe["query"],  # type: ignore[arg-type]
                    sort_category=probe["sort_category"],  # type: ignore[arg-type]
                    sort_direction=probe["sort_direction"],  # type: ignore[arg-type]
                    page_token=page_token,
                )
            except (RobloxError, OSError) as exc:
                log.warning("срез %s сломался: %s", probe["label"], exc)
                break

            # Trending не поддерживается для Plugin, Video и FontFamily — ручка
            # молча отдаёт пустоту. Не тратим шаг ротации впустую.
            if not assets and page_index == 0 and probe["sort_category"] != FALLBACK_SORT[0]:
                probe["sort_category"], probe["sort_direction"] = FALLBACK_SORT
                probe["label"] = f"{probe['label']} → фолбэк {FALLBACK_SORT[0]}"
                page_token = None
                continue

            page_index += 1

            # Категорию из categoryPath поиск не сообщает — проставляем по
            # assetTypeId, иначе не будем знать, чем сохранять в закладки.
            if probe.get("category_path"):
                for asset in assets:
                    asset.category = _category_from_type_id(asset.asset_type_id, asset.category)

            new_count = await db.insert_assets(assets)
            added += new_count
            log.info("срез %s: +%d новых из %d", probe["label"], new_count, len(assets))

            if not page_token:
                break
            await asyncio.sleep(0.3)

    await db.set_setting(CURSOR_KEY, str(cursor + PROBES_PER_CYCLE))

    # Превьюшки поиск не отдаёт — добираем отдельной ручкой заранее,
    # чтобы /next не тормозил.
    missing = await db.missing_thumbnails(limit=200)
    if missing:
        await db.set_thumbnails(await client.thumbnails(missing))

    dropped = await db.trim_queue(cfg.max_queue)
    if dropped:
        log.info("очередь подрезана: выкинуто %d самых старых", dropped)

    return added


# assetTypeId -> searchCategoryType. Нужен, когда ассет найден через categoryPath.
_TYPE_ID_TO_CATEGORY = {
    3: "Audio",
    10: "Model",
    13: "Decal",
    38: "Plugin",
    40: "MeshPart",
    62: "Video",
    73: "FontFamily",
}


def _category_from_type_id(type_id: int, default: str) -> str:
    return _TYPE_ID_TO_CATEGORY.get(type_id, default)


async def poller_loop(cfg: Settings, db: Database, client: ToolboxClient) -> None:
    """Бесконечный цикл сбора. Ошибки логируются и не роняют процесс."""
    paths = await client.category_paths()
    log.info("категорий для обхода: %d", len(paths))

    while True:
        try:
            added = await run_one_cycle(cfg, db, client, paths)
            stats = await db.stats()
            log.info("цикл закончен: +%d, в очереди %d", added, stats["queue"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("цикл сбора упал, продолжаю по расписанию")
        await asyncio.sleep(cfg.poll_interval_min * 60)
