# syntax=docker/dockerfile:1

FROM python:3.11-slim AS builder
WORKDIR /wheels
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir -r requirements.txt -w /wheels

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# CA untuk runtime SSL (requests/snscrape)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
# ⬇️ HANYA pasang wheel; tidak membaca requirements.txt lagi
RUN pip install --no-cache-dir /wheels/*.whl
COPY . .
CMD ["python", "main.py"]
