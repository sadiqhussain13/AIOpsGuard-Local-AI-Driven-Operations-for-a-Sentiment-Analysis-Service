# syntax=docker/dockerfile:1
# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.10-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.10-slim AS runtime

RUN addgroup --gid 1001 appgroup \
 && adduser --uid 1001 --gid 1001 --no-create-home --disabled-password appuser

WORKDIR /app

COPY --from=builder /install /usr/local

# Copy source files
COPY predict_server.py ./
# Model artefact is mounted at runtime via volume or copied in CI
RUN mkdir -p model && chown appuser:appgroup model

USER appuser

ENV PORT=8080 \
    MODEL_PATH=/app/model/anomaly_model.pkl \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "predict_server:application"]
