# 비교 문맥과 결과 판정

- 양쪽 실행의 비교 가능 조건 확인
- 실행 오류·계약 차이·비교 한계 구분

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 양쪽 backend 결과, 성공·예상 거절·업무 오류 계약, 권한·데이터 문맥 |
| 결과·출력 | result·reason·contract class와 제한된 차이 정보 |
| 실패·제한 | 예상 밖 API 오류·정보 부족의 정상 일치 처리 금지, 동일 응답만으로 권한의 절대적 정확성 증명 불가 |

## 비교의 목적과 한계

- 비교 목적: 같은 논리 입력에 대한 v1/v2의 등록 API 계약 충족 여부 조사
- 증명 범위: 관측된 요청과 등록 비교 정책으로 한정
- 동일 응답만으로 전체 데이터 정합성·권한 정확성·사용자 행동 개선·운영 가용성 보장 불가
- 비교 정책과 저장 정책의 분리: 개인정보 저장 제외를 업무 필드 비교 제외로 자동 적용 금지
- 민감 필드: 허용 범위의 메모리 비교와 값 저장 금지 계약을 별도 정의

## 입력과 문맥

| 항목 | 확인할 내용 | 부족할 때 |
| --- | --- | --- |
| 논리 요청 | 같은 method·경로 의미·query·body인가 | 입력 불일치 또는 실행 오류로 조사 |
| 사용자·권한 | 같은 사용자·테넌트·역할·권한 범위인가 | 정상 일치 판정 불가; 권한 차이는 별도 중요 오류 |
| 데이터 | 같은 source 또는 비교 가능한 snapshot·freshness인가 | `not_comparable` 또는 명시된 제한 문맥 아래 비교 |
| 시간·외부 의존 | now·random·외부 시세·모델 결과가 개입하는가 | 고정 입력·허용 오차·비교 제외 가능성을 정책으로 결정 |
| 공개 계약 | v1/v2가 같은 의미의 성공·거절을 표현하는가 | 매핑 또는 호환 계약 정의 전 전환 불가 |
| 수집 완전성 | 양쪽 body가 온전히 수신·캡처되었는가 | `not_comparable`, 부분 데이터로 정상 판정하지 않음 |

- route별 허용 문맥 수준과 데이터 시차 범위 명시
- 근사 비교 허용 시 해당 한계와 문맥을 함께 기록
- 허용 시차 초과·필수 문맥 부재 시 `not_comparable` 판정
- 차이 원인을 확인하지 않은 채 데이터 문제로 일괄 제외 금지
- backend 배포·데이터 revision 변경 시 기존 비교 구간과 분리; 확인 불가 값은 `unknown`으로 유지

## backend 결과 모델

- backend 버전과 역할을 각각 저장
- v2 serving 전환 시 v1이 shadow 역할 수행

| 필드 | 값 또는 의미 |
| --- | --- |
| `backend` | `v1`, `v2` |
| `deployment_revision` | 실제 실행 대상의 확인 가능한 배포 버전; 확인 불가 시 `unknown` |
| `role` | `serving`, `shadow` |
| `execution_outcome` | `http_response`, `transport_error`, `timeout`, `cancelled`, `not_dispatched` |
| `contract_class` | `success`, `expected_rejection`, `unexpected_error`, `unknown` |
| `status_code` | 응답 headers 수신 시 코드, 미수신 시 null; body 수신 실패 시에도 수신 코드는 보존 |
| `duration_ms` | 실행 시작부터 완료·실패 확정까지의 시간, 미실행이면 null |
| `response_bytes` | 수신한 본문 바이트 수와 완전 수신 여부 |
| `capture_state` | `complete`, `oversized`, `unavailable`, `not_needed` |
| `reason` | 유한한 이유 코드; credential이 섞인 원문 예외 메시지 제외 |

- HTTP 200 내부의 업무 실패: route 계약에 따라 `unexpected_error` 분류
- 잘못된 입력의 400·권한 부족의 403: 등록 계약상 예상 동작인 경우에만 `expected_rejection` 분류
- status code만으로 예상 거절 자동 확정 금지
- body 수신 도중 연결 종료: `transport_error`와 불완전 수신 상태 기록; 완전 응답으로 처리 금지
- 필수 body·정책 정보 부족으로 계약 판정 불가: `contract_class=unknown` 유지

## 비교 결과 모델

- 최종 비교 이벤트: `result` 1개, 주된 `reason`, `comparison_class`, 양쪽 backend 결과로 구성
- 여러 문제가 함께 발생한 경우 아래 우선순위로 주원인 선택; 개별 backend 결과는 별도 보존

| 우선순위 | `result` | 의미 |
| --- | --- | --- |
| 1 | `not_executed` | 선택된 shadow를 전송하지 못함. 과부하·본문 크기·종료 등의 원인 |
| 2 | `execution_error` | 한쪽 이상 timeout·취소·전송 오류·예상 밖 API 오류 |
| 3 | `not_comparable` | 양쪽 응답은 있으나 문맥·캡처·파싱·정책 한계 또는 계약 class 미확인으로 판정 불가 |
| 4 | `different` | 비교 가능한 응답에서 status·계약 class·필수 헤더·JSON 의미가 다름 |
| 5 | `matched` | 비교 가능한 두 성공 응답 또는 두 예상 거절 응답이 등록 정책에서 일치 |

| `comparison_class` | 적용 조건 |
| --- | --- |
| `success` | 양쪽 모두 `success`이며 비교 가능한 쌍 |
| `expected_rejection` | 양쪽 모두 `expected_rejection`이며 비교 가능한 쌍 |
| `mixed` | 한쪽 성공·반대편 예상 거절이며 비교 가능한 쌍 |
| `unavailable` | 비실행·실행 오류·비교 불가로 위 세 class에 속하지 않는 쌍 |

- `matched`의 성공 쌍·예상 거절 쌍 분리 집계
- 같은 예상 밖 500 두 개: `execution_error`, `comparison_class=unavailable`
- 양쪽 `contract_class=unknown`: 같다는 이유로 `matched` 처리 금지
- 성공·예상 거절 혼합 쌍: `different`, `comparison_class=mixed`; 각 backend class 보존
- 양쪽 실행 오류 메트릭은 최종 비교 결과와 독립 유지
- shadow 표본 제외 요청: 비교 이벤트 생성 불필요, 선택 메트릭에 기록
- 비교 큐 제출 실패: 비교 이벤트 유실 가능, `collection_dropped_total`로 감지된 드롭 보고
- 저장되지 않은 이벤트의 가짜 `matched`·`not_executed` 행 생성 금지

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-17 | P0 | route별 성공·예상 거절·예상 밖 오류 계약 정의 | 같은 HTTP 500을 정상 일치로 집계하지 않고, 예상 4xx는 별도 분류 |
| FR-20 | P0 | 비교 불가·실행 실패·비실행을 구분 | timeout·큰 본문·문맥 부족·드롭이 일치 결과로 처리되지 않음 |

## 검증 계획

- 구현 후 검증: [T-16, T-17, T-21, T-22](../validation/test-catalog.md)

## 관련 기능

- [정규화와 차이 비교](normalization.md)
- [이벤트 모델](../collection/event-model.md)
- [비교 분모](../observability/coverage-and-analysis.md)
