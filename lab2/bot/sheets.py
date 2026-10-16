"""Синхронизация задач с Google Таблицей (Google Sheets API через gspread).

Основное хранилище — SQLite, а таблица — её «зеркало», которое удобно
смотреть и фильтровать в браузере. Каждая задача = одна строка, первая
колонка — номер задачи, по нему строка находится при обновлении.

Синхронизация необязательная: если GOOGLE_SHEET_ID не задан, бот
работает без неё. Если Google недоступен, бот тоже продолжает работать,
а ошибка пишется в лог.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = ["id", "created_at", "chat", "author", "task", "priority", "assignee", "due", "status", "done_at"]


class SheetSync:
    def __init__(self, worksheet: gspread.Worksheet) -> None:
        self.worksheet = worksheet
        self.url = worksheet.spreadsheet.url

    @classmethod
    def from_env(cls) -> Optional["SheetSync"]:
        """Подключается к таблице по настройкам из .env или возвращает None."""
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not sheet_id:
            logger.info("GOOGLE_SHEET_ID is not set, Google Sheets sync disabled")
            return None

        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
        if not Path(credentials_path).exists():
            logger.error("Google credentials file not found: %s, sync disabled", credentials_path)
            return None

        try:
            credentials = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
            spreadsheet = gspread.authorize(credentials).open_by_key(sheet_id)
            name = os.getenv("GOOGLE_SHEET_NAME", "Tasks")
            try:
                worksheet = spreadsheet.worksheet(name)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=len(HEADERS))
            sync = cls(worksheet)
            sync.ensure_headers()
        except Exception:  # сеть, права доступа, неверный ID — бот должен запуститься всё равно
            logger.exception("Could not connect to Google Sheets, sync disabled")
            return None

        logger.info("Google Sheets sync enabled: %s", sync.url)
        return sync

    def ensure_headers(self) -> None:
        if self.worksheet.row_values(1) != HEADERS:
            self.worksheet.update(range_name="A1", values=[HEADERS])
            self.worksheet.freeze(rows=1)

    def _find_row(self, task_id: int) -> Optional[int]:
        """Номер строки в таблице для задачи task_id (или None)."""
        ids = self.worksheet.col_values(1)
        try:
            return ids.index(str(task_id)) + 1
        except ValueError:
            return None

    def upsert(self, values: List[str]) -> None:
        """Добавляет строку задачи или обновляет существующую."""
        row_number = self._find_row(int(values[0]))
        if row_number is None:
            self.worksheet.append_row(values, value_input_option="RAW")
        else:
            self.worksheet.update(range_name=f"A{row_number}", values=[values], raw=True)

    def delete(self, task_id: int) -> None:
        row_number = self._find_row(task_id)
        if row_number is not None:
            self.worksheet.delete_rows(row_number)

    def replace_all(self, rows: Iterable[List[str]]) -> int:
        """Полностью перезаписывает таблицу (команда /sync)."""
        data = [HEADERS] + list(rows)
        self.worksheet.clear()
        self.worksheet.update(range_name="A1", values=data, raw=True)
        self.worksheet.freeze(rows=1)
        return len(data) - 1
