"""Хранилище очереди на SQLite."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .roblox import Asset

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    asset_id        INTEGER PRIMARY KEY,
    category        TEXT NOT NULL,
    name            TEXT NOT NULL,
    creator         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    create_time     TEXT,
    update_time     TEXT,
    price           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    category_path   TEXT NOT NULL DEFAULT '',
    up_votes        INTEGER NOT NULL DEFAULT 0,
    down_votes      INTEGER NOT NULL DEFAULT 0,
    up_vote_percent INTEGER NOT NULL DEFAULT 0,
    tech            TEXT NOT NULL DEFAULT '{}',
    analysis        TEXT,
    analysis_cost   REAL NOT NULL DEFAULT 0,
    thumb_url       TEXT,
    discovered_at   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new'
);
-- Выдача идёт "сначала самое свежее", отбор — по статусу.
CREATE INDEX IF NOT EXISTS idx_items_queue ON items(status, create_time DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STATUS_NEW = "new"
STATUS_SHOWN = "shown"
STATUS_SAVED = "saved"
STATUS_SKIPPED = "skipped"


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # WAL: поллер пишет параллельно с тем, как бот читает очередь.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        log.info("база готова: %s", self._path)

    # Колонки, добавленные после первого релиза. CREATE TABLE IF NOT EXISTS их
    # не подтянет — базу на Railway надо доводить руками.
    _ADDED_COLUMNS = (
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("down_votes", "INTEGER NOT NULL DEFAULT 0"),
        ("up_vote_percent", "INTEGER NOT NULL DEFAULT 0"),
        ("tech", "TEXT NOT NULL DEFAULT '{}'"),
        ("analysis", "TEXT"),
        ("analysis_cost", "REAL NOT NULL DEFAULT 0"),
    )

    async def _migrate(self) -> None:
        cursor = await self.conn.execute("PRAGMA table_info(items)")
        existing = {row["name"] for row in await cursor.fetchall()}
        for column, decl in self._ADDED_COLUMNS:
            if column not in existing:
                await self.conn.execute(f"ALTER TABLE items ADD COLUMN {column} {decl}")
                log.info("миграция: добавлена колонка %s", column)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() ещё не вызван")
        return self._conn

    # --------------------------------------------------------------- запись

    async def insert_assets(self, assets: list[Asset]) -> int:
        """Кладёт незнакомые ассеты в очередь. Возвращает число новых.

        INSERT OR IGNORE по первичному ключу — уже показанное или сохранённое
        не всплывает повторно, даже если поиск снова его отдал.
        """
        if not assets:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                a.asset_id,
                a.category,
                a.name[:300],
                a.creator_name[:100],
                a.description[:2000],
                a.create_time.isoformat() if a.create_time else None,
                a.update_time.isoformat() if a.update_time else None,
                a.price,
                a.currency,
                a.category_path,
                a.up_votes,
                a.down_votes,
                a.up_vote_percent,
                json.dumps(a.tech),
                now,
            )
            for a in assets
        ]
        cursor = await self.conn.executemany(
            """INSERT OR IGNORE INTO items
               (asset_id, category, name, creator, description, create_time, update_time,
                price, currency, category_path, up_votes, down_votes, up_vote_percent,
                tech, discovered_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')""",
            rows,
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    async def set_thumbnails(self, thumbs: dict[int, str]) -> None:
        if not thumbs:
            return
        await self.conn.executemany(
            "UPDATE items SET thumb_url = ? WHERE asset_id = ?",
            [(url, asset_id) for asset_id, url in thumbs.items()],
        )
        await self.conn.commit()

    async def enrich_item(self, asset: Asset) -> None:
        """Обновляет карточку данными детальной ручки: техсводка и описание
        там есть всегда, в выдаче поиска — не для каждого ассета."""
        await self.conn.execute(
            """UPDATE items SET description = ?, tech = ?, up_votes = ?,
                   down_votes = ?, up_vote_percent = ?
               WHERE asset_id = ?""",
            (
                asset.description[:2000],
                json.dumps(asset.tech),
                asset.up_votes,
                asset.down_votes,
                asset.up_vote_percent,
                asset.asset_id,
            ),
        )
        await self.conn.commit()

    async def set_analysis(self, asset_id: int, text: str, cost: float) -> None:
        """Разбор кешируется: повторное нажатие кнопки не должно снова
        оплачиваться запросом к модели."""
        await self.conn.execute(
            "UPDATE items SET analysis = ?, analysis_cost = ? WHERE asset_id = ?",
            (text, cost, asset_id),
        )
        await self.conn.commit()

    async def add_spend(self, cost: float, tokens: int, counted: bool = True) -> None:
        """Пожизненные счётчики расходов в settings.

        Отдельно от items.analysis_cost: строку могут вычистить из очереди, а
        потраченные деньги от этого никуда не денутся. `counted=False` — для
        сорвавшихся разборов: токены оплачены, но разбора не случилось.
        """
        if cost <= 0 and tokens <= 0:
            return
        spent = float(await self.get_setting("spend_usd", "0") or 0) + cost
        total_tokens = int(await self.get_setting("spend_tokens", "0") or 0) + tokens
        await self.set_setting("spend_usd", f"{spent:.6f}")
        await self.set_setting("spend_tokens", str(total_tokens))
        if counted:
            calls = int(await self.get_setting("spend_calls", "0") or 0) + 1
            await self.set_setting("spend_calls", str(calls))

    async def spend_summary(self) -> dict[str, float]:
        return {
            "usd": float(await self.get_setting("spend_usd", "0") or 0),
            "tokens": int(await self.get_setting("spend_tokens", "0") or 0),
            "calls": int(await self.get_setting("spend_calls", "0") or 0),
        }

    async def set_status(self, asset_id: int, status: str) -> None:
        await self.conn.execute("UPDATE items SET status = ? WHERE asset_id = ?", (status, asset_id))
        await self.conn.commit()

    async def trim_queue(self, max_queue: int) -> int:
        """Держит очередь в рамках: выкидывает самые старые непоказанные."""
        cursor = await self.conn.execute(
            """DELETE FROM items WHERE asset_id IN (
                   SELECT asset_id FROM items WHERE status = 'new'
                   ORDER BY create_time ASC LIMIT MAX(0, (
                       SELECT COUNT(*) FROM items WHERE status = 'new') - ?)
               )""",
            (max_queue,),
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    # --------------------------------------------------------------- чтение

    async def take_batch(self, limit: int, max_age_days: int = 0) -> list[aiosqlite.Row]:
        """Отдаёт пачку из очереди и помечает её показанной.

        Порядок — самое свежее по createTime вперёд: индекс Roblox не умеет
        отдавать честную ленту новинок, поэтому свежесть наводим уже у себя.
        """
        where = "status = 'new'"
        params: list[object] = []
        if max_age_days > 0:
            where += " AND create_time IS NOT NULL AND create_time >= ?"
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
            params.append(datetime.fromtimestamp(cutoff, timezone.utc).isoformat())
        params.append(limit)

        cursor = await self.conn.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY create_time DESC LIMIT ?", params
        )
        rows = await cursor.fetchall()
        if rows:
            ids = [r["asset_id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            await self.conn.execute(
                f"UPDATE items SET status = 'shown' WHERE asset_id IN ({placeholders})", ids
            )
            await self.conn.commit()
        return list(rows)

    async def get_item(self, asset_id: int) -> aiosqlite.Row | None:
        cursor = await self.conn.execute("SELECT * FROM items WHERE asset_id = ?", (asset_id,))
        return await cursor.fetchone()

    async def missing_thumbnails(self, limit: int = 100) -> list[int]:
        # Аудио пропускаем: у него превью — дежурная иконка, а карточка аудио
        # картинку всё равно не показывает. Заодно меньше упираемся в 429
        # на thumbnails-ручке.
        cursor = await self.conn.execute(
            "SELECT asset_id FROM items "
            "WHERE status = 'new' AND thumb_url IS NULL AND category != 'Audio' LIMIT ?",
            (limit,),
        )
        return [row["asset_id"] for row in await cursor.fetchall()]

    async def stats(self) -> dict[str, int]:
        cursor = await self.conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        counts = {row["status"]: row["n"] for row in await cursor.fetchall()}
        return {
            "queue": counts.get(STATUS_NEW, 0),
            "shown": counts.get(STATUS_SHOWN, 0),
            "saved": counts.get(STATUS_SAVED, 0),
            "skipped": counts.get(STATUS_SKIPPED, 0),
            "total": sum(counts.values()),
        }

    async def recent_saved(self, limit: int = 20) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            "SELECT * FROM items WHERE status = 'saved' ORDER BY discovered_at DESC LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    # ------------------------------------------------------------ настройки

    async def get_setting(self, key: str, default: str = "") -> str:
        cursor = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
