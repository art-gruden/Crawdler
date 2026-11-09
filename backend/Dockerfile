FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY gcc_core ./gcc_core
COPY company_discovery ./company_discovery
RUN useradd --system --uid 10001 --home /app gcc && mkdir -p /data && chown gcc /data
USER gcc
EXPOSE 8000
# Default: API. docker-compose overrides the command for the workers and beat.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn gcc_core.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
