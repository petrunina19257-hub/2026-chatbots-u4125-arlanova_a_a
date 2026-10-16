"""Хранилище задач в SQLite.

SQLite встроена в Python, отдельный сервер базы данных не нужен:
все данные лежат в одном файле (по умолчанию tasks.db).
Время в базе храним в UTC в виде строки 'YYYY-MM-DD HH:MM:SS',
поэтому такие строки можно сравнивать прямо в SQL-запросах.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import List, Optional

DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,           -- чат, в котором создана задача
    user_id     INTEGER,                    -- кто создал задачу
    author      TEXT,
    text        TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'medium',
    assignee    TEXT,
    due_at      TEXT,                       -- дедлайн (UTC) или NULL
    has_time    INTEGER NOT NULL DEFAULT 0, -- 1, если у дедлайна есть время
    remind_at   TEXT,                       -- когда напомнить (UTC) или NULL
    reminded    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open', -- open / done
    created_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_status ON tasks (chat_id, status);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id     INTEGER PRIMARY KEY,
    digest      INTEGER NOT NULL DEFAULT 1  -- 1 = присылать утренний дайджест
);
"""

# Сортировка списка: сначала высокий приоритет, потом ближайший дедлайн
ORDER_BY = """
ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
         due_at IS NULL, due_at, id
"""


def to_db(dt: Optional[datetime]) -> Optional[str]:
    """datetime с часовым поясом -> строка UTC для базы."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime(DB_TIME_FORMAT)


def from_db(value: Optional[str]) -> Optional[datetime]:
    """Строка UTC из базы -> datetime с часовым поясом UTC."""
    if not value:
        return None
    return datetime.strptime(value, DB_TIME_FORMAT).replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStorage:
    """Все операции с базой собраны в одном классе."""

    def __init__(self, path: str) -> None:
        self.path = path
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row  # строки можно читать как словари: row["text"]
        return conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Выполняет изменяющий запрос и сразу сохраняет изменения."""
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, params)

    def _fetch(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchall()

    # --- Задачи --------------------------------------------------------------

    def add_task(
        self,
        chat_id: int,
        user_id: Optional[int],
        author: str,
        text: str,
        priority: str,
        assignee: str,
        due_at: Optional[datetime],
        has_time: bool,
        remind_at: Optional[datetime],
    ) -> int:
        cursor = self._execute(
            """INSERT INTO tasks (chat_id, user_id, author, text, priority, assignee,
                                  due_at, has_time, remind_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, user_id, author, text, priority, assignee,
             to_db(due_at), int(has_time), to_db(remind_at), to_db(utc_now())),
        )
        return cursor.lastrowid

    def get_task(self, chat_id: int, task_id: int) -> Optional[sqlite3.Row]:
        rows = self._fetch("SELECT * FROM tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id))
        return rows[0] if rows else None

    def list_open(self, chat_id: int) -> List[sqlite3.Row]:
        return self._fetch(
            f"SELECT * FROM tasks WHERE chat_id = ? AND status = 'open' {ORDER_BY}",
            (chat_id,),
        )

    def list_due_before(self, chat_id: int, until: datetime) -> List[sqlite3.Row]:
        """Открытые задачи с дедлайном раньше until (включая просроченные)."""
        return self._fetch(
            f"""SELECT * FROM tasks
                WHERE chat_id = ? AND status = 'open' AND due_at IS NOT NULL AND due_at <= ?
                {ORDER_BY}""",
            (chat_id, to_db(until)),
        )

    def mark_done(self, chat_id: int, task_id: int) -> bool:
        cursor = self._execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ? AND chat_id = ? AND status = 'open'",
            (to_db(utc_now()), task_id, chat_id),
        )
        return cursor.rowcount > 0

    def delete_task(self, chat_id: int, task_id: int) -> bool:
        cursor = self._execute("DELETE FROM tasks WHERE id = ? AND chat_id = ?", (task_id, chat_id))
        return cursor.rowcount > 0

    def set_priority(self, chat_id: int, task_id: int, priority: str) -> bool:
        cursor = self._execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND chat_id = ?",
            (priority, task_id, chat_id),
        )
        return cursor.rowcount > 0

    # --- Напоминания ---------------------------------------------------------

    def due_reminders(self, now: datetime) -> List[sqlite3.Row]:
        """Задачи, о которых пора напомнить."""
        return self._fetch(
            """SELECT * FROM tasks
               WHERE status = 'open' AND reminded = 0
                 AND remind_at IS NOT NULL AND remind_at <= ?""",
            (to_db(now),),
        )

    def mark_reminded(self, task_id: int) -> None:
        self._execute("UPDATE tasks SET reminded = 1 WHERE id = ?", (task_id,))

    def snooze(self, chat_id: int, task_id: int, remind_at: datetime) -> bool:
        """Отложить напоминание (кнопка «+1 час»)."""
        cursor = self._execute(
            "UPDATE tasks SET remind_at = ?, reminded = 0 WHERE id = ? AND chat_id = ? AND status = 'open'",
            (to_db(remind_at), task_id, chat_id),
        )
        return cursor.rowcount > 0

    # --- Настройки дайджеста -------------------------------------------------

    def set_digest(self, chat_id: int, enabled: bool) -> None:
        self._execute(
            """INSERT INTO chat_settings (chat_id, digest) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET digest = excluded.digest""",
            (chat_id, int(enabled)),
        )

    def digest_chats(self) -> List[int]:
        """Чаты, где есть открытые задачи и дайджест не выключен."""
        rows = self._fetch(
            """SELECT DISTINCT t.chat_id FROM tasks t
               LEFT JOIN chat_settings s ON s.chat_id = t.chat_id
               WHERE t.status = 'open' AND COALESCE(s.digest, 1) = 1"""
        )
        return [row["chat_id"] for row in rows]
