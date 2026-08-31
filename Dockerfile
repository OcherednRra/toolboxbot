FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY robloxbox ./robloxbox
COPY scripts ./scripts

# Каталог для SQLite. На Railway поверх него монтируется volume — своя
# директива VOLUME здесь не нужна и мешает платформе.
RUN mkdir -p /data

CMD ["python", "-m", "robloxbox"]
