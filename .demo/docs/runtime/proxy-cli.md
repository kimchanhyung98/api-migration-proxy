# 로컬 검증용 Proxy CLI 실행

- 범위: 현재 실행기의 호스트 실행 기본값·진단 안내; 최종 배치나 관측 도구 선정 의미 없음
- Docker 데모는 [Compose 설정](README.md) 사용
- 목적: 자주 바꾸는 실행값을 `.env`로 관리하고 복잡한 route·비교 정책은 선택적인 JSON 설정으로 분리
- 기본 실행: JSON 없이 `api-migration-proxy serve`로 기동; v1·v2 API는 운영자가 별도로 제공하는 외부 목적지

## 설정 선택과 우선순위

- 적용 순서: 명시적인 CLI 옵션 > 프로세스 환경변수 > 현재 작업 디렉터리의 `.env` > 코드 기본값
- `.env` 부재: 기본값으로 실행; `.env.example`은 실제 기본값을 기록하며 진짜 토큰·credential을 포함하지 않음
- `serve`·`check-config`·`purge-events`의 `--env-file`: 읽을 환경 파일 선택; 기본 경로는 현재 작업 디렉터리의 `.env`
- `API_PROXY_CONFIG`가 비어 있으면 단순 환경변수 모드 사용; 파일 경로를 지정하면 완전한 JSON 설정 사용
- JSON 모드: routing·comparison·실행 및 수집 예산은 JSON이 소유; 환경변수의 목적지·route·v2 비율·shadow 값으로 덮어쓰지 않음
- listener 주소·port, 저장소 선택·자동 만료 정리, health 활성화·접근 설정: JSON 모드에서도 CLI·환경변수 우선순위 적용
- timeout·동시 실행·캡처·큐·저장 재시도 예산: 단순 모드에서는 유한한 코드 기본값 사용; 고급 조정은 JSON에서 수행
- 설정 변경: 다시 검증한 뒤 프로세스 재시작으로 적용; 실행 중 `.env` 자동 재로딩 없음
- 논리 모델의 YAML `null`: [설정 모델](../../../docs/routing/configuration.md)의 미정 값 의미 유지; 실행 기본값과 구분

## 자주 변경하는 환경변수

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `API_PROXY_CONFIG` | 빈 값 | 선택적인 완전 JSON 설정 파일 경로 |
| `API_PROXY_HOST` | `127.0.0.1` | 사용자 요청 listener bind 주소 |
| `API_PROXY_PORT` | `8080` | 사용자 요청 listener port |
| `API_PROXY_EVENT_STORE` | `events.sqlite` | SQLite 이벤트 저장 파일 |
| `API_PROXY_EVENT_STORE_BACKEND` | `sqlite` | `sqlite` 또는 `postgresql` 저장소 선택 |
| `API_PROXY_POSTGRES_DSN` | 빈 값 | PostgreSQL 선택 시 필요한 연결 설정 |
| `API_PROXY_RETENTION_INTERVAL_SECONDS` | `60` | 자동 만료 정리 주기, 초 |
| `API_PROXY_RETENTION_BATCH_SIZE` | `1000` | 정리 한 번에 삭제할 상세·요약 행 각각의 상한 |
| `API_PROXY_V1_URL` | `http://127.0.0.1:8001` | 단순 모드의 기본 v1 origin |
| `API_PROXY_V2_URL` | `http://127.0.0.1:8002` | 단순 모드의 등록 route용 v2 origin |
| `API_PROXY_ROUTE_PATH` | 빈 값 | 단순 모드에서 등록할 GET path template; 빈 값이면 전환 route 없음 |
| `API_PROXY_V2_RATIO` | `0` | 등록 route의 v2 serving 비율; `0`~`1` |
| `API_PROXY_SHADOW_ENABLED` | `false` | 등록 route의 shadow 활성화 |
| `API_PROXY_SHADOW_REVIEW_REF` | 빈 값 | shadow의 실제 효과·권한·데이터 검토 근거 참조 |
| `API_PROXY_CONTROL_ENABLED` | `true` | 별도 loopback health listener 활성화 |
| `API_PROXY_CONTROL_HOST` | `127.0.0.1` | health listener bind 주소; literal loopback만 허용 |
| `API_PROXY_CONTROL_PORT` | `9090` | health listener port |
| `API_PROXY_CONTROL_TOKEN` | 빈 값 | health 요청의 선택 bearer token; token 유무와 무관하게 loopback peer만 허용 |

- v1·v2 기본 주소: 제품이 API를 함께 생성하거나 실행한다는 의미 없음; 실제 요청 성공에는 해당 주소의 외부 API 필요
- origin: 고정 HTTP(S) origin만 허용; credential·path·query를 목적지 주소에 포함하지 않음
- 잘못된 비율·port·boolean·목적지·shadow 검토값: 실행 전 검증 실패; 오류 출력에 입력 원문이나 비밀값 포함 금지
- 비loopback health bind·접속: token 유무와 무관하게 거부; 상세 접근 계약은 [실행 중 관측](observation.md) 기준

