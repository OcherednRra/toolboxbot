"""Телеграм-интерфейс: выдача пачек и кнопка «в закладки»."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
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

# Roblox отдаёт аудиофайл без авторизации только для партнёрских лейблов
# (Distrokid, APM и подобные). Всё, что залили пользователи, отвечает 401 —
# и с Open Cloud API-ключом тоже. Послушать такое можно только на странице.
AUDIO_LOCKED = (
    "\n\n🔇 <i>Послушать здесь не выйдет: Roblox отдаёт файл только для музыки "
    "партнёрских лейблов. Открой страницу ассета кнопкой ниже.</i>"
)

HELP = """<b>Что умею</b>

/next — следующий ассет (сначала самые свежие)
/stats — сколько в очереди, сохранено, пропущено
/cats — какие типы ассетов собирать
/fresh — фильтр по возрасту ассета
/saved — последние сохранённые
/help — это сообщение

Под каждой карточкой:
🔖 — ассет уходит в <b>Saved</b> твоего аккаунта Roblox (виден в
Studio → Toolbox → Saved), карточка остаётся в чате для истории,
и сразу прилетает следующая
⏭ — карточка удаляется из чата, прилетает следующая
🧠 — разбор от Claude, если задан ключ

Аудио приходит файлом — играется прямо в чате."""


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


def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_tech(raw: str, category: str = "") -> str:
    """Строка техсводки. Скрипты выносим вперёд: бесплатные модели со
    скриптами — классический способ занести в игру чужой код."""
    try:
        tech = json.loads(raw or "{}")
    except ValueError:
        return ""
    if not tech:
        return ""

    # У аудио скрипты и полигоны бессмысленны — показываем то, что о нём
    # действительно говорит.
    if category == "Audio":
        audio_parts: list[str] = []
        if tech.get("duration"):
            audio_parts.append(f"⏱ {_fmt_duration(int(tech['duration']))}")
        if tech.get("artist"):
            audio_parts.append(f"🎤 {html.escape(str(tech['artist']))[:60]}")
        kind = " / ".join(
            str(tech[key]) for key in ("audio_type", "genre") if tech.get(key)
        )
        if kind:
            audio_parts.append(f"🎵 {html.escape(kind)[:40]}")
        return " · ".join(audio_parts)

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
    tech = _fmt_tech(row["tech"], row["category"])
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
            InlineKeyboardButton(text="⏭ Дальше", callback_data=f"skip:{asset_id}"),
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


def _audio_filename(name: str) -> str:
    """Имя видно в плеере, поэтому чистим только то, что ломает загрузку."""
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', " ", name).strip() or "audio"
    return f"{safe[:60]}.ogg"


async def _send_audio_card(
    bot: Bot,
    chat_id: int,
    row: aiosqlite.Row,
    keyboard: InlineKeyboardMarkup,
    client: ToolboxClient,
) -> bool:
    """Отправляет аудио файлом. False — не вышло, зовите обычную карточку."""
    data = await client.audio_bytes(row["asset_id"])
    if not data:
        return False

    try:
        tech = json.loads(row["tech"] or "{}")
    except ValueError:
        tech = {}

    try:
        await bot.send_audio(
            chat_id,
            audio=BufferedInputFile(data, filename=_audio_filename(row["name"])),
            caption=_caption(row, CAPTION_LIMIT),
            reply_markup=keyboard,
            duration=int(tech.get("duration") or 0) or None,
            title=row["name"][:64],
            performer=str(tech.get("artist") or row["creator"])[:64],
        )
        return True
    except TelegramRetryAfter:
        raise
    except TelegramBadRequest as exc:
        log.info("аудио %s не отправилось (%s), шлю текстом", row["asset_id"], exc)
        return False


async def _send_card(
    bot: Bot,
    chat_id: int,
    row: aiosqlite.Row,
    with_analysis: bool = True,
    client: ToolboxClient | None = None,
) -> None:
    keyboard = _keyboard(row["asset_id"], with_analysis)
    thumb = row["thumb_url"]

    # Аудио отправляем самим файлом — телеграм играет его прямо в чате.
    if row["category"] == "Audio":
        if client is not None and await _send_audio_card(bot, chat_id, row, keyboard, client):
            return
        # Файл не отдали. Превьюшка у аудио — дежурная иконка, толку от неё
        # никакого, поэтому уходим текстом и честно говорим почему.
        await bot.send_message(
            chat_id,
            _caption(row, MESSAGE_LIMIT - len(AUDIO_LOCKED) - 4) + AUDIO_LOCKED,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

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


async def deliver_next(
    bot: Bot,
    chat_id: int,
    db: Database,
    cfg: Settings,
    client: ToolboxClient,
    analyst: Analyst,
) -> int:
    """Отправляет очередную порцию карточек. Общий путь для /next и кнопки ⏭."""
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
        await bot.send_message(
            chat_id,
            f"Пока пусто — сборщик добирает новое, следующий проход через "
            f"{cfg.poll_interval_min} мин.{hint}",
        )
        return 0

    for row in rows:
        # Техсводку и полное описание надёжно отдаёт только детальная ручка;
        # обновляем строку и перечитываем её, чтобы карточка была полной.
        detail = await client.get_asset(row["asset_id"], row["category"])
        if detail is not None:
            await db.enrich_item(detail)
            row = await db.get_item(row["asset_id"]) or row

        try:
            await _send_card(bot, chat_id, row, analyst.enabled, client)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            await _send_card(bot, chat_id, row, analyst.enabled, client)
        await asyncio.sleep(SEND_DELAY)

    return len(rows)


@router.message(Command("next"))
async def cmd_next(
    message: Message,
    db: Database,
    cfg: Settings,
    bot: Bot,
    client: ToolboxClient,
    analyst: Analyst,
) -> None:
    await deliver_next(bot, message.chat.id, db, cfg, client, analyst)


@router.callback_query(F.data.startswith("save:"))
async def cb_save(
    callback: CallbackQuery,
    db: Database,
    cfg: Settings,
    bot: Bot,
    client: ToolboxClient,
    analyst: Analyst,
) -> None:
    asset_id = int(callback.data.split(":", 1)[1])
    row = await db.get_item(asset_id)
    category = row["category"] if row else "Model"

    try:
        await client.save_asset(asset_id, category)
    except RobloxError as exc:
        # Следующую карточку не шлём: пусть остаётся на этой и попробует ещё раз.
        await callback.answer(f"⚠️ {exc}", show_alert=True)
        return

    await db.set_status(asset_id, STATUS_SAVED)
    await callback.answer("🔖 Сохранено — ищи в Studio → Toolbox → Saved")
    # Карточку оставляем в чате: сохранённое должно остаться в истории.
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_done_keyboard(asset_id, "✅ В закладках", analyst.enabled)
        )
    except TelegramBadRequest:
        pass

    await deliver_next(bot, callback.message.chat.id, db, cfg, client, analyst)


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(
    callback: CallbackQuery,
    db: Database,
    cfg: Settings,
    bot: Bot,
    client: ToolboxClient,
    analyst: Analyst,
) -> None:
    asset_id = int(callback.data.split(":", 1)[1])
    await db.set_status(asset_id, STATUS_SKIPPED)
    await callback.answer("⏭")

    try:
        await callback.message.delete()
    except TelegramBadRequest as exc:
        # Своё сообщение бот может удалить только первые 48 часов. Дальше
        # карточка остаётся в чате — гасим у неё кнопки, чтобы не мозолила.
        log.info("карточку %s удалить не вышло (%s), гашу кнопки", asset_id, exc)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=_done_keyboard(asset_id, "⏭ Пропущено", analyst.enabled)
            )
        except TelegramBadRequest:
            pass

    await deliver_next(bot, callback.message.chat.id, db, cfg, client, analyst)


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
        total = await db.spend_summary()
        await _reply_analysis(
            callback,
            row["analysis"],
            footer=f"♻️ из кеша, повторно платить не пришлось · всего потрачено {_usd(total['usd'])}",
        )
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
        result = await analyst.analyze(row)
    except AnalysisError as exc:
        # Ответ мог прийти и быть оплачен — учитываем даже неудачу.
        await db.add_spend(exc.spend.usd, exc.spend.total_tokens, counted=False)
        await _reply_analysis(callback, f"⚠️ разбор не вышел: {exc}")
        return
    except Exception:
        log.exception("разбор ассета %s упал", asset_id)
        await _reply_analysis(callback, "⚠️ разбор не вышел: внутренняя ошибка")
        return

    spend = result.spend
    await db.set_analysis(asset_id, result.text, spend.usd)
    await db.add_spend(spend.usd, spend.total_tokens)

    total = await db.spend_summary()
    fetched = f" · 🔗 {spend.fetches} переходов по ссылкам" if spend.fetches else ""
    await _reply_analysis(
        callback,
        result.text,
        footer=(
            f"💸 {_usd(spend.usd)} за этот разбор "
            f"({spend.input_tokens}→{spend.output_tokens} токенов){fetched}\n"
            f"Всего на разборы: {_usd(total['usd'])} за {total['calls']} шт."
        ),
    )


def _usd(amount: float) -> str:
    """Разбор стоит центы, поэтому мельчить приходится сильнее обычного."""
    if amount <= 0:
        return "$0"
    if amount < 0.1:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


async def _reply_analysis(callback: CallbackQuery, text: str, footer: str = "") -> None:
    head = "🧠 <b>Разбор</b>"
    tail = f"\n\n<i>{html.escape(footer)}</i>" if footer else ""
    budget = MESSAGE_LIMIT - len(head) - len(tail) - 8
    body = html.escape(text)[:budget]
    await callback.message.reply(f"{head}\n\n{body}{tail}", disable_web_page_preview=True)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, cfg: Settings, analyst: Analyst) -> None:
    stats = await db.stats()
    max_age = int(await db.get_setting("max_age_days", str(cfg.max_age_days)) or 0)
    age_line = f"{max_age} дн." if max_age else "выключен"

    spend_block = ""
    if analyst.enabled or (await db.spend_summary())["calls"]:
        spend = await db.spend_summary()
        average = spend["usd"] / spend["calls"] if spend["calls"] else 0.0
        spend_block = (
            f"\n🧠 разборов: <b>{spend['calls']}</b>\n"
            f"💸 потрачено: <b>{_usd(spend['usd'])}</b>"
            + (f" · в среднем {_usd(average)} за разбор" if spend["calls"] else "")
            + f"\n🔢 токенов: {spend['tokens']:,}".replace(",", " ")
        )

    await message.answer(
        f"📥 в очереди: <b>{stats['queue']}</b>\n"
        f"👀 показано: {stats['shown']}\n"
        f"🔖 сохранено: <b>{stats['saved']}</b>\n"
        f"⏭ пропущено: {stats['skipped']}\n"
        f"— всего в базе: {stats['total']}\n"
        f"{spend_block}\n\n"
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
