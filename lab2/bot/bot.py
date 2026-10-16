"""Telegram-бот «ассистент для задач» — версия 2 (работа с данными).

Что умеет:
- принимает задачи обычным текстом (в личке — любое сообщение, в группе — /task или упоминание);
- сам определяет приоритет («срочно», «!!», «не срочно») и дедлайн («завтра 15:00», «до пятницы»);
- показывает список дел, задачи на сегодня, отмечает выполненные;
- напоминает о дедлайнах и каждое утро присылает дайджест;
- НОВОЕ: зеркалит задачи в Google Таблицу (Google Sheets API);
- НОВОЕ: экспорт в Excel (/export) и импорт задач из .xlsx/.csv файла;
- НОВОЕ: поиск (/find), задачи за неделю (/week), статистика (/stats).

Запуск: python bot.py (токен берётся из файла .env)
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import TaskStorage, from_db, utc_now
from excel_io import ImportFileError, export_xlsx, read_import_file, task_values
from sheets import SheetSync
from task_parser import (
    PRIORITY_EMOJI,
    PRIORITY_TITLE,
    compute_remind_at,
    format_due,
    parse_due,
    parse_priority_arg,
    parse_task,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Библиотека httpx пишет в лог каждый запрос к Telegram — приглушаем её
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "09:00")

HELP_TEXT = """<b>Я помогаю не забывать задачи.</b>

Просто напиши задачу текстом, например:
<code>Срочно отправить отчёт завтра 15:00</code>
<code>Купить подарок до пятницы</code>
<code>Позвонить в банк через 2 часа</code>

<b>Приоритет</b> ставлю сам: «срочно», «важно» или <code>!!</code> → 🔴, «не срочно» или <code>#потом</code> → 🟢, иначе 🟡.
<b>Дедлайн</b> понимаю: сегодня, завтра, послезавтра, дни недели, 20.10, 20 октября, 18:30, через N минут/часов/дней.

<b>Команды</b>
/list — все открытые задачи
/today — что нужно сделать сегодня (и просроченное)
/done 5 — отметить задачу №5 выполненной
/del 5 — удалить задачу №5
/prio 5 высокий — сменить приоритет (высокий / средний / низкий)
/digest on|off — утренний дайджест

<b>Работа с данными</b>
/find слово — поиск по задачам
/week — задачи, созданные за последние 7 дней
/stats — статистика
/export — выгрузить задачи в Excel
📎 Пришли файл .xlsx или .csv с колонками «задача», «приоритет», «дедлайн» — импортирую задачи
/sheet — ссылка на Google Таблицу
/sync — перезаписать Google Таблицу из базы

<b>Прочее</b>
/task текст — добавить задачу (нужно в группах)
/help — эта справка

В группе я реагирую на /task, на упоминание меня и на ответ на моё сообщение."""


# --- Вспомогательные функции -----------------------------------------------

def now_local() -> datetime:
    return datetime.now(TZ)


def storage(context: ContextTypes.DEFAULT_TYPE) -> TaskStorage:
    """База лежит в bot_data, чтобы любой обработчик мог до неё добраться."""
    return context.application.bot_data["storage"]


def format_task(row, now: datetime) -> str:
    """Одна строка списка: 🔴 #12 Текст — до завтра 15:00 ⚠️"""
    line = f"{PRIORITY_EMOJI.get(row['priority'], '🟡')} <b>#{row['id']}</b> {html.escape(row['text'])}"
    due_at = from_db(row["due_at"])
    if due_at:
        due_local = due_at.astimezone(TZ)
        line += f" — <i>⏰ {format_due(due_local, bool(row['has_time']), now)}</i>"
        if due_local < now:
            line += " ⚠️ просрочено"
    return line


def format_task_list(title: str, rows: List, now: datetime) -> str:
    if not rows:
        return f"{title}\n\nЗадач нет 🎉"
    lines = [format_task(row, now) for row in rows]
    return f"{title} ({len(rows)})\n\n" + "\n".join(lines)


