FROM python:3.12-slim

WORKDIR /app

# Системные зависимости (curl нужен для healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Зависимости Python — отдельным слоем для кэширования
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходники приложения
COPY app/ ./app/

# Непривилегированный пользователь
RUN useradd -m -u 1001 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-config", "app/log_config.json"]
