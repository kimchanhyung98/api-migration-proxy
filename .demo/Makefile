.PHONY: help check lint typecheck test up down reload docker-test smoke request distribution

PYTHON ?= ../.venv/bin/python
COMPOSE = docker compose -f compose.yml
SERVING_BACKEND ?= v1
REPORT ?= results/user.json
DEMO_DISTRIBUTION_REQUESTS ?= 1000
DEMO_DISTRIBUTION_V2_RATIO ?= 0.5
DEMO_DISTRIBUTION_TOLERANCE ?= 0.08

.DEFAULT_GOAL := help

help: ## 데모 명령 목록
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*?##/ { printf "  %-15s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

check: lint typecheck test ## 데모 코드·테스트만 검증

lint: ## 데모 코드 린트·형식 검사
	$(PYTHON) -m ruff check --no-cache backend.py v1 v2 user tests
	$(PYTHON) -m ruff format --check --no-cache backend.py v1 v2 user tests

typecheck: ## 데모 실행 코드 타입 검사
	$(PYTHON) -m mypy --cache-dir /tmp/api-migration-proxy-demo-mypy backend.py v1 v2 user tests/distribution.py tests/run_distribution.py

test: ## 데모 테스트 실행
	$(PYTHON) -m pytest -c pyproject.toml

up: ## 더미 v1·v2와 제품 Proxy 실행
	$(COMPOSE) up --build -d --wait v1 v2 proxy

down: ## 데모 컨테이너 중지·제거 (이벤트 볼륨 유지)
	$(COMPOSE) down

reload: ## 설정 검사 후 Proxy만 재생성
	$(COMPOSE) run --rm --no-deps proxy check-config
	$(COMPOSE) up -d --wait --no-deps --force-recreate proxy

docker-test: ## 별도 데모 이미지에서 데모 검사 실행
	$(COMPOSE) --profile test run --build --rm --no-deps test

smoke: ## 실행 중인 데모 HTTP·내부 이벤트 저장 검증
	$(COMPOSE) --profile test run --build --rm --no-deps test python -m tests.smoke --url http://proxy:8080 --event-store /data/events.sqlite --serving "$(SERVING_BACKEND)"

request: ## Proxy에 요청을 보내 응답 비율·오류·지연 기록
	$(COMPOSE) build user
	@mkdir -p "$(dir $(REPORT))"
	$(COMPOSE) --profile user run --rm --no-deps user > "$(REPORT)"
	@cat "$(REPORT)"

distribution: ## 독립 Docker 환경에서 serving 응답 분배 검증
	$(PYTHON) -m tests.run_distribution --requests "$(DEMO_DISTRIBUTION_REQUESTS)" --v2-ratio "$(DEMO_DISTRIBUTION_V2_RATIO)" --tolerance "$(DEMO_DISTRIBUTION_TOLERANCE)"
