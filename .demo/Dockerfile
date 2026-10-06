FROM python:3.12-slim AS dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /demo
COPY requirements-dev.txt /tmp/constraints.txt
RUN python -m pip install --no-cache-dir -c /tmp/constraints.txt fastapi httpx uvicorn
USER 10001:10001

FROM dependencies AS backend
COPY .demo/backend.py ./backend.py
COPY .demo/v1/ ./v1/
COPY .demo/v2/ ./v2/

FROM dependencies AS user
COPY .demo/user/ ./user/
ENTRYPOINT ["python", "-m", "user.cli"]
CMD ["--help"]

FROM dependencies AS test
USER root
COPY pyproject.toml /proxy/pyproject.toml
COPY src/ /proxy/src/
RUN python -m pip install --no-cache-dir -r /tmp/constraints.txt /proxy
COPY .demo/ ./
USER 10001:10001
CMD ["sh", "-c", "python -m ruff check --no-cache backend.py v1 v2 user scenarios tests && python -m ruff format --check --no-cache backend.py v1 v2 user scenarios tests && python -m mypy --cache-dir /tmp/demo-mypy backend.py v1 v2 user scenarios && python -m pip check && python -m pytest -c pyproject.toml"]