## 단순 모드의 라우팅·비교 범위

- route 미지정: 등록 전환 route 없이 일반 HTTP 요청을 기본 v1으로 전달
- route 미지정 상태의 `API_PROXY_V2_RATIO>0` 또는 `API_PROXY_SHADOW_ENABLED=true`: 실행 전 설정 검증 실패; 명시적인 route 등록 후 활성화
- route 지정: GET 하나, 요청 단위 cohort, 기본 v2 비율 `0`, 기본 shadow OFF
- 미일치 method·path: 기본 v1 전달 유지
- 요청 단위 cohort: 무상태 API에 적용하는 요청별 독립 배정; 사용자·세션·테넌트의 연속 요청 일관성 보장 제외
- shadow ON: 비어 있지 않은 검토 참조 필수; 적격 요청의 표본 비율은 `1`이며 실행 상한·본문 제한·취소 조건은 계속 적용
- GET method만으로 복제 안전성 확정 금지; 검토 참조는 실제 효과·권한·데이터 검토를 가리켜야 함
- 다중 route, GET 외 method, 안정적인 cohort, shadow 부분 표본, 비교 규칙·자원 예산 변경: JSON 모드 사용
- 기본 CLI: 신뢰된 identity·비교 문맥 provider 없음; JSON에 안정적인 cohort를 지정해도 필수 키가 없으면 v1 배정·`missing_cohort_key` 유지
- 비교 문맥 미확인: 양쪽 정상 응답이 같아도 `not_comparable`; JSON·shadow 활성화만으로 정상 비교 가능 상태가 되지 않음
- 요청 헤더·cookie·token 전달과 신뢰된 identity·데이터 문맥 검증: 별도 책임; 호출자 임의 헤더를 신뢰된 값으로 승격하지 않음

## 응답 계약과 관측 구간의 한계

- 기본 응답 계약: 2xx만 성공으로 등록, 예상 거절 미등록; 404는 `unknown` 분류이며 `not_comparable`·`contract_class_unknown` 기록. API별 계약은 JSON으로 지정
- 단순 모드 revision: 정규화된 라우팅 설정의 `env-` fingerprint; 관측 `epoch_id`도 같은 값 사용
- 동일 설정 재시작 또는 비율 `0 → 1 → 0` 복귀 시 최초와 복귀 구간 식별자 재사용
- 식별자만으로 실행 구간·변경 이력 분리 불가; 동일 revision의 저장 이벤트 합산 시 구간 혼합 가능
- [순차 시나리오](../validation/scenarios.md)는 단계별 JSON revision·epoch와 산출물로 구간 분리
- 제품의 변경 이력·관측 구간 분리 요구사항을 대체하지 않음

## CLI 사용과 진단

```sh
cp .env.example .env
api-migration-proxy check-config --explain
api-migration-proxy serve
```

- `check-config`: backend 네트워크 호출 없이 동일한 설정 선택·검증 수행
- `check-config --explain`: 유효성 외에 설정 모드, 등록 route·cohort·v2 비율·shadow, identity·비교 문맥의 지원 한계를 안전한 요약으로 출력
- 진단 제외 값: backend origin, bearer token, credential, identity 원문; 설정 전체나 요청 헤더를 출력하지 않음
- 진단 성공: 설정과 지원 범위 확인; backend 가용성·저장 성공·실제 전환 준비의 증거 아님
- `serve --config /path/to/proxy.json`: 복잡한 정책을 완전 JSON으로 선택하는 실행 방식
- `serve --host 127.0.0.1 --port 8080 --event-store events.sqlite`: 해당 실행값을 환경변수보다 우선 지정
- `purge-events --batch-size 1000`: 환경 파일·환경변수로 선택한 기존 저장소에서 만료 데이터를 한 배치 정리. `--event-store`를 지정하면 SQLite 선택을 우선 적용하며, 새 저장소를 생성하지 않음
- health 기본 조회: 같은 호스트의 `http://127.0.0.1:9090/healthcheck` 사용. 메트릭·상태 HTTP API와 이전 health 경로는 제공하지 않음

## 검증 항목

- 기능 단위 검증: 환경 파일 우선순위·기본값·JSON 선택·안전한 진단·관측 접근 검사
- HTTP 통합 검증: JSON 없는 기동·v1 전달, GET route 배정, identity·비교 문맥 한계, 별도 listener 접근

## 관련 기능

- [설정 검증과 적용](../../../docs/routing/configuration.md)
- [응답 선택과 cohort](../../../docs/routing/serving-and-cohorts.md)
- [비교 문맥과 결과](../../../docs/comparison/context-and-outcomes.md)
- [실행 중 관측](observation.md)
