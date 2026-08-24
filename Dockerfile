FROM python:3.11-slim AS base

# System deps: none of QuantSphere's Python packages need compiling from
# source on this base image except asyncpg/cryptography's wheels, which are
# published for slim — no build-essential needed.
WORKDIR /app

COPY pyproject.toml ./
# Install dependencies first (better layer caching — this layer only
# rebuilds when pyproject.toml changes, not on every code edit). The
# MetaTrader5 dependency's `platform_system == 'Windows'` marker means pip
# correctly skips it on this Linux image.
RUN pip install --no-cache-dir .

COPY app ./app
COPY config ./config
COPY frontend ./frontend

# Runs as a non-root user — this image never needs root once dependencies
# are installed.
RUN useradd --create-home --uid 1000 quantsphere \
    && mkdir -p /app/media/trade_screenshots \
    && chown -R quantsphere:quantsphere /app
USER quantsphere

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
