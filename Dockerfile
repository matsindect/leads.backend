# Multi-stage build for minimal production image
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install .

# --- Production image ---
FROM python:3.11-slim

RUN groupadd --gid 1001 app \
    && useradd --uid 1001 --gid app --shell /bin/false app

COPY --from=builder /install /usr/local
COPY alembic/ /app/alembic/
COPY alembic.ini /app/
COPY src/ /app/src/

WORKDIR /app

ENV PYTHONPATH=/app/src

USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
