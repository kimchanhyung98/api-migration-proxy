FROM python:3.12-slim AS application

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements-dev.txt ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir -c requirements-dev.txt . \
    && mkdir /data && chown 10001:10001 /data
USER 10001:10001

FROM application AS test
USER root
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
COPY examples/ ./examples/
USER 10001:10001
CMD ["sh", "-c", "python -m ruff check --no-cache src tests examples && python -m ruff format --check --no-cache src tests examples && python -m mypy --cache-dir /tmp/mypy src/api_migration_proxy && python -m pip check && python -m pytest"]

FROM application AS local
COPY examples/ ./examples/
CMD ["api-migration-proxy", "serve", "--config", "examples/docker.json", "--host", "0.0.0.0", "--port", "8080", "--event-store", "/data/events.sqlite"]