def task_keyboard(task_id: int, with_snooze: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под сообщением о задаче."""
    buttons = [InlineKeyboardButton("✅ Готово", callback_data=f"done:{task_id}")]
    if with_snooze:
        buttons.append(InlineKeyboardButton("⏰ +1 час", callback_data=f"snooze:{task_id}"))
    else:
        buttons.append(InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{task_id}"))
    return InlineKeyboardMarkup([buttons])


def parse_task_id(args: List[str]) -> Optional[int]:
    """Номер задачи из аргументов команды: '/done 5' или '/done #5'."""
    if not args:
        return None
    value = args[0].lstrip("#")
    return int(value) if value.isdigit() else None


def chat_title(chat) -> str:
    return chat.title or chat.full_name or str(chat.id)


# --- Синхронизация с Google Таблицей ---------------------------------------
# gspread работает синхронно (блокирует), поэтому запускаем его в отдельном
# потоке через asyncio.to_thread — так бот не «замирает», пока идёт запрос к Google.

def sheet(context: ContextTypes.DEFAULT_TYPE) -> Optional[SheetSync]:
    return context.application.bot_data.get("sheet")


async def sync_task(context: ContextTypes.DEFAULT_TYPE, chat, task_id: int) -> None:
    """Обновляет (или удаляет) строку задачи в Google Таблице. Ошибки не роняют бота."""
    sync = sheet(context)
    if sync is None:
        return
    row = storage(context).get_task(chat.id, task_id)
    try:
        if row is None:
            await asyncio.to_thread(sync.delete, task_id)
        else:
            await asyncio.to_thread(sync.upsert, task_values(row, chat_title(chat), TZ))
    except Exception:
        logger.exception("Google Sheets sync failed for task #%s", task_id)


# --- Логика групп -----------------------------------------------------------

def should_capture(message: Message, bot_username: Optional[str]) -> bool:
    """В личке берём любое сообщение, в группе — только адресованные боту."""
    if message.chat.type == ChatType.PRIVATE:
        return True
    text = (message.text or "").strip()
    if text.startswith("/task"):
        return True
    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.username == bot_username)


def strip_invocation(text: str, bot_username: Optional[str]) -> str:
    """Убирает из текста '/task' и '@имя_бота'."""
    result = re.sub(r"^/(?:task|add)(?:@\w+)?", "", text.strip(), count=1).strip()
    if bot_username:
        result = re.sub(rf"@{re.escape(bot_username)}", "", result, flags=re.IGNORECASE).strip()
    return result


# --- Обработчики команд ----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name if update.effective_user else ""
    await update.message.reply_text(
        f"Привет, {html.escape(name)}! 👋\n\n" + HELP_TEXT, parse_mode=ParseMode.HTML
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт задачу из текста сообщения (обычный текст или /task ...)."""
    message = update.message
    if not message or not message.text:
        return

    bot_username = context.bot.username
    if not should_capture(message, bot_username):
        return

    text = strip_invocation(message.text, bot_username)
    if not text:
        await message.reply_text("Напиши текст задачи, например: /task Отправить отчёт завтра 12:00")
        return
    if len(text) > 500:
        await message.reply_text("Слишком длинная задача — уложись, пожалуйста, в 500 символов.")
        return

    now = now_local()
    parsed = parse_task(text, now)
    user = message.from_user
    task_id = storage(context).add_task(
        chat_id=message.chat_id,
        chat_title=chat_title(message.chat),
        user_id=user.id if user else None,
        author=user.full_name if user else "",
        text=parsed.text,
        priority=parsed.priority,
        assignee=parsed.assignee,
        due_at=parsed.due.at if parsed.due else None,
        has_time=parsed.due.has_time if parsed.due else False,
        remind_at=compute_remind_at(parsed.due, now),
    )
    logger.info("Task #%s added in chat %s", task_id, message.chat_id)

    reply = [
        f"✅ Задача <b>#{task_id}</b> добавлена",
        f"{PRIORITY_EMOJI[parsed.priority]} {html.escape(parsed.text)}",
        f"Приоритет: {PRIORITY_TITLE[parsed.priority]}",
    ]
    if parsed.due:
        reply.append(f"Дедлайн: {format_due(parsed.due.at, parsed.due.has_time, now)}")
        remind_at = compute_remind_at(parsed.due, now)
        if remind_at:
            reply.append(f"Напомню: {format_due(remind_at, True, now)}")
    if parsed.assignee:
        reply.append(f"Исполнитель: {html.escape(parsed.assignee)}")

    await message.reply_text("\n".join(reply), parse_mode=ParseMode.HTML, reply_markup=task_keyboard(task_id))
    await sync_task(context, message.chat, task_id)


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_local()
    rows = storage(context).list_open(update.effective_chat.id)
    await update.message.reply_text(
        format_task_list("📋 <b>Открытые задачи</b>", rows, now), parse_mode=ParseMode.HTML
    )


async def today_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_local()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    rows = storage(context).list_due_before(update.effective_chat.id, end_of_day)
    await update.message.reply_text(
        format_task_list("📅 <b>На сегодня</b>", rows, now), parse_mode=ParseMode.HTML
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_task_id(context.args)
    if task_id is None:
        await update.message.reply_text("Укажи номер задачи: /done 5")
        return
    if storage(context).mark_done(update.effective_chat.id, task_id):
        await update.message.reply_text(f"🎉 Задача #{task_id} выполнена!")
        await sync_task(context, update.effective_chat, task_id)
    else:
        await update.message.reply_text(f"Не нашёл открытую задачу #{task_id}. Посмотри /list")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_task_id(context.args)
    if task_id is None:
        await update.message.reply_text("Укажи номер задачи: /del 5")
        return
    if storage(context).delete_task(update.effective_chat.id, task_id):
        await update.message.reply_text(f"🗑 Задача #{task_id} удалена.")
        await sync_task(context, update.effective_chat, task_id)
    else:
        await update.message.reply_text(f"Задачи #{task_id} нет. Посмотри /list")


async def priority_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task_id = parse_task_id(context.args)
    priority = parse_priority_arg(context.args[1]) if len(context.args) > 1 else None
    if task_id is None or priority is None:
        await update.message.reply_text("Формат: /prio 5 высокий (или средний / низкий)")
        return
    if storage(context).set_priority(update.effective_chat.id, task_id, priority):
        await update.message.reply_text(
            f"{PRIORITY_EMOJI[priority]} У задачи #{task_id} теперь {PRIORITY_TITLE[priority]} приоритет."
        )
        await sync_task(context, update.effective_chat, task_id)
    else:
        await update.message.reply_text(f"Задачи #{task_id} нет. Посмотри /list")


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off", "вкл", "выкл"):
        await update.message.reply_text("Формат: /digest on или /digest off")
        return
    enabled = arg in ("on", "вкл")
    storage(context).set_digest(update.effective_chat.id, enabled)
    state = f"включён, приходит в {DIGEST_TIME}" if enabled else "выключен"
    await update.message.reply_text(f"Утренний дайджест {state}.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатия на inline-кнопки под задачами."""
    query = update.callback_query
    action, _, raw_id = (query.data or "").partition(":")
    if not raw_id.isdigit():
        await query.answer()
        return
    task_id = int(raw_id)
    chat_id = query.message.chat_id
    db = storage(context)

    if action == "done":
        ok = db.mark_done(chat_id, task_id)
        await query.answer("Готово! 🎉" if ok else "Задача уже закрыта")
        if ok:
            await query.edit_message_text(f"✅ <s>Задача #{task_id}</s> выполнена", parse_mode=ParseMode.HTML)
            await sync_task(context, query.message.chat, task_id)
    elif action == "del":
        ok = db.delete_task(chat_id, task_id)
        await query.answer("Удалено" if ok else "Задачи уже нет")
        if ok:
            await query.edit_message_text(f"🗑 Задача #{task_id} удалена")
            await sync_task(context, query.message.chat, task_id)
    elif action == "snooze":
        ok = db.snooze(chat_id, task_id, utc_now() + timedelta(hours=1))
        await query.answer("Напомню через час ⏰" if ok else "Задача уже закрыта")
        if ok:
            await query.edit_message_reply_markup(reply_markup=None)
    else:
        await query.answer()


# --- Работа с данными (лабораторная 2) -------------------------------------

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/find слово — поиск по тексту задачи и исполнителю."""
    query = " ".join(context.args).strip()
    if len(query) < 2:
        await update.message.reply_text("Что ищем? Например: /find отчёт")
        return
    now = now_local()
    rows = storage(context).search(update.effective_chat.id, query)[:30]
    lines = [format_task(row, now) + (" ✅" if row["status"] == "done" else "") for row in rows]
    title = f"🔎 <b>Найдено по «{html.escape(query)}»</b>"
    text = f"{title} ({len(rows)})\n\n" + "\n".join(lines) if rows else f"{title}\n\nНичего не нашёл."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/week — «покажи мои задачи за последнюю неделю»."""
    now = now_local()
    rows = storage(context).created_since(update.effective_chat.id, now - timedelta(days=7))
    lines = [format_task(row, now) + (" ✅" if row["status"] == "done" else "") for row in rows]
    title = "🗓 <b>Задачи за последние 7 дней</b>"
    text = f"{title} ({len(rows)})\n\n" + "\n".join(lines) if rows else f"{title}\n\nЗа неделю задач не было."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — сводка по задачам чата."""
    s = storage(context).stats(update.effective_chat.id, utc_now() - timedelta(days=7))
    if not s["total"]:
        await update.message.reply_text("Пока нет ни одной задачи — статистику считать не из чего 🙂")
        return
    done_share = round(100 * s["done"] / s["total"])
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Всего задач: {s['total']}\n"
        f"В работе: {s['open']} (🔴 {s['high']} · 🟡 {s['medium']} · 🟢 {s['low']})\n"
        f"Выполнено: {s['done']} ({done_share}%)\n"
        f"Просрочено: {s['overdue']}\n\n"
        f"За 7 дней: создано {s['created_week']}, выполнено {s['done_week']}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — присылает Excel-файл со всеми задачами чата."""
    chat = update.effective_chat
    rows = storage(context).all_tasks(chat.id)
    if not rows:
        await update.message.reply_text("Нечего выгружать — задач пока нет.")
        return
    buffer = export_xlsx(rows, chat_title(chat), TZ)
    filename = f"tasks_{now_local():%Y-%m-%d}.xlsx"
    await update.message.reply_document(buffer, filename=filename, caption=f"📤 Задач в файле: {len(rows)}")


async def import_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь прислал .xlsx/.csv — добавляем задачи из файла."""
    message = update.message
    document = message.document
    if message.chat.type != ChatType.PRIVATE and not (message.caption or "").startswith("/import"):
        return  # в группах импортируем только файлы с подписью /import
    if document.file_size and document.file_size > 2 * 1024 * 1024:
        await message.reply_text("Файл больше 2 МБ — слишком большой для импорта.")
        return

    telegram_file = await document.get_file()
    data = bytes(await telegram_file.download_as_bytearray())
    try:
        rows = read_import_file(data, document.file_name or "")
    except ImportFileError as error:
        await message.reply_text(f"⚠️ {error}")
        return

    now = now_local()
    db = storage(context)
    user = message.from_user
    added = []
    for item in rows:
        parsed = parse_task(item.text, now)
        priority = parse_priority_arg(item.priority) or parsed.priority
        due = parse_due(item.due, now) if item.due else parsed.due
        task_id = db.add_task(
            chat_id=message.chat_id,
            chat_title=chat_title(message.chat),
            user_id=user.id if user else None,
            author=user.full_name if user else "",
            text=parsed.text,
            priority=priority,
            assignee=parsed.assignee,
            due_at=due.at if due else None,
            has_time=due.has_time if due else False,
            remind_at=compute_remind_at(due, now),
        )
        added.append(task_id)
    logger.info("Imported %s tasks into chat %s", len(added), message.chat_id)

    await message.reply_text(
        f"📥 Импортировано задач: {len(added)} (№{added[0]}–{added[-1]}). Посмотри /list"
    )
    sync = sheet(context)
    if sync:
        for task_id in added:
            await sync_task(context, message.chat, task_id)


async def sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sync = sheet(context)
    if sync is None:
        await update.message.reply_text("Google Таблица не подключена (не задан GOOGLE_SHEET_ID).")
        return
    await update.message.reply_text(f"📗 Google Таблица с задачами:\n{sync.url}")


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sync — полностью перезаписать Google Таблицу данными из базы."""
    sync = sheet(context)
    if sync is None:
        await update.message.reply_text("Google Таблица не подключена (не задан GOOGLE_SHEET_ID).")
        return
    db = storage(context)
    values = [task_values(row, row["chat_title"], TZ) for row in db.everything()]
    try:
        count = await asyncio.to_thread(sync.replace_all, values)
    except Exception:
        logger.exception("Full Google Sheets sync failed")
        await update.message.reply_text("😔 Не удалось обновить таблицу. Подробности в логе.")
        return
    await update.message.reply_text(f"🔄 Таблица обновлена, строк: {count}\n{sync.url}")


# --- Фоновые задачи (JobQueue) ---------------------------------------------

async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту ищет задачи, о которых пора напомнить."""
    db = storage(context)
    now = now_local()
    for row in db.due_reminders(utc_now()):
        text = "⏰ <b>Напоминание</b>\n\n" + format_task(row, now)
        try:
            await context.bot.send_message(
                row["chat_id"], text, parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(row["id"], with_snooze=True),
            )
        except Forbidden:
            logger.warning("Chat %s blocked the bot, reminder skipped", row["chat_id"])
        except TelegramError:
            logger.exception("Failed to send reminder for task #%s", row["id"])
            continue  # попробуем ещё раз через минуту
        db.mark_reminded(row["id"])


async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утренний дайджест: задачи на сегодня + общее число открытых."""
    db = storage(context)
    now = now_local()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    for chat_id in db.digest_chats():
        today = db.list_due_before(chat_id, end_of_day)
        total = len(db.list_open(chat_id))
        text = format_task_list("☀️ <b>Доброе утро! Дела на сегодня</b>", today, now)
        text += f"\n\nВсего открытых задач: {total}. Полный список — /list"
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            logger.warning("Failed to send digest to chat %s", chat_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любая непойманная ошибка: пишем в лог и вежливо сообщаем пользователю."""
    logger.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("😔 Что-то пошло не так. Попробуй ещё раз.")
        except TelegramError:
            pass


# --- Запуск ----------------------------------------------------------------

def build_application() -> Application:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируй .env.example в .env и вставь токен.")

    application = Application.builder().token(token).build()
    application.bot_data["storage"] = TaskStorage(os.getenv("DB_PATH", "tasks.db"))
    application.bot_data["sheet"] = SheetSync.from_env()  # None, если таблица не настроена

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler(["task", "add"], add_task))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("today", today_tasks))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler(["del", "delete"], delete_command))
    application.add_handler(CommandHandler("prio", priority_command))
    application.add_handler(CommandHandler("digest", digest_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("week", week_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("sheet", sheet_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(MessageHandler(filters.Document.ALL, import_file))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_task))
    application.add_error_handler(error_handler)

    hour, minute = (int(part) for part in DIGEST_TIME.split(":"))
    application.job_queue.run_repeating(check_reminders, interval=60, first=5)
    application.job_queue.run_daily(send_digest, time=time(hour, minute, tzinfo=TZ))
    return application


def main() -> None:
    application = build_application()
    logger.info("Bot started (polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
