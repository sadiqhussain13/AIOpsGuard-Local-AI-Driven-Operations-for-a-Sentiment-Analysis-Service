# syntax=docker/dockerfile:1
# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.10-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.10-slim AS runtime

# Non-root user
RUN addgroup --gid 1001 appgroup \
 && adduser --uid 1001 --gid 1001 --no-create-home --disabled-password appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY app.py fault_injector.py metrics.py ./
COPY templates ./templates
COPY static ./static

# Persistent data directory (incident history DB)
RUN mkdir -p /app/data && chown 1001:1001 /app/data

USER appuser

ENV PORT=5000 \
    OLLAMA_BASE_URL=http://ollama:11434 \
    OLLAMA_MODEL=mistral \
    FAILURE_RATE=0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

# Use gunicorn for production; the WSGI entrypoint is `app:application`
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", \
     "--timeout", "120", "app:application"]
