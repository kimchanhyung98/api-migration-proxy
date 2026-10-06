# 로컬 실행의 관측과 이벤트 확인

- 범위: 컨테이너 내부 health·더미 backend 요청 수·SQLite 저장·로그 확인. 운영 관측 도구 선정 의미 없음.
- 실행 명령: [데모 Proxy 관측](../proxy/README.md#관측) 기준.
- 제품 메트릭은 내부에서 계측하며, 메트릭·상태를 조회하는 HTTP API는 제공하지 않음.

## 관측 경로

| 대상 | 응답·의미 | 판단 한계 |
| --- | --- | --- |
| Proxy 내부 `GET /healthcheck` | 요청 수락 준비 상태이면 200과 `{"ready":true}`, 미준비이면 503과 `{"ready":false}` | backend 정상·저장 성공·활성 revision의 증거 아님 |
| 더미 v1·v2의 `GET /__demo/requests` | `{version, items}` 형태의 버전·누적 item 요청 수 | 제품 내부 메트릭이나 요청 완료·성공 지표가 아님 |
| User 보고서 | 실제 HTTP 응답의 버전·상태·지연 | shadow·내부 queue를 직접 확인하지 않음 |
| SQLite 이벤트 | 정상 종료 후 저장된 단계별 비교 결과 | 모든 serving 요청의 접근 로그가 아님 |

- backend counter는 프로세스 생존 구간의 누적값. 시나리오는 backend를 유지하고 단계 전후 증가량을 사용.
- health·counter 조회 자체는 item 요청 수에 포함하지 않음.
- shadow OFF에서는 backend별 증가량과 User의 버전별 응답 수를 대조. ON에서는 양쪽 backend가 각각 요청 수만큼 호출되어야 함.
- 실제 성공·실패와 비교 결과는 User 응답·저장 이벤트로 함께 확인. counter만으로 성공을 판정하지 않음.
- 제품의 내부 queue·메트릭·drop counter는 이 데모 실행에서 외부로 조회하지 않으며 별도 제품 테스트 범위.

## 로그와 저장 이벤트

- CLI HTTP access log는 기본 비활성화. 요청별 URL·header·body를 별도 로그로 남기지 않음.
- 서버 시작·종료·오류 로그는 Uvicorn 표준 로그 사용.
- serving 결과는 내부 메트릭에 집계. shadow OFF 요청마다 SQLite 이벤트를 생성하는 구조가 아님.
- shadow 비교 이벤트는 유한한 수집 큐를 거쳐 SQLite에 저장. 정상 응답 쌍도 데이터·권한 문맥이 미확인이면 `not_comparable`로 기록.
- 시나리오는 User 완료 후 Proxy에 정상 종료를 요청하고 종료 상태·코드·OOM 여부·완료 로그를 확인한 뒤 저장소 조회.
- 해당 revision 이벤트 수·내용, 이전 단계 이벤트의 정확한 보존, 예상하지 않은 추가 이벤트 유무를 함께 판정.
- 상세 데이터는 기본 비활성화. 원문 body·인증 정보를 산출물이나 로그에 추가하지 않음.

## 접근 경계와 설정

- health bind는 Proxy 컨테이너의 loopback `127.0.0.1:9090`. 호스트·다른 컨테이너에 포트를 공개하지 않음.
- `API_PROXY_CONTROL_ENABLED=false`는 health listener 비활성화. 데모 Compose에서는 healthcheck를 위해 활성화.
- 실제 연결 peer가 loopback인지 확인. `Forwarded`·`X-Forwarded-For`·token으로 외부 접속을 허용하지 않음.
- `API_PROXY_CONTROL_TOKEN`이 비어 있지 않으면 loopback 요청에도 Bearer 인증 추가 적용. 컨테이너 내부 조회 명령은 환경변수에서 읽으며 값을 출력하지 않음.
- 비loopback bind는 token 유무와 무관하게 시작 전 거부.
- `/health/live`, `/health/ready`, `/metrics`, `/state`는 health listener에서 제공하지 않음.
- 설정 우선순위·기본값은 [환경변수와 CLI](proxy-cli.md) 참고.

## 사용자 요청과 분리

- 사용자 요청 listener의 `/healthcheck`, `/metrics`, `/state` 등은 일반 HTTP 경로로 기존 backend 전달 계약 유지.
- health를 위해 사용자 API 경로를 예약하거나 가로채지 않음.
- 두 listener는 동일 runtime의 시작·종료 수명을 공유. health용 두 번째 runtime 없음.
- 종료는 HTTP 연결 배출 후 runtime 내부 작업 배출 순서. 각 단계의 종료 예산을 고려해 Docker 정리 시간 설정.
- health 비활성화는 serving·shadow·비교·수집 정책을 바꾸지 않음.

## 관련 기능

- [순차 시나리오와 산출물](../validation/scenarios.md)
- [제품 내부 메트릭 계측](../../../docs/observability/metrics.md)
- [실행 구조와 종료](../../../docs/infrastructure/deployment-and-stack.md)
