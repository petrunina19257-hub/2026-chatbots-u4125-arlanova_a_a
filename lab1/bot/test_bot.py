"""Автотесты: разбор задач и работа с базой. Запуск: python -m unittest -v"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database import TaskStorage
from task_parser import compute_remind_at, format_due, parse_task

TZ = ZoneInfo("Europe/Moscow")
# Пятница, 16 октября 2026, 12:00
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=TZ)


class ParserTests(unittest.TestCase):
    def test_priority(self):
        self.assertEqual(parse_task("Срочно отправить отчёт", NOW).priority, "high")
        self.assertEqual(parse_task("!! позвонить", NOW).priority, "high")
        self.assertEqual(parse_task("Не срочно почитать статью", NOW).priority, "low")
        self.assertEqual(parse_task("Купить молоко", NOW).priority, "medium")

    def test_text_cleanup(self):
        self.assertEqual(parse_task("!! позвонить #срочно", NOW).text, "Позвонить")
        self.assertEqual(parse_task("Срочно отправить отчёт завтра в 15:00", NOW).text, "Отправить отчёт")
        self.assertEqual(parse_task("Купить подарок до пятницы", NOW).text, "Купить подарок")
        self.assertEqual(parse_task("Позвонить в банк через 2 часа", NOW).text, "Позвонить в банк")
        self.assertEqual(parse_task("Сдать лабу до 20.10, дедлайн жёсткий", NOW).text, "Сдать лабу, дедлайн жёсткий")
        self.assertEqual(parse_task("Завтра", NOW).text, "Завтра")  # пустой текст не оставляем

    def test_tomorrow_with_time(self):
        due = parse_task("Отчёт завтра 15:00", NOW).due
        self.assertEqual(due.at, datetime(2026, 10, 17, 15, 0, tzinfo=TZ))
        self.assertTrue(due.has_time)

    def test_day_after_tomorrow(self):
        due = parse_task("Сдать лабу послезавтра", NOW).due
        self.assertEqual(due.at.date(), datetime(2026, 10, 18).date())
        self.assertFalse(due.has_time)

    def test_weekday(self):
        # сегодня пятница -> «до пятницы» значит следующая пятница
        self.assertEqual(parse_task("до пятницы", NOW).due.at.day, 23)
        self.assertEqual(parse_task("в среду", NOW).due.at.day, 21)
        self.assertEqual(parse_task("к понедельнику", NOW).due.at.day, 19)

    def test_numeric_and_word_dates(self):
        self.assertEqual(parse_task("до 20.10", NOW).due.at.date(), datetime(2026, 10, 20).date())
        self.assertEqual(parse_task("до 01.02", NOW).due.at.year, 2027)  # дата прошла -> след. год
        self.assertEqual(parse_task("5 ноября", NOW).due.at.month, 11)
        self.assertIsNone(parse_task("31.02", NOW).due)

    def test_relative(self):
        self.assertEqual(parse_task("через 2 часа", NOW).due.at, NOW + timedelta(hours=2))
        self.assertEqual(parse_task("через 30 минут", NOW).due.at, NOW + timedelta(minutes=30))
        self.assertEqual(parse_task("через неделю", NOW).due.at, NOW + timedelta(weeks=1))

    def test_time_only(self):
        self.assertEqual(parse_task("в 18:30", NOW).due.at, NOW.replace(hour=18, minute=30))
        self.assertEqual(parse_task("в 09:00", NOW).due.at.day, 17)  # уже прошло -> завтра

    def test_assignee(self):
        self.assertEqual(parse_task("Смета для @alina_k до пятницы", NOW).assignee, "@alina_k")

    def test_no_due(self):
        self.assertIsNone(parse_task("Купить молоко", NOW).due)

    def test_remind(self):
        due = parse_task("завтра 15:00", NOW).due
        self.assertEqual(compute_remind_at(due, NOW), due.at - timedelta(hours=1))
        soon = parse_task("через 30 минут", NOW).due
        self.assertEqual(compute_remind_at(soon, NOW), soon.at)
        date_only = parse_task("завтра", NOW).due
        self.assertEqual(compute_remind_at(date_only, NOW).hour, 10)

    def test_format_due(self):
        self.assertEqual(format_due(NOW.replace(hour=18), True, NOW), "сегодня 18:00")
        self.assertEqual(format_due(NOW + timedelta(days=1), False, NOW), "завтра")
        self.assertEqual(format_due(NOW + timedelta(days=4), False, NOW), "вт 20.10")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TaskStorage(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, text, priority="medium", due=None, remind=None, chat_id=1):
        return self.db.add_task(chat_id, 10, "Тест", text, priority, "", due, due is not None, remind)

    def test_sorting_and_done(self):
        low = self.add("низкий", "low")
        high = self.add("высокий", "high")
        self.add("средний")
        self.assertEqual([r["id"] for r in self.db.list_open(1)][0], high)
        self.assertTrue(self.db.mark_done(1, high))
        self.assertFalse(self.db.mark_done(1, high))  # повторно закрыть нельзя
        self.assertEqual(len(self.db.list_open(1)), 2)
        self.assertFalse(self.db.mark_done(2, low))  # чужой чат

    def test_reminders(self):
        now = datetime.now(timezone.utc)
        task_id = self.add("напомни", due=now, remind=now - timedelta(minutes=1))
        self.add("позже", due=now, remind=now + timedelta(hours=1))
        self.assertEqual([r["id"] for r in self.db.due_reminders(now)], [task_id])
        self.db.mark_reminded(task_id)
        self.assertEqual(self.db.due_reminders(now), [])

    def test_digest_settings(self):
        self.add("задача")
        self.assertEqual(self.db.digest_chats(), [1])
        self.db.set_digest(1, False)
        self.assertEqual(self.db.digest_chats(), [])


class AppTests(unittest.TestCase):
    def test_build_application(self):
        os.environ["BOT_TOKEN"] = "123456:TEST"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DB_PATH"] = os.path.join(tmp, "app.db")
            import bot
            app = bot.build_application()
            self.assertGreater(len(app.handlers[0]), 5)
            self.assertEqual(len(app.job_queue.jobs()), 2)  # напоминания + дайджест


if __name__ == "__main__":
    unittest.main()
