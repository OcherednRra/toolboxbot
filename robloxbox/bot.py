"""Телеграм-интерфейс: выдача пачек и кнопка «в закладки»."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import constants as C
from .analysis import AnalysisError, Analyst
from .config import Settings
from .db import STATUS_SAVED, STATUS_SKIPPED, Database
from .roblox import RobloxError, ToolboxClient

log = logging.getLogger(__name__)
router = Router()

# Телеграм режет примерно на 20 сообщениях в минуту в один чат.
SEND_DELAY = 0.4
# Лимиты телеграма: подпись под фото — 1024 символа, обычное сообщение — 4096.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

HELP = """<b>Что умею</b>

/next — следующий ассет (сначала самые свежие)
/stats — сколько в очереди, сохранено, пропущено
/cats — какие типы ассетов собирать
/fresh — фильтр по возрасту ассета
/saved — последние сохранённые
/help — это сообщение

Под каждой карточкой кнопка 🔖 — ассет уходит в <b>Saved</b>
твоего аккаунта Roblox и сразу виден в Studio → Toolbox → Saved."""


def _fmt_price(price: float, currency: str) -> str:
    if price <= 0:
        return "🆓 бесплатно"
    return f"💵 {price:.2f} {currency}"


def _fmt_date(raw: str | None) -> str:
    if not raw:
        return "дата неизвестна"
    try:
        return datetime.fromisoformat(raw).strftime("%d.%m.%Y")
    except ValueError:
        return "дата неизвестна"


def _fmt_votes(up: int, down: int, percent: int) -> str:
    total = up + down
    if not total:
        return "🤷 без оценок"
    # percent приходит из API; если он почему-то пуст, считаем сами.
    pct = percent or round(up * 100 / total)
    face = "🔥" if pct >= 80 else "👍" if pct >= 50 else "👎"
    return f"{face} {pct}% ({up}↑ {down}↓)"


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _fmt_tech(raw: str) -> str:
    """Строка техсводки. Скрипты выносим вперёд: бесплатные модели со
    скриптами — классический способ занести в игру чужой код."""
    try:
        tech = json.loads(raw or "{}")
    except ValueError:
        return ""
    if not tech:
        return ""

    parts: list[str] = []
    scripts = tech.get("script_count", 0)
    if scripts:
        parts.append(f"⚠️ {scripts} {_plural(scripts, 'скрипт', 'скрипта', 'скриптов')}")
    elif tech.get("has_scripts"):
        parts.append("⚠️ есть скрипты")
    else:
        parts.append("✅ без скриптов")

    if tech.get("triangles"):
        parts.append(f"🔺 {_fmt_count(tech['triangles'])} трис")

    inner = [
        f"{_fmt_count(tech[key])} {_plural(tech[key], *forms)}"
        for key, forms in (
            ("meshPart", ("меш", "меша", "мешей")),
            ("audio", ("аудио", "аудио", "аудио")),
            ("decal", ("текстура", "текстуры", "текстур")),
            ("animation", ("анимация", "анимации", "анимаций")),
            ("tool", ("инструмент", "инструмента", "инструментов")),
        )
        if tech.get(key)
    ]
    if inner:
        parts.append("🧩 " + ", ".join(inner))
    return " · ".join(parts)


def _clean_description(raw: str) -> str:
    """Схлопывает пустые строки: в описаниях их бывает по десятку подряд."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _caption(row: aiosqlite.Row, limit: int = CAPTION_LIMIT) -> str:
    name = html.escape(row["name"])[:150]
    creator = html.escape(row["creator"])
    label = C.CATEGORY_LABELS.get(row["category"], row["category"])

    lines = [
        f"<b>{name}</b>",
        f"{label} · 👤 {creator}",
        _fmt_votes(row["up_votes"], row["down_votes"], row["up_vote_percent"]),
        f"{_fmt_price(row['price'], row['currency'])} · 📅 {_fmt_date(row['create_time'])}",
    ]
    tech = _fmt_tech(row["tech"])
    if tech:
        lines.append(tech)
    lines.append(f"<code>{row['asset_id']}</code>")
    head = "\n".join(lines)

    description = _clean_description(row["description"])
    if not description:
        return head

    # Экранируем до обрезки, иначе можно разрубить HTML-сущность пополам.
    escaped = html.escape(description)
    budget = limit - len(head) - 2
    if budget < 40:
        return head
    if len(escaped) > budget:
        escaped = escaped[: budget - 1].rsplit(" ", 1)[0] + "…"
    return f"{head}\n\n{escaped}"


