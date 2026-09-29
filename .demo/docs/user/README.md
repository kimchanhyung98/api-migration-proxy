# User 호출과 집계

- 역할: Proxy에 순차 GET 요청을 보내 사용자에게 반환된 응답의 버전·상태·지연 집계.
- 구현: [User CLI](../../user/cli.py). DB·Proxy 내부 관리 API 접근 없음.
- 실행 형태: 일회성 컨테이너. [Docker 실행](../runtime/README.md)으로 v1·v2·Proxy를 먼저 시작한 뒤 호출.

## 호출 방법

```sh
make demo-request
DEMO_REQUESTS=200 make demo-request REPORT=results/mixed.json
DEMO_REQUESTS=10 DEMO_PATH=/items/missing make demo-request REPORT=results/404.json
DEMO_REQUESTS=10 DEMO_PATH=/items/error make demo-request REPORT=results/500.json
```

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| `DEMO_REQUESTS` | `100` | 순차 요청 횟수, 양의 정수 |
| `DEMO_PATH` | `/items/different` | Proxy에 요청할 상대 경로 |
| `DEMO_TIMEOUT` | `5` | HTTP 클라이언트 timeout, 초 단위 양의 유한값 |
| `REPORT` | `results/user.json` | `.demo` 기준 출력 파일 경로 |

- Compose의 User 대상: `http://proxy:8080`. 호스트 공개 포트를 바꿔도 동일.
- 결과: JSON을 화면과 `REPORT` 파일에 출력. 같은 경로를 다시 사용하면 덮어쓰기.
- `make demo-request`: User 이미지 빌드·실행만 수행. 기반 서비스 시작·설정 적용은 별도.
- CLI 직접 사용 옵션: `--url`, `--path`, `--requests`, `--timeout`, `--output`.
- `--url`: 인증 정보·query·fragment·하위 경로가 없는 HTTP(S) origin만 허용.
- `--path`: `/`로 시작하는 origin 내부 경로. 외부 URL·`//`·fragment·공백/제어 문자 제외.
- redirect 추적과 환경변수 기반 HTTP proxy 사용 비활성화.

## 결과 해석

| 필드 | 의미 |
| --- | --- |
| `attempts` | 전체 요청 시도 수 |
| `responses` | 완전한 HTTP 응답 수. 4xx·5xx도 포함 |
| `transport_errors` | 연결 실패·timeout 등 응답을 완전히 받지 못한 수 |
| `status_counts` | HTTP 상태 코드별 응답 수 |
| `status_classes` | 2xx·4xx·5xx 등 class별 응답 수 |
| `backend_versions.counts` | `x-backend-version`을 기준으로 한 v1·v2·unknown 응답 수 |
| `backend_versions.ratios` | `responses`를 분모로 한 비율. unknown 포함, 응답 0개면 `null` |
| `latency_ms` | 실패한 시도를 포함한 전체 요청 지연의 건수·최소·최대·평균 |
| `elapsed_seconds` | 전체 요청 실행에 걸린 시간 |

- 헤더 누락 또는 v1·v2 이외 값: `unknown` 집계.
- v1·v2의 본문 구조 차이와 무관하게 버전 헤더로 집계. 본문이 같도록 요구하거나 User에서 응답 형식을 변환하지 않음.
- 네트워크 실패: v1·v2 어느 쪽으로도 추정하지 않으며 응답 비율 분모에서 제외.
- 404·500: 응답을 받은 HTTP 오류로 집계. 네트워크 실패와 구분.
- v2 본문의 `code`·`message`는 집계 기준이 아님. HTTP 상태와 버전 헤더를 사용하며, 본문 계약은 [합성 API·smoke 테스트](../validation/README.md)에서 검증.
- 저장 제외: URL·query·응답 본문·개별 헤더·예외 원문·인증 정보.
- 종료 코드 0: 보고서 생성 성공 의미. 요청 성공률·분배 통과를 보장하지 않으므로 [응답 분배 검증](../validation/distribution.md)으로 별도 판정.

## 측정 범위

- 사용자에게 반환된 serving 응답 비율만 측정. shadow 요청 수나 내부 비교 결과는 확인하지 않음.
- 유한한 요청 표본의 비율이므로 설정값과 정확히 일치하지 않을 수 있음.
- 순차 호출 방식. 동시 부하·목표 QPS·운영 성능 측정 도구가 아님.
