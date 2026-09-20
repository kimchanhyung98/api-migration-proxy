# 로컬 실행

Python/FastAPI 프록시와 합성 v1/v2 API를 실행하는 예제입니다. 운영 인프라와 DB 선택을 전제하지 않습니다. SQLite는 로컬 검증용 이벤트 저장소입니다.

## Docker

Docker Engine과 Compose가 실행 중인 환경에서 프로젝트 루트에서 실행합니다.

```sh
make docker-up
curl http://127.0.0.1:8080/items/example
make docker-smoke
make docker-test
make docker-down
```

호스트에는 프록시의 `127.0.0.1:8080`만 공개합니다. 포트 충돌 시 `PROXY_PORT=18080 make docker-up`으로 변경할 수 있습니다. 이벤트는 Compose 볼륨에 저장하며 `docker-down`으로 삭제하지 않습니다. Docker의 프록시 healthcheck는 TCP 수락 여부만 확인하며, 비교 품질이나 저장소 준비를 보장하지 않습니다.

`docker-test`는 호스트 소스를 마운트하지 않고 이미지 안에서 린트·형식·타입 검사와 전체 테스트를 실행합니다. `docker-smoke`는 기본 Docker 설정에서 실제 HTTP 요청, 오류 전달, 양쪽 실행과 이벤트 저장을 확인합니다.

## 로컬 Python

Python 3.12 이상을 사용합니다.

```sh
make init
make check
make build
```

두 터미널에서 합성 backend를 각각 실행합니다.

```sh
BACKEND_VERSION=v1 .venv/bin/python -m uvicorn examples.backend:app --port 8101 --no-access-log --no-proxy-headers
BACKEND_VERSION=v2 .venv/bin/python -m uvicorn examples.backend:app --port 8102 --no-access-log --no-proxy-headers
```

별도 터미널에서 프록시를 실행합니다. `local.json`은 v1 통과만 활성화한 설정입니다.

```sh
.venv/bin/python -m api_migration_proxy.cli check-config --config examples/local.json
make run
curl http://127.0.0.1:8080/items/example
```

다른 파일·포트·저장소를 사용할 때는 `make run CONFIG=설정.json PORT=18080 EVENT_STORE=events.sqlite`로 지정합니다.

## 실행과 비교 확인

`docker.json`은 부작용 없는 합성 API에만 shadow를 100% 활성화하고 serving은 v1으로 유지합니다. `/items/missing`은 404, `/items/error`는 500을 반환합니다. `/items/slow`는 v2만 지연시키며 `/items/different`는 양쪽의 `name` 값을 다르게 반환합니다.

일반 CLI는 실제 서비스의 데이터·권한 문맥을 임의로 신뢰하지 않습니다. 따라서 정상 응답 쌍은 문맥 미확인으로 `not_comparable`, 예상 밖 500은 `execution_error`로 기록합니다. 테스트에서는 명시적인 합성 문맥을 주입하여 `matched`·`different`, 마스킹, timeout, 저장 실패와 종료 처리까지 검증합니다. 운영 API의 신원·업무 오류·데이터 문맥은 별도로 연결해야 합니다.

Docker 설정의 `snapshot.routes[0].v2_serve_ratio`를 바꾸면 사용자 응답 비율을, `shadow.sample_ratio`를 바꾸면 반대편 실행 비율을 제어합니다. 설정 변경 시 revision·변경 사유도 갱신하고 검증 후 재시작합니다.

```sh
docker compose exec -T proxy api-migration-proxy check-config --config examples/docker.json
docker compose restart proxy
```

기본 `docker-smoke`는 v1 serving·shadow 100% 설정을 검사합니다. 변경한 비율의 동작은 응답의 `x-backend-version`과 저장된 이벤트에서 확인합니다. 설정 재시작은 로컬 확인 절차이며 무중단 전환이나 운영 변경 이력의 내구성을 보장하지 않습니다.

메트릭과 상태 조회용 `create_control_app()`은 프록시 경로와 분리되어 있으며 명시적 접근 검증 함수를 요구합니다. 일반 CLI에서 공개 포트에 자동으로 붙이지 않습니다. 실제 모니터링·인증·배포 연동과 대표 부하 검증은 이 예제의 범위 밖입니다.
