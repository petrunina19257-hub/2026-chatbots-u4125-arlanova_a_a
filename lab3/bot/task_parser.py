"""Разбор текста задачи: приоритет, исполнитель и дедлайн.

Пользователь пишет задачу свободным текстом, например:
    "Срочно подготовить смету для клиента @alina до пятницы 18:00"
Здесь мы вытаскиваем из текста всё, что понадобится боту:
    - приоритет (высокий / средний / низкий);
    - исполнителя (первый @username);
    - дедлайн (дату и, если указано, время).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

# --- Приоритеты -------------------------------------------------------------

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

# Порядок сортировки: чем меньше число, тем выше задача в списке
PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}
PRIORITY_EMOJI = {PRIORITY_HIGH: "🔴", PRIORITY_MEDIUM: "🟡", PRIORITY_LOW: "🟢"}
PRIORITY_TITLE = {PRIORITY_HIGH: "высокий", PRIORITY_MEDIUM: "средний", PRIORITY_LOW: "низкий"}

# Синонимы, которые пользователь может написать в команде /prio
PRIORITY_ALIASES = {
    "high": PRIORITY_HIGH, "h": PRIORITY_HIGH, "1": PRIORITY_HIGH,
    "высокий": PRIORITY_HIGH, "в": PRIORITY_HIGH, "срочно": PRIORITY_HIGH,
    "medium": PRIORITY_MEDIUM, "m": PRIORITY_MEDIUM, "2": PRIORITY_MEDIUM,
    "средний": PRIORITY_MEDIUM, "с": PRIORITY_MEDIUM,
    "low": PRIORITY_LOW, "l": PRIORITY_LOW, "3": PRIORITY_LOW,
    "низкий": PRIORITY_LOW, "н": PRIORITY_LOW, "потом": PRIORITY_LOW,
}

# Маркеры в тексте задачи. "не срочно" проверяем раньше, чем "срочно".
LOW_RE = re.compile(r"\bне\s+срочно\b|#потом\b|#low\b|\bкогда-нибудь\b", re.IGNORECASE)
HIGH_RE = re.compile(r"\bсрочно\b|\bважно\b|!!|#срочно\b|#high\b", re.IGNORECASE)
# Служебные метки (!!, #срочно и т.п.) вырезаем из итогового текста
TAG_RE = re.compile(r"!!+|#(?:срочно|потом|high|low)\b", re.IGNORECASE)

# --- Исполнитель ------------------------------------------------------------

ASSIGNEE_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{5,32})")

# --- Дата и время -----------------------------------------------------------

DEFAULT_REMIND_HOUR = 10      # во сколько напомнить о задаче без времени
DATE_ONLY_DUE = (23, 59)      # задача без времени считается просроченной после 23:59

TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RELATIVE_RE = re.compile(
    r"\bчерез\s+(\d+\s*)?(минут\w*|мин\b|час\w*|день|дн\w*|недел\w*)",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?(?!\d)")
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WORD_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.IGNORECASE)
# Дни недели в любом падеже: "в пятницу", "до пятницы", "к пятнице"
WEEKDAY_PATTERNS = [
    r"понедельник\w*", r"вторник\w*", r"сред[аыуе]\b", r"четверг\w*",
    r"пятниц\w*", r"суббот\w*", r"воскресень\w*",
]
WEEKDAY_RE = re.compile(r"\b(" + "|".join(WEEKDAY_PATTERNS) + r")", re.IGNORECASE)
WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Чтобы в тексте задачи не дублировался дедлайн, вырезаем найденные фразы
# вместе с предлогом: «до пятницы», «в 18:00», «завтра», «через 2 часа».
PREPOSITION = r"(?:\b(?:до|к|ко|в|во|на|дедлайн|deadline|by)\s*:?\s*)?"
DUE_PHRASE_RES = [
    re.compile(PREPOSITION + pattern.pattern, re.IGNORECASE)
    for pattern in (RELATIVE_RE, NUMERIC_DATE_RE, WORD_DATE_RE, WEEKDAY_RE, TIME_RE)
] + [re.compile(PREPOSITION + r"\b(?:послезавтра|завтра|сегодня)\b", re.IGNORECASE)]


@dataclass(frozen=True)
class Due:
    """Дедлайн задачи. has_time=False, если пользователь указал только дату."""

    at: datetime
    has_time: bool


@dataclass(frozen=True)
class ParsedTask:
    text: str
    priority: str
    assignee: str
    due: Optional[Due]


def detect_priority(text: str) -> str:
    """Определяет приоритет по ключевым словам в тексте."""
    if LOW_RE.search(text):
        return PRIORITY_LOW
    if HIGH_RE.search(text):
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


def _find_time(text: str) -> Optional[tuple]:
    """Ищет время в формате ЧЧ:ММ."""
    match = TIME_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _find_date(text: str, today: date) -> Optional[date]:
    """Ищет дату: сегодня/завтра, день недели, 20.10, 20.10.2026, 20 октября."""
    lower = text.lower()

    # "послезавтра" проверяем первым, потому что внутри него есть "завтра"
    if re.search(r"\bпослезавтра\b", lower):
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", lower):
        return today + timedelta(days=1)
    if re.search(r"\bсегодня\b", lower):
        return today

    match = NUMERIC_DATE_RE.search(lower)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = match.group(3)
        return _make_date(day, month, int(year) if year else None, today)

    match = WORD_DATE_RE.search(lower)
    if match:
        day = int(match.group(1))
        month = MONTHS.index(match.group(2).lower()) + 1
        return _make_date(day, month, None, today)

    match = WEEKDAY_RE.search(lower)
    if match:
        word = match.group(1)
        weekday = next(i for i, p in enumerate(WEEKDAY_PATTERNS) if re.fullmatch(p, word))
        days_ahead = (weekday - today.weekday()) % 7 or 7  # всегда ближайший будущий день
        return today + timedelta(days=days_ahead)

    return None


def _make_date(day: int, month: int, year: Optional[int], today: date) -> Optional[date]:
    """Собирает дату. Если год не указан и дата уже прошла — берём следующий год."""
    if year is not None and year < 100:
        year += 2000
    try:
        result = date(year or today.year, month, day)
    except ValueError:  # например, 31.02
        return None
    if year is None and result < today:
        result = result.replace(year=today.year + 1)
    return result


def parse_due(text: str, now: datetime) -> Optional[Due]:
    """Находит дедлайн в тексте. now — текущее время в часовом поясе пользователя."""
    # 1. Относительное время: "через 2 часа", "через 30 минут", "через неделю"
    match = RELATIVE_RE.search(text)
    if match:
        amount = int(match.group(1)) if match.group(1) else 1
        unit = match.group(2).lower()
        if unit.startswith("мин"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("час"):
            delta = timedelta(hours=amount)
        elif unit.startswith("нед"):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount)
        return Due(at=(now + delta).replace(second=0, microsecond=0), has_time=True)

    # 2. Абсолютная дата и/или время
    found_time = _find_time(text)
    found_date = _find_date(text, now.date())

    if found_date is None and found_time is None:
        return None

    if found_date is None:
        # Указано только время: сегодня, а если оно уже прошло — завтра
        found_date = now.date()
        candidate = now.replace(hour=found_time[0], minute=found_time[1], second=0, microsecond=0)
        if candidate <= now:
            found_date += timedelta(days=1)

    hour, minute = found_time or DATE_ONLY_DUE
    due_at = now.replace(
        year=found_date.year, month=found_date.month, day=found_date.day,
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    return Due(at=due_at, has_time=found_time is not None)


def compute_remind_at(due: Optional[Due], now: datetime) -> Optional[datetime]:
    """Когда напомнить о задаче.

    - Задача со временем: за час до дедлайна (или в момент дедлайна, если до него < 1 часа).
    - Задача только с датой: в 10:00 этого дня.
    - Если момент напоминания уже прошёл — не напоминаем.
    """
    if due is None:
        return None
    if due.has_time:
        before = due.at - timedelta(hours=1)
        if before > now:
            return before
        return due.at if due.at > now else None
    remind = due.at.replace(hour=DEFAULT_REMIND_HOUR, minute=0)
    return remind if remind > now else None


def clean_task_text(text: str, has_due: bool) -> str:
    """Убирает из текста то, что бот уже распознал: приоритет и дедлайн.

    «Срочно отправить отчёт завтра в 15:00» -> «Отправить отчёт»
    """
    result = TAG_RE.sub(" ", text)
    result = LOW_RE.sub(" ", result)
    result = HIGH_RE.sub(" ", result)
    if has_due:
        for pattern in DUE_PHRASE_RES:
            result = pattern.sub(" ", result)
    result = " ".join(result.split())
    result = re.sub(r"\s+([,.;:!?])", r"\1", result).strip(" ,.;:-—")
    if not result:
        return text  # если вырезали всё — оставляем исходный текст
    return result[0].upper() + result[1:]


def parse_task(text: str, now: datetime) -> ParsedTask:
    """Главная функция модуля: превращает сообщение в структуру задачи."""
    clean = " ".join(text.strip().split())
    assignee_match = ASSIGNEE_RE.search(clean)
    priority = detect_priority(clean)
    due = parse_due(clean, now)
    display_text = clean_task_text(clean, due is not None)
    return ParsedTask(
        text=display_text,
        priority=priority,
        assignee=f"@{assignee_match.group(1)}" if assignee_match else "",
        due=due,
    )


def parse_priority_arg(value: str) -> Optional[str]:
    """Превращает аргумент команды (/prio 5 высокий) в код приоритета."""
    return PRIORITY_ALIASES.get(value.strip().lower())


def format_due(due_at: datetime, has_time: bool, now: datetime) -> str:
    """Человекочитаемый дедлайн: 'сегодня 18:00', 'завтра', 'пт 20.10'."""
    days = (due_at.date() - now.date()).days
    if days == 0:
        day_text = "сегодня"
    elif days == 1:
        day_text = "завтра"
    else:
        day_text = f"{WEEKDAY_SHORT[due_at.weekday()]} {due_at:%d.%m}"
        if due_at.year != now.year:
            day_text += f".{due_at:%Y}"
    return f"{day_text} {due_at:%H:%M}" if has_time else day_text
