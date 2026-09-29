.PHONY: help check test lint typecheck build init run docker-build docker-test

PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3
CONFIG ?=
EVENT_STORE ?= events.sqlite
PORT ?= 8080

.DEFAULT_GOAL := help

help: ## 사용 가능한 명령어 목록 출력
	@awk 'BEGIN {FS = ":.*##"; printf "\n사용법:\n  make \033[36m<target>\033[0m\n\n명령어:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

check: lint typecheck test ## 린트, 타입 검사 및 테스트 실행

test: ## 단위 및 로컬 HTTP 통합 테스트 실행
	$(PYTHON) -m pytest

lint: ## Python 코드 린트 및 형식 검사
	$(PYTHON) -m ruff check --no-cache src tests
	$(PYTHON) -m ruff format --check src tests

typecheck: ## Python 소스 타입 검사
	$(PYTHON) -m mypy --cache-dir /tmp/api-migration-proxy-mypy src/api_migration_proxy

build: ## 배포용 Python 패키지 빌드
	$(PYTHON) -m build

init: ## Python 가상 환경과 개발 의존성 설치
	@test -d .venv || $(PYTHON_BOOTSTRAP) -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt -e '.[test]'

run: ## 설정 파일로 로컬 프록시 실행
	@test -n "$(CONFIG)" || { echo 'CONFIG에 Proxy 설정 파일 경로를 지정하세요.' >&2; exit 2; }
	$(PYTHON) -m api_migration_proxy.cli serve --config "$(CONFIG)" --port "$(PORT)" --event-store "$(EVENT_STORE)"

docker-build: ## Proxy 전용 실행 이미지 빌드
	docker build --target runtime -t api-migration-proxy .

docker-test: ## 컨테이너에서 린트·타입 검사·테스트 실행
	docker build --target test -t api-migration-proxy-test .
	docker run --rm api-migration-proxy-test
