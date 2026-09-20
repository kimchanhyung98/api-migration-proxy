.PHONY: help check test lint typecheck build init run docker-up docker-down docker-test docker-smoke

PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3
CONFIG ?= examples/local.json
EVENT_STORE ?= events.sqlite
PORT ?= 8080

.DEFAULT_GOAL := help

help: ## 사용 가능한 명령어 목록 출력
	@awk 'BEGIN {FS = ":.*##"; printf "\n사용법:\n  make \033[36m<target>\033[0m\n\n명령어:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

check: lint typecheck test ## 린트, 타입 검사 및 테스트 실행

test: ## 단위 및 로컬 HTTP 통합 테스트 실행
	$(PYTHON) -m pytest

lint: ## Python 코드 린트 및 형식 검사
	$(PYTHON) -m ruff check --no-cache src tests examples
	$(PYTHON) -m ruff format --check src tests examples

typecheck: ## Python 소스 타입 검사
	$(PYTHON) -m mypy --cache-dir /tmp/api-migration-proxy-mypy src/api_migration_proxy

build: ## 배포용 Python 패키지 빌드
	$(PYTHON) -m build

init: ## Python 가상 환경과 개발 의존성 설치
	@test -d .venv || $(PYTHON_BOOTSTRAP) -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt -e '.[test]'

run: ## 설정 파일로 로컬 프록시 실행
	$(PYTHON) -m api_migration_proxy.cli serve --config "$(CONFIG)" --port "$(PORT)" --event-store "$(EVENT_STORE)"

docker-up: ## 합성 v1/v2와 로컬 프록시 실행
	docker compose up --build -d --wait

docker-down: ## 로컬 컨테이너 중지 및 제거 (이벤트 볼륨 유지)
	docker compose down

docker-test: ## 컨테이너에서 린트·타입 검사·테스트 실행
	docker compose --profile test run --build --rm --no-deps test

docker-smoke: ## 실행 중인 Docker 프록시의 HTTP·이벤트 저장 확인
	docker compose exec -T proxy python examples/smoke.py --url http://127.0.0.1:8080 --event-store /data/events.sqlite
