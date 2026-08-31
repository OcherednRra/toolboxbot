FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY robloxbox ./robloxbox
COPY scripts ./scripts

# На Railway сюда монтируется volume, иначе очередь теряется при передеплое.
VOLUME ["/data"]

CMD ["python", "-m", "robloxbox"]
