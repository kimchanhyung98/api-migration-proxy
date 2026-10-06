# Docker 실행 환경

- 구성: [compose.yml](../../compose.yml), [데모 Dockerfile](../../Dockerfile), [루트 Makefile](../../../Makefile), [데모 Makefile](../../Makefile).
- 조건: Docker Engine·Compose 사용 가능. 명령은 프로젝트 루트에서 실행.
- Proxy 이미지: 제품 Dockerfile의 `runtime`. 데모 구현은 제품 패키지·Proxy 이미지에서 제외.
- 데모 이미지: 실행 결과·로컬 환경 파일·검사 캐시를 Docker build context에서 제외.

## 서비스와 실행

| 서비스 | 이미지 target | 역할 | 실행 기간 |
| --- | --- | --- | --- |
| `v1`, `v2` | 데모 `backend` | 버전별 합성 API | 상시 |
| `proxy` | 제품 `runtime` | Proxy·컨테이너 내부 health listener | 상시 |
| `user` | 데모 `user` | 외부 HTTP 호출·집계 | 일회성, `user` profile |
| `test` | 데모 `test` | 개별 기능 테스트·관측 도구 | 일회성, `test` profile |

```sh
make demo-up
make demo-request
make demo-down
```

- `demo-up`: 이미지 빌드 후 v1·v2·Proxy 시작, healthcheck 완료 대기.
- 기본 동작: v1 응답 100%, shadow OFF.
- backend healthcheck: `/health` HTTP 응답 확인.
- Proxy readiness: runtime이 요청 수락 중이고 설정이 유효한지 확인. backend 성공률·비교 품질·저장소 준비 보장 아님.
- `demo-down`: 컨테이너·네트워크 정리, 이벤트 volume 유지.

## 환경변수 기본값

```sh
cp .demo/.env.example .demo/.env
```

- 복사는 선택 사항. 파일이 없어도 아래 기본값으로 실행.
- Compose는 `.demo/.env`를 자동 사용. 제품용 `.env`와 별도이며 서로 복사할 필요 없음.
- 셸에서 지정한 같은 변수는 `.demo/.env`보다 우선.
- Proxy 설정·포트·선택 health 토큰 변경 후 `make demo-reload`로 설정 검사·Proxy 재생성.
- User 요청 수·경로·timeout 변경은 다음 `make demo-request`에 바로 반영.
- backend 공개 포트를 변경한 경우 `make demo-up`으로 해당 서비스도 반영.

| 변수 | 기본값 | 역할 |
| --- | --- | --- |
| `PROXY_PORT` | `8080` | Proxy 호스트 포트 |
| `V1_PORT` | `8101` | v1 호스트 포트 |
| `V2_PORT` | `8102` | v2 호스트 포트 |
| `DEMO_REQUESTS` | `100` | User 순차 요청 수 |
| `DEMO_TIMEOUT` | `5` | User 요청 timeout, 초 |
| `DEMO_PATH` | `/items/different` | User 요청 경로 |
| `DEMO_V2_RATIO` | `0` | v2 serving 비율, 0~1 |
| `DEMO_SHADOW_ENABLED` | `false` | 반대 backend shadow ON/OFF |
| `DEMO_CONTROL_TOKEN` | `local-demo-only` | 컨테이너 내부 health의 Bearer 토큰 |
| `PROXY_CONFIG` | 빈 값 | 고급 전체 JSON 설정 파일 경로 |

- `local-demo-only`는 공개된 로컬 편의값이며 제품 기본 토큰이 아님. health는 토큰 유무와 무관하게 실제 loopback 접속만 허용.
- Compose에서 데모 변수를 제품의 `API_PROXY_` 환경변수로 전달. 제품 코드가 데모 변수명을 직접 해석하지 않음.
- 기본 환경변수 모드의 Docker 내부 backend DNS는 `v1`, `v2`, 대상 route는 `GET /items/{id}`로 고정.
- 합성 읽기 전용 `review_ref`와 실행·저장 한도는 Compose·코드에 고정. 일반 실행용 `.env`에는 노출하지 않음.

## 주소와 포트

| 서비스 | 기본 호스트 주소 | Docker 내부 주소 |
| --- | --- | --- |
| v1 | `http://127.0.0.1:8101` | `http://v1:8000` |
| v2 | `http://127.0.0.1:8102` | `http://v2:8000` |
| Proxy | `http://127.0.0.1:8080` | `http://proxy:8080` |
| health | 호스트 공개 없음 | Proxy 컨테이너 안의 `http://127.0.0.1:9090/healthcheck` |

- 모든 호스트 포트는 loopback에만 공개.
- health는 Proxy 컨테이너의 `127.0.0.1`에만 bind. 다른 컨테이너·호스트에서 직접 조회하지 않고 [컨테이너 내부 명령](../proxy/README.md#관측) 사용.
- 브라우저 확인: Proxy의 `/items/different`. [버전별 응답](../backends/README.md) 참고.

```sh
PROXY_PORT=18080 V1_PORT=18101 V2_PORT=18102 make demo-up
```

- 셸로 덮어쓴 설정은 관련 명령에도 동일하게 전달하거나 `.demo/.env`에 저장.

## 고급 JSON 설정

- `PROXY_CONFIG`가 비어 있으면 환경변수로 기본 설정 구성.
- 전체 JSON을 지정하면 route·serving·shadow·비교 정책은 JSON 기준. `DEMO_V2_RATIO`·`DEMO_SHADOW_ENABLED`보다 우선.
- 상대 경로는 `.demo` 기준. 호스트용 backend 주소와 Docker 내부 DNS 주소 혼용 금지.

```sh
cp .demo/config/docker.json .demo/config/custom.json
PROXY_CONFIG=./config/custom.json make demo-up
PROXY_CONFIG=./config/custom.json make demo-reload
```

- JSON의 revision·previous revision·변경 사유도 함께 갱신. mount한 설정과 실제 응답 분포를 확인하고, shadow ON에서는 저장 이벤트의 revision도 대조. health는 revision을 반환하지 않음.
- [config/local.json](../../config/local.json)은 호스트 backend 8101·8102를 사용하는 별도 예시.
- 재적용은 설정 검사 후 Proxy 재생성. hot reload 아님.

## 데이터와 정리

- 기본 Compose project: `api-migration-proxy`.
- 이벤트 저장: `events` named volume의 `/data/events.sqlite`. 기본 실제 이름은 `api-migration-proxy_events`.
- Proxy는 저장소에 쓰며, 시나리오는 Proxy의 정상 종료를 확인한 뒤 별도 컨테이너에서 읽기 전용 SQLite 연결로 조회.
- 선택 smoke의 테스트 컨테이너는 같은 volume을 읽기 전용 mount하고 읽기 전용 연결 사용.
- Proxy 재생성·`demo-down`은 저장 완료된 이벤트 보존. 데이터가 필요하면 `down -v`·volume 삭제 금지.
- 종료 중 메모리 작업의 무손실·호스트 장애 복구·운영 DB 내구성을 보장하지 않음.
- [순차 시나리오](../validation/scenarios.md)·[단일 분배 검사](../validation/distribution.md)는 독립 project를 생성하고 자신이 만든 자원만 정리.

## 실행기 상세

- [Proxy CLI](proxy-cli.md): 호스트 직접 실행 기본값·설정 선택·진단·revision 한계.
- [관측과 이벤트](observation.md): 현재 출력·저장·로그 방식과 조회 범위.
