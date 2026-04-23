FROM python:3.10-slim
WORKDIR /app
# Установка системных зависимостей для Postgres драйвера
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*
RUN pip install pyTelegramBotAPI psycopg2-binary
COPY support_bot.py .
CMD ["python", "support_bot.py"]
