"""Точка входа: поднимает базу, фоновый сбор и long polling телеграма."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .bot import build_dispatcher
from .config import load_settings
from .db import Database
from .poller import poller_loop
from .roblox import ToolboxClient

log = logging.getLogger("robloxbox")

COMMANDS = [
    BotCommand(command="next", description="Следующий ассет"),
    BotCommand(command="stats", description="Очередь и статистика"),
    BotCommand(command="cats", description="Какие типы ассетов собирать"),
    BotCommand(command="fresh", description="Фильтр по возрасту ассета"),
    BotCommand(command="saved", description="Последние сохранённые"),
    BotCommand(command="help", description="Справка"),
]


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # aiogram на INFO слишком болтлив про каждый апдейт, а httpx печатает
    # URL целиком — запрос превьюшек на 50 ассетов занимает пол-экрана.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_settings()
    if not cfg.api_key:
        log.warning("ROBLOX_API_KEY пуст — поиск работать будет, закладки нет")

    db = Database(cfg.db_path)
    await db.connect()

    client = ToolboxClient(cfg.api_key)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher(cfg, db, client)

    harvester = asyncio.create_task(poller_loop(cfg, db, client), name="poller")

    try:
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        log.info("бот @%s поднялся, опрос каждые %d мин.", me.username, cfg.poll_interval_min)
        await dispatcher.start_polling(bot, db=db, client=client, cfg=cfg)
    finally:
        harvester.cancel()
        try:
            await harvester
        except asyncio.CancelledError:
            pass
        await client.aclose()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("остановлено вручную")


if __name__ == "__main__":
    main()