def _open_button(asset_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🔗 Открыть в Creator Store", url=C.STORE_URL.format(asset_id=asset_id)
    )


def _keyboard(asset_id: int, with_analysis: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔖 В закладки", callback_data=f"save:{asset_id}"),
            InlineKeyboardButton(text="⏭ Пропуск", callback_data=f"skip:{asset_id}"),
        ]
    ]
    if with_analysis:
        rows.append(
            [InlineKeyboardButton(text="🧠 Разбор", callback_data=f"ai:{asset_id}")]
        )
    rows.append([_open_button(asset_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _done_keyboard(asset_id: int, text: str, with_analysis: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text, callback_data="noop")]]
    if with_analysis:
        rows.append(
            [InlineKeyboardButton(text="🧠 Разбор", callback_data=f"ai:{asset_id}")]
        )
    rows.append([_open_button(asset_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_card(
    bot: Bot, chat_id: int, row: aiosqlite.Row, with_analysis: bool = True
) -> None:
    keyboard = _keyboard(row["asset_id"], with_analysis)
    thumb = row["thumb_url"]

    if thumb:
        try:
            await bot.send_photo(
                chat_id,
                photo=thumb,
                caption=_caption(row, CAPTION_LIMIT),
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            # Телеграм иногда не может скачать картинку с rbxcdn — не беда,
            # карточка уходит текстом, где и описание влезает целиком.
            log.info("превью %s не отправилось (%s), шлю текстом", row["asset_id"], exc)

    await bot.send_message(
        chat_id,
        _caption(row, MESSAGE_LIMIT),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, client: ToolboxClient) -> None:
    warn = ""
    if not client.has_api_key:
        warn = "\n\n⚠️ <b>ROBLOX_API_KEY не задан</b> — кнопка «в закладки» работать не будет."
    await message.answer(f"Привет. Жми /next.\n\n{HELP}{warn}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("next"))
async def cmd_next(
    message: Message,
    db: Database,
    cfg: Settings,
    bot: Bot,
    client: ToolboxClient,
    analyst: Analyst,
) -> None:
    max_age = int(await db.get_setting("max_age_days", str(cfg.max_age_days)) or 0)
    rows = await db.take_batch(cfg.batch_size, max_age_days=max_age)

    if not rows:
        stats = await db.stats()
        hint = ""
        if max_age > 0 and stats["queue"]:
            hint = (
                f"\n\nВ очереди {stats['queue']} шт., но все старше {max_age} дн. "
                "Ослабь фильтр: /fresh 0"
            )
        await message.answer(
            f"Пока пусто — сборщик добирает новое, следующий проход через "
            f"{cfg.poll_interval_min} мин.{hint}"
        )
        return

    for row in rows:
        # Техсводку и полное описание надёжно отдаёт только детальная ручка;
        # обновляем строку и перечитываем её, чтобы карточка была полной.
        detail = await client.get_asset(row["asset_id"], row["category"])
        if detail is not None:
            await db.enrich_item(detail)
            row = await db.get_item(row["asset_id"]) or row

        try:
            await _send_card(bot, message.chat.id, row, analyst.enabled)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            await _send_card(bot, message.chat.id, row, analyst.enabled)
        await asyncio.sleep(SEND_DELAY)


@router.callback_query(F.data.startswith("save:"))
async def cb_save(
    callback: CallbackQuery, db: Database, client: ToolboxClient, analyst: Analyst
) -> None:
    asset_id = int(callback.data.split(":", 1)[1])
    row = await db.get_item(asset_id)
    category = row["category"] if row else "Model"

    try:
        await client.save_asset(asset_id, category)
    except RobloxError as exc:
        await callback.answer(f"⚠️ {exc}", show_alert=True)
        return

    await db.set_status(asset_id, STATUS_SAVED)
    await callback.answer("🔖 Сохранено — ищи в Studio → Toolbox → Saved")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_done_keyboard(asset_id, "✅ В закладках", analyst.enabled)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery, db: Database, analyst: Analyst) -> None:
    asset_id = int(callback.data.split(":", 1)[1])
    await db.set_status(asset_id, STATUS_SKIPPED)
    await callback.answer("⏭")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_done_keyboard(asset_id, "⏭ Пропущено", analyst.enabled)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ai:"))
async def cb_analyze(
    callback: CallbackQuery, db: Database, client: ToolboxClient, analyst: Analyst
) -> None:
    asset_id = int(callback.data.split(":", 1)[1])
    row = await db.get_item(asset_id)
    if row is None:
        await callback.answer("⚠️ ассет пропал из базы", show_alert=True)
        return

    # Готовый разбор отдаём из базы: повторный клик не должен стоить денег.
    if row["analysis"]:
        await callback.answer()
        await _reply_analysis(callback, row["analysis"], cached=True)
        return

    if not analyst.enabled:
        await callback.answer(
            "⚠️ не задан ANTHROPIC_API_KEY — разбор недоступен", show_alert=True
        )
        return

    await callback.answer("🧠 разбираю, это займёт секунд десять")
    try:
        # Разбор опирается на описание и техсводку, а они полны только после
        # детальной ручки — на всякий случай дотягиваем перед запросом.
        detail = await client.get_asset(asset_id, row["category"])
        if detail is not None:
            await db.enrich_item(detail)
            row = await db.get_item(asset_id) or row
        text = await analyst.analyze(row)
    except AnalysisError as exc:
        await _reply_analysis(callback, f"⚠️ разбор не вышел: {exc}")
        return
    except Exception:
        log.exception("разбор ассета %s упал", asset_id)
        await _reply_analysis(callback, "⚠️ разбор не вышел: внутренняя ошибка")
        return

    await db.set_analysis(asset_id, text)
    await _reply_analysis(callback, text)


async def _reply_analysis(callback: CallbackQuery, text: str, cached: bool = False) -> None:
    head = "🧠 <b>Разбор</b>" + (" <i>(из кеша)</i>" if cached else "")
    body = html.escape(text)[: MESSAGE_LIMIT - len(head) - 20]
    await callback.message.reply(f"{head}\n\n{body}", disable_web_page_preview=True)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, cfg: Settings) -> None:
    stats = await db.stats()
    max_age = int(await db.get_setting("max_age_days", str(cfg.max_age_days)) or 0)
    age_line = f"{max_age} дн." if max_age else "выключен"
    await message.answer(
        f"📥 в очереди: <b>{stats['queue']}</b>\n"
        f"👀 показано: {stats['shown']}\n"
        f"🔖 сохранено: <b>{stats['saved']}</b>\n"
        f"⏭ пропущено: {stats['skipped']}\n"
        f"— всего в базе: {stats['total']}\n\n"
        f"Фильтр по возрасту: {age_line}\n"
        f"За один /next: {cfg.batch_size} · опрос каждые {cfg.poll_interval_min} мин."
    )


def _cats_keyboard(enabled: set[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if cat in enabled else '☑️'} {C.CATEGORY_LABELS[cat]}",
                callback_data=f"cat:{cat}",
            )
        ]
        for cat in C.SEARCH_CATEGORIES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _enabled_categories(db: Database, cfg: Settings) -> set[str]:
    raw = await db.get_setting("categories")
    cats = {c for c in raw.split(",") if c in C.SEARCH_CATEGORIES}
    return cats or set(cfg.categories)


@router.message(Command("cats"))
async def cmd_cats(message: Message, db: Database, cfg: Settings) -> None:
    enabled = await _enabled_categories(db, cfg)
    await message.answer(
        "Что собирать (изменения подхватит следующий проход сборщика):",
        reply_markup=_cats_keyboard(enabled),
    )


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(callback: CallbackQuery, db: Database, cfg: Settings) -> None:
    cat = callback.data.split(":", 1)[1]
    enabled = await _enabled_categories(db, cfg)

    if cat in enabled:
        if len(enabled) == 1:
            await callback.answer("Хотя бы одна категория должна остаться", show_alert=True)
            return
        enabled.discard(cat)
    else:
        enabled.add(cat)

    ordered = [c for c in C.SEARCH_CATEGORIES if c in enabled]
    await db.set_setting("categories", ",".join(ordered))
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=_cats_keyboard(enabled))
    except TelegramBadRequest:
        pass


@router.message(Command("fresh"))
async def cmd_fresh(message: Message, db: Database, cfg: Settings) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        current = int(await db.get_setting("max_age_days", str(cfg.max_age_days)) or 0)
        await message.answer(
            f"Сейчас фильтр по возрасту: <b>{current or 'выключен'}</b>\n\n"
            "<code>/fresh 30</code> — показывать только ассеты моложе 30 дней\n"
            "<code>/fresh 0</code> — выключить фильтр\n\n"
            "Учти: по-настоящему свежего в индексе Roblox мало, "
            "с жёстким фильтром очередь будет пустеть."
        )
        return

    days = max(0, int(parts[1]))
    await db.set_setting("max_age_days", str(days))
    await message.answer(
        f"Готово: показываю только моложе {days} дн." if days else "Фильтр по возрасту выключен."
    )


@router.message(Command("saved"))
async def cmd_saved(message: Message, db: Database) -> None:
    rows = await db.recent_saved(20)
    if not rows:
        await message.answer("Пока ничего не сохранено.")
        return
    lines = [
        f"• <a href=\"{C.STORE_URL.format(asset_id=r['asset_id'])}\">"
        f"{html.escape(r['name'])[:60]}</a> — {html.escape(r['creator'])}"
        for r in rows
    ]
    await message.answer(
        "🔖 <b>Последние сохранённые</b>\n\n" + "\n".join(lines),
        disable_web_page_preview=True,
    )


def build_dispatcher(
    cfg: Settings, db: Database, client: ToolboxClient, analyst: Analyst
) -> Dispatcher:
    dispatcher = Dispatcher()
    # Бот приватный: отвечаем только в разрешённом чате.
    if cfg.chat_id:
        router.message.filter(F.chat.id == cfg.chat_id)
        router.callback_query.filter(F.message.chat.id == cfg.chat_id)
    else:
        log.warning(
            "TELEGRAM_CHAT_ID не задан — бот ответит кому угодно. "
            "Напиши боту, возьми chat id из лога и пропиши в переменные."
        )

        @router.message()
        async def _log_chat_id(message: Message) -> None:
            log.warning("chat id этого чата: %s", message.chat.id)
            await message.answer(
                f"chat id этого чата: <code>{message.chat.id}</code>\n"
                "Пропиши его в TELEGRAM_CHAT_ID и перезапусти."
            )

    dispatcher.include_router(router)
    # Прокидываем зависимости в хендлеры через workflow_data.
    dispatcher["db"] = db
    dispatcher["client"] = client
    dispatcher["cfg"] = cfg
    dispatcher["analyst"] = analyst
    return dispatcher
