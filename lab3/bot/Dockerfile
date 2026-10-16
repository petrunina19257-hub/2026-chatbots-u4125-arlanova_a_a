FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# база и логи лежат в /app/data, чтобы переживать пересоздание контейнера
ENV DB_PATH=/app/data/tasks.db LOG_FILE=/app/data/bot.log
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
