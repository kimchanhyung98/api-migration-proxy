FROM python:3.13-slim AS application

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_PROXY_EVENT_STORE=/data/events.sqlite
WORKDIR /app
COPY pyproject.toml requirements-dev.txt ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir -c requirements-dev.txt . \
    && mkdir -m 0700 /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["api-migration-proxy"]
CMD ["--help"]

FROM application AS test
USER root
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
COPY .env.example ./.env.example
USER 10001:10001
ENTRYPOINT []
CMD ["sh", "-c", "python -m ruff check --no-cache src tests && python -m ruff format --check --no-cache src tests && python -m mypy --cache-dir /tmp/mypy src/api_migration_proxy && python -m pip check && python -m pytest"]

FROM application AS runtime
