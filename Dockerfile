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
# CA bundle for runtime SSL (snscrape/requests)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt
COPY . .
CMD ["python", "main.py"]
