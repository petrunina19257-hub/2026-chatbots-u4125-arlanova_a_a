# Vibe Coding: AI-боты для бизнеса — лабораторные работы

Курс [Vibe Coding: AI-боты для бизнеса](https://github.com/itmo-ict-faculty/vibe-coding-for-business), ИТМО, ФИКТ, 2026/2027.
Автор: Арланова Алина Андреевна, группа U4125.

Все три лабораторные — это развитие одного продукта: **Telegram-бота для задач** [@task_hunter_bot](https://t.me/task_hunter_bot). Задача отправляется командой `/task текст` в личке или в общем чате и попадает в Google Таблицу.

У бота две версии:
- **рабочая — на Google Apps Script** ([`lab1/apps-script/Code.gs`](lab1/apps-script/Code.gs)): работает в облаке Google по триггеру с лета 2026, ей пользуются коллеги в рабочем чате;
- **расширенная — на Python** ([`lab1/bot`](lab1/bot) → [`lab2/bot`](lab2/bot) → [`lab3/bot`](lab3/bot)): понимает приоритеты и дедлайны на русском, напоминает о задачах, работает с Excel, собирает отзывы.

| Лаба | Что сделано | Отчёт |
|---|---|---|
| 1 | Бот на Apps Script: `/task` из Telegram → Google Таблица. Версия на Python: приоритеты, дедлайны, `/list`, `/today`, напоминания, SQLite | [lab1_report.md](lab1/lab1_report.md) |
| 2 | Интеграция с Telegram API и Google Таблицей; в версии на Python — синхронизация через Sheets API, экспорт/импорт Excel и CSV, `/find`, `/stats` | [lab2_report.md](lab2/lab2_report.md) |
| 3 | Деплой в облако Google по триггеру, реальные пользователи и статистика; версия на Python подготовлена к запуску через ngrok и Docker, с `/feedback` и `/admin` | [lab3_report.md](lab3/lab3_report.md) |

## Запуск версии на Apps Script

Таблица → **Расширения → Apps Script** → вставить [`Code.gs`](lab1/apps-script/Code.gs), указать токен и ID таблицы → **Триггеры → Добавить триггер** → `pollTelegram`, по времени, каждую минуту.

## Запуск версии на Python

```bash
cd lab3/bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # вставить BOT_TOKEN; для простого запуска MODE=polling
python bot.py
```

Автотесты (без интернета и токена): `python -m unittest -v` в любой папке `labN/bot`.

## Как делалось

Код написан с помощью LLM по промптам на естественном языке (версия на Python — ИИ-агентом Claude Code); промпты и итерации описаны в отчётах.

Интересные решения:
- бот на Apps Script работает без сервера: триггер раз в минуту забирает сообщения через `getUpdates`, а `offset` в свойствах скрипта не даёт задачам задвоиться;
- разбор русских дат без внешних библиотек: «до пятницы», «через 2 часа», «20 октября»; распознанное убирается из текста задачи;
- SQLite — главное хранилище, а Google Таблица — необязательное «зеркало»: бот работает, даже когда Google недоступен;
- отзывы и метрики собираются внутри бота, без отдельных форм.
