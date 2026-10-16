# Task Assistant Bot — лабораторная 3 (запуск для пользователей)

Бот из лабораторной 2, подготовленный к работе с реальными людьми:

- **webhook-режим** для запуска через ngrok (`MODE=webhook`), обычный polling тоже остался;
- **сбор отзывов**: `/feedback` → оценка ⭐1–5 кнопками → текст отзыва; каждый отзыв сразу приходит администратору;
- **статистика использования**: каждое действие пишется в базу, `/admin` показывает пользователей, активность, популярные команды и среднюю оценку;
- **логи в файл** `bot.log` с ротацией (5 файлов по 1 МБ);
- `Dockerfile` и `docker-compose.yml` — запасной вариант для сервера.

## Запуск через ngrok

### 1. Один раз: установить и настроить ngrok

```bash
brew install ngrok
```

Зарегистрируйся на [ngrok.com](https://ngrok.com/), скопируй authtoken со страницы **Your Authtoken** и выполни:

```bash
ngrok config add-authtoken ТВОЙ_NGROK_ТОКЕН
```

На бесплатном тарифе ngrok даёт один постоянный домен (**Domains** в кабинете) — с ним адрес не меняется после перезапуска.

### 2. Установить зависимости бота

```bash
cd lab3/bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 3. Терминал 1 — туннель

```bash
ngrok http 8000
```

Или с постоянным доменом:

```bash
ngrok http --url=твой-домен.ngrok-free.app 8000
```

Скопируй адрес из строки `Forwarding` (`https://....ngrok-free.app`).

### 4. Заполнить `.env`

```
BOT_TOKEN=токен от BotFather
MODE=webhook
WEBHOOK_URL=https://....ngrok-free.app
PORT=8000
WEBHOOK_SECRET=любая-случайная-строка
```

### 5. Терминал 2 — бот

```bash
python bot.py
```

В логе появится `Bot started (webhook) on port 8000`. Бот сам регистрирует webhook в Telegram, открывать `setWebhook` в браузере не нужно. Проверить можно так (подставь токен):

```text
https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```

В ответе должен быть твой `url` и `pending_update_count: 0`. Входящие запросы видны в веб-панели ngrok: http://127.0.0.1:4040.

### 6. Стать администратором

Отправь боту `/admin` — он ответит твоим Telegram ID. Впиши его в `ADMIN_IDS` в `.env` и перезапусти бота.

## Команды

Всё из лаб 1–2 плюс:

| Команда | Кто | Что делает |
|---|---|---|
| `/feedback` | все | оценка кнопками ⭐1–5, затем текст отзыва |
| `/feedback текст` | все | сразу оставить отзыв текстом |
| `/skip` | все | не писать текст после оценки |
| `/admin` | админ | статистика использования |
| `/feedbacks` | админ | последние 20 отзывов |

## Стабильность

- Логи: `tail -f bot.log`
- Посмотреть, сколько памяти занимает бот (запусти пару раз с перерывом в несколько часов):

```bash
ps -o pid,rss,etime,command -p $(pgrep -f "python bot.py")
```

`rss` — память в КБ, `etime` — сколько бот уже работает.

- Чтобы Mac не уснул и бот работал, пока открыт ноутбук:

```bash
caffeinate -i python bot.py
```

## Docker (запасной вариант для сервера)

```bash
docker compose up -d --build
docker compose logs -f
```

В `docker-compose.yml` стоит `MODE=polling`, база и логи сохраняются в `./data`.
