FROM python:3.11-slim AS builder
WORKDIR /wheels
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir -r requirements.txt -w /wheels

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=builder /wheels /wheels
# note the .whl glob so we ignore requirements.txt
RUN pip install --no-cache-dir /wheels/*.whl
COPY . .
CMD ["python", "main.py"]
