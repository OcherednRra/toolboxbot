"""Конфигурация из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .constants import SEARCH_CATEGORIES

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: int
    api_key: str
    data_dir: Path
    poll_interval_min: int
    batch_size: int
    max_queue: int
    max_age_days: int
    categories: tuple[str, ...] = field(default=SEARCH_CATEGORIES)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "toolbox.db"


def load_settings() -> Settings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан — возьми токен у @BotFather")

    raw_cats = os.getenv("CATEGORIES", "").strip()
    if raw_cats:
        cats = tuple(c.strip() for c in raw_cats.split(",") if c.strip() in SEARCH_CATEGORIES)
        if not cats:
            raise SystemExit(f"CATEGORIES содержит только неизвестные значения. Допустимы: {', '.join(SEARCH_CATEGORIES)}")
    else:
        cats = ("Model", "Decal", "Audio", "MeshPart", "Plugin")

    data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=token,
        # 0 = бот ещё не привязан к чату: залогирует id первого написавшего.
        chat_id=_int("TELEGRAM_CHAT_ID", 0),
        api_key=os.getenv("ROBLOX_API_KEY", "").strip(),
        data_dir=data_dir,
        poll_interval_min=max(1, _int("POLL_INTERVAL_MIN", 15)),
        batch_size=max(1, min(_int("BATCH_SIZE", 1), 30)),
        max_queue=max(50, _int("MAX_QUEUE", 2000)),
        # 0 = не фильтровать по возрасту.
        max_age_days=max(0, _int("MAX_AGE_DAYS", 0)),
        categories=cats,
    )
