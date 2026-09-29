# Proxy 전환과 shadow

- 실행 대상: 제품 Proxy 패키지. 데모는 합성 endpoint·설정·요청을 제공하는 역할.
- 기본 설정: [config/docker.json](../../config/docker.json)의 `synthetic_item` route, `GET /items/{id}`.
- 기본 동작: v1 응답 100% 반환, 안전한 합성 읽기 요청의 v2 shadow 표본 비율 100%.

## 독립 설정

| `snapshot.routes[0]` 설정 | 기본값 | 역할·변경 예 |
| --- | --- | --- |
| `rollout_enabled` | `true` | 전환 활성화. `false`이면 v1만 사용하며 shadow도 실행하지 않음 |
| `v2_serve_ratio` | `0` | v2 응답 배정 비율. `0`: v1 100%, `0.5`: v1·v2 각각 약 50%, `1`: v2 100% |
| `cohort.mode` | `request` | 요청마다 배정. 같은 사용자도 요청마다 다른 버전을 받을 수 있음 |
| `shadow.sample_ratio` | `1` | 반대편 요청의 표본 비율. `0.1`: 약 10%, `1`: 100%, `0`: 비활성 |
| `shadow.stopped` | `false` | `true`: 신규 shadow 중지, `false`: 표본 비율에 따라 실행 |

- serving: 사용자에게 반환할 응답 선택. 서로 다른 [v1·v2 본문](../backends/README.md)을 선택한 버전 그대로 전달하며, 공통 형식으로 변환하거나 양쪽 응답을 합치지 않음.
- shadow: 부가 실행. shadow 비율을 변경해도 serving 비율은 그대로 유지.
- shadow 표본 비율은 선택 기준이며, 동시 실행 한도·요청 크기·취소 등에 따라 실제 실행 생략 가능.
- serving 비율은 유한한 요청 표본에서 설정값과 다를 수 있음. 사용자 고정 A/B 배정과 구분.
- User의 응답 버전 집계와 backend 전체 호출 횟수는 서로 다른 지표.

## 수동 전환 절차

1. 기본 설정으로 서비스 실행 및 v1 응답 확인.

   ```sh
   make demo-up
   make demo-request REPORT=results/v1.json
   ```

2. `snapshot.routes[0].v2_serve_ratio`를 `0.5`로 변경.
3. `snapshot.previous_revision`에 이전 revision 기록, `snapshot.revision`과 `snapshot.change_reason` 갱신.
4. 설정 검사·Proxy 재생성 후 혼합 응답 확인.

   ```sh
   make demo-reload
   DEMO_REQUESTS=200 make demo-request REPORT=results/mixed.json
   ```

5. `v2_serve_ratio=1`과 새 revision·변경 사유 적용 후 전체 v2 확인.

   ```sh
   make demo-reload
   make demo-request REPORT=results/v2.json
   make demo-smoke SERVING_BACKEND=v2
   ```

6. `v2_serve_ratio=0`과 새 revision·변경 사유 적용 후 v1 복귀 확인.

   ```sh
   make demo-reload
   make demo-request REPORT=results/rollback.json
   make demo-smoke SERVING_BACKEND=v1
   ```

- 각 변경에서 `previous_revision`도 직전 값으로 갱신.
- `demo-reload`: 설정 검사 성공 후 Proxy만 재생성. 설정 파일 편집이나 단순 restart만으로 적용 여부 판단 금지.
- `SERVING_BACKEND`: smoke의 기대값. 실제 serving 설정을 변경하는 옵션이 아님.
- smoke 조건: serving 0 또는 1, shadow 100% 활성. 중간 비율은 User 또는 [자동 분배 검증](../validation/distribution.md)으로 확인.
- 무중단 전환·운영 변경 이력의 내구성을 보장하는 배포 절차가 아님.

## shadow 중지·재개

- 중지: `shadow.stopped=true` 또는 `shadow.sample_ratio=0` 적용, revision 갱신 후 `make demo-reload`.
- 재개: 합성 route의 `eligible=true`·`review_ref`를 유지하면서 `stopped=false`와 양수 `sample_ratio`를 모두 적용.
- User 보고서만으로 shadow 실행 여부 판단 불가. [내부 이벤트 smoke](../validation/README.md)와 구분.

## 별도 설정 사용

```sh
cp .demo/config/docker.json .demo/config/custom.json
PROXY_CONFIG=./config/custom.json make demo-up
PROXY_CONFIG=./config/custom.json make demo-reload
```

- `PROXY_CONFIG`의 상대 경로는 `.demo` 기준. 모든 관련 Compose·Make 명령에 같은 값 전달.
- 자동 분배 검증은 원본 설정을 복사해 별도 환경에서 실행하므로 위 수동 전환 환경을 변경하지 않음.

## 비교·관측 범위

- 일반 Proxy CLI: 데이터·권한 문맥을 자동 신뢰하지 않아 정상 응답 쌍은 `not_comparable`, 예상 밖 500은 `execution_error`.
- 테스트 전용 합성 문맥 주입 시: 기본 비교 정책에서는 v1 평면 본문과 v2 공통 응답 구조의 차이를 `different`로 판정. `/items/same`도 전체 JSON은 다름.
- 별도 필드 매핑 테스트: 비교할 데이터 위치를 맞추면 `same`은 `matched`, `different`는 `different` 확인. 이 테스트 정책은 데모 실행 설정·serving 응답에 적용하지 않음.
- `create_control_app()`: 접근 검증을 요구하는 별도 관리 앱. 일반 CLI 공개 포트에 메트릭·상태 endpoint가 자동 연결되지 않음.
- 사용자 응답 집계, 내부 이벤트 저장, 비교 품질을 각각 구분해 검증.
