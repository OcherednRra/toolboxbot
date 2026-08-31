#!/usr/bin/env python3
"""Диагностика toolbox-service. Только stdlib — запускается любым python3.

    python3 scripts/probe.py              # проверить поиск и enum'ы
    python3 scripts/probe.py --key KEY    # плюс проверить закладки
    python3 scripts/probe.py --key KEY --save-test 8309341353

Зачем: Roblox не документирует значения searchCategoryType/sortCategory, а
ручка закладок — единственное место, где нужен API-ключ. Скрипт отвечает на
два вопроса: «какие значения принимает поиск» и «работает ли мой ключ».
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://apis.roblox.com"
SEARCH = BASE + "/toolbox-service/v2/assets:search"
SAVES = BASE + "/toolbox-service/v1/saves"

CATEGORY_CANDIDATES = [
    "Model", "Models", "Audio", "Decal", "Decals", "Image", "Mesh", "MeshPart",
    "Plugin", "Video", "Font", "FontFamily", "Animation", "Package", "All",
]
SORT_CANDIDATES = [
    "Relevance", "UpdatedTime", "CreateTime", "Trending", "MostRecent",
    "Recent", "Rating", "Popularity",
]

_csrf: str | None = None


def request(method: str, url: str, body: dict | None = None, api_key: str = ""):
    """-> (status, headers, parsed_json_or_text). CSRF подхватывается сам."""
    global _csrf
    for _ in range(2):
        headers = {"Content-Type": "application/json", "User-Agent": "toolbox-probe/1.0"}
        if api_key:
            headers["x-api-key"] = api_key
        if _csrf:
            headers["X-CSRF-TOKEN"] = _csrf
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode(errors="replace")
                return resp.status, resp.headers, _maybe_json(raw)
        except urllib.error.HTTPError as exc:
            token = exc.headers.get("x-csrf-token")
            if token and token != _csrf:
                _csrf = token
                continue  # протухший токен — повторяем со свежим
            raw = exc.read().decode(errors="replace")
            return exc.code, exc.headers, _maybe_json(raw)
    return 0, {}, "не удалось выполнить запрос"


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_ts(raw: str):
    s = raw.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + frac.ljust(6, "0")[:6]
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)


def probe_enums(api_key: str) -> None:
    print("\n== searchCategoryType ==")
    valid_cats = []
    for cat in CATEGORY_CANDIDATES:
        status, _, _ = request("POST", SEARCH, {"searchCategoryType": cat, "maxPageSize": 1}, api_key)
        if status == 200:
            valid_cats.append(cat)
    print("  валидные:", ", ".join(valid_cats) or "нет")

    print("\n== sortCategory ==")
    valid_sorts = []
    for sort in SORT_CANDIDATES:
        status, _, _ = request(
            "POST", SEARCH, {"searchCategoryType": "Model", "sortCategory": sort, "maxPageSize": 1}, api_key
        )
        if status == 200:
            valid_sorts.append(sort)
    print("  валидные:", ", ".join(valid_sorts) or "нет")


def probe_freshness(api_key: str) -> None:
    """Показывает, насколько свежий контент отдаёт каждая связка сортировок.

    Ради этой таблицы всё и затевалось: честной ленты новинок у Roblox нет,
    и здесь видно, какой перебор реально приносит свежак.
    """
    print("\n== свежесть выдачи (Model, 100 штук на связку) ==")
    now = datetime.datetime.now(datetime.timezone.utc)
    for sort in ("Trending", "Relevance", "CreateTime", "UpdatedTime"):
        for direction in ("Ascending", "Descending"):
            status, _, data = request(
                "POST",
                SEARCH,
                {
                    "searchCategoryType": "Model",
                    "sortCategory": sort,
                    "sortDirection": direction,
                    "maxPageSize": 100,
                },
                api_key,
            )
            if status != 200 or not isinstance(data, dict):
                print(f"  {sort}/{direction}: HTTP {status}")
                continue
            ages = [
                (now - parse_ts(a["asset"]["createTime"])).days
                for a in data.get("creatorStoreAssets", [])
                if a.get("asset", {}).get("createTime")
            ]
            if not ages:
                print(f"  {sort}/{direction}: пусто")
                continue
            fresh30 = sum(1 for a in ages if a <= 30)
            print(f"  {sort:<12}/{direction:<10} n={len(ages):<4} самый свежий={min(ages)}д  моложе месяца={fresh30}")


def probe_saves(api_key: str, asset_id: int | None) -> None:
    print("\n== закладки ==")
    if not api_key:
        print("  пропущено: нет --key")
        return

    status, _, data = request("GET", SAVES + "?limit=5&page=1", None, api_key)
    if status == 401:
        print("  ❌ 401 — ключ не принят. Проверь скоупы creator-store-save:read/write и IP-ограничение.")
        return
    if status != 200:
        print(f"  ❌ GET /saves -> {status}: {str(data)[:300]}")
        return
    saves = data.get("saves", []) if isinstance(data, dict) else []
    print(f"  ✅ ключ рабочий, сейчас в закладках: {data.get('totalCount', len(saves))}")

    if asset_id is None:
        print("  (передай --save-test <assetId>, чтобы проверить сохранение)")
        return

    for target_type in ("Model", "Asset"):
        status, _, data = request(
            "POST", SAVES, {"targetType": target_type, "targetId": asset_id}, api_key
        )
        if status in (200, 201, 204):
            print(f"  ✅ сохранено с targetType={target_type!r} — проверь Studio → Toolbox → Saved")
            return
        if status == 409:
            print(f"  ✅ уже было сохранено (targetType={target_type!r})")
            return
        print(f"  targetType={target_type!r} -> {status}: {str(data)[:200]}")
    print("  ❌ ни один targetType не подошёл")


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика Roblox toolbox-service")
    parser.add_argument("--key", default="", help="Open Cloud API-ключ")
    parser.add_argument("--save-test", type=int, default=None, help="assetId для тестового сохранения")
    parser.add_argument("--skip-enums", action="store_true", help="не перебирать enum'ы")
    args = parser.parse_args()

    status, _, data = request("POST", SEARCH, {"searchCategoryType": "Model", "maxPageSize": 1}, args.key)
    if status != 200:
        print(f"❌ поиск недоступен: HTTP {status}: {str(data)[:300]}")
        return 1
    print("✅ поиск работает")

    if not args.skip_enums:
        probe_enums(args.key)
    probe_freshness(args.key)
    probe_saves(args.key, args.save_test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
