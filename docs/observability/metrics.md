# 실행·비교·수집 메트릭 계측

- 사용자 응답·양쪽 backend 실행·비교·저장 작업의 독립 계측

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 요청·backend·비교·적재의 상태 변화, 제한된 label과 측정 시계 |
| 결과·출력 | counter·histogram·gauge 및 revision과 연결할 배포 메타데이터 |
| 실패·제한 | raw URL·query·사용자 ID·무한 revision의 label 사용 금지, 측정 구간·단위가 다른 지연의 혼합 금지 |

## 관측에서 답해야 할 질문

1. 사용자 요청의 backend 배정·실제 응답 출처·오류·지연 확인
2. v1/v2 실제 시작·종료 요청 수 확인
3. 동일 요청 응답의 차이율·차이 유형 확인
4. 비교·저장 누락 요청 수와 원인 확인
5. shadow·캡처·비교·저장의 사용자 경로 영향과 비용 확인
6. 설정 revision·backend 배포·전환 구간별 관측 조건 확인

- 사용자·backend 실행·비교·관측 시스템 자체의 지표 분리
- 저장 이벤트만으로 프록시 전체 가용성 계산 금지

## 지표 목록

- 아래 이름: 자체 지표 제안, 구현 시 기존 계측 체계의 이름 규칙에 맞춰 매핑
- counter: 누적 발생 수와 구간 증가량
- histogram: 같은 bucket·단위의 관측 분포
- gauge: 현재 상태

| 이름 | 종류 | 주요 차원·의미 |
| --- | --- | --- |
| `proxy_assignments_total` | counter | route·serving별 응답 backend 배정 수; 배정 이후 실패·client 취소도 분모 유지 |
| `proxy_requests_total` | counter | route·serving·outcome별 사용자 요청 종료; 응답 출처 `backend`·`proxy`·`none` 구분, 배정 전 실패의 serving은 `unknown` |
| `proxy_request_duration_seconds` | histogram | route·serving별 요청 수신부터 응답 전송 종료·실패까지 |
| `backend_started_total` | counter | route·backend·role별 실제 backend attempt 시작 |
| `backend_completed_total` | counter | route·backend·role·outcome별 실행 종료; HTTP 상태·backend `contract_class`도 별도 확인 |
| `backend_duration_seconds` | histogram | backend 실행 시작부터 완전 응답 또는 실패 확정까지 |
| `backend_inflight` | gauge | backend·role별 현재 실행 중인 요청 수 |
| `shadow_decisions_total` | counter | route·decision별 selected, sampled_out, ineligible |
| `shadow_not_dispatched_total` | counter | route·reason별 선택 후 실행 생략 |
| `comparison_pipeline_total` | counter | route·step별 eligible, selected, dispatched, terminal, comparable, stored |
| `comparison_results_total` | counter | route·result·comparison_class·reason별 최종 비교 결과 |
| `comparison_duration_seconds` | histogram | 큐 대기와 비교 계산 시간을 구분한 분포 |
| `collection_queue_depth` | gauge | 수집 대기 이벤트 개수와 필요 시 메모리 사용량 |
| `collection_oldest_age_seconds` | gauge | 처리되지 않은 가장 오래된 이벤트의 나이 |
| `collection_dropped_total` | counter | 큐 포화·만료·저장 재시도 소진 등 감지된 드롭 |
| `collection_write_failures_total` | counter | 배치 저장 실패; backend 실패와 구분 |
| `collection_expired_pending` | gauge | 보존 기한이 지났으나 정리되지 않은 기록량 |

- `outcome`·`reason`·`contract_class`·`comparison_class`·`step`: 제한된 enum으로 고정
- backend `contract_class`와 비교 쌍의 `comparison_class` 구분: [결과 모델](../comparison/context-and-outcomes.md) 기준
- 동적인 예외 메시지의 label 사용 금지
- 실제 HTTP status code: 유한 범위에서 별도 계측 가능, 불필요한 모든 차원 조합 생성 금지
- serving 배정과 backend 응답 전달 구분: 프록시 생성 gateway 오류·응답 시작 전 취소를 v2 응답 제공 성공으로 집계 금지
- 응답 전송 완료: 프록시 관측 범위의 완료, 호출자가 응답을 소비했다는 증명으로 해석 금지

- HTTP 계측: [OpenTelemetry HTTP 메트릭 규약](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/)에 따른 표준 이름과 자체 지표 이름 구분
- 표준 `http.server.request.duration`·`http.client.request.duration`: 초 단위 histogram; `http.route`에 raw URL 대입 금지
- 도입 여부·버전은 스택 결정 후 확정; 선택한 instrumentation의 완료 시점·본문 수신 경계 검증 후 자체 지표와 매핑

## 계측 위치와 수집 책임

| 대상 | 계측·확인 위치 | 저장 장애 시 동작 |
| --- | --- | --- |
| 사용자·backend 실행, E·S·D·T | 프록시 요청 및 실행 생명주기 | 비교 저장 성공 여부와 독립 유지 |
| C·M·X와 비교 결과 | 비교 판정 완료 지점 | 상세 표본·요약 저장 여부와 독립 유지 |
| W·저장 실패·드롭 | 저장 확인·유실 감지 지점 | ACK 미확인과 확정 드롭 분리, 고유 event ID 중복 확인 |
| 프로세스·계측 경로 가용성 | 배포 환경의 상태 점검과 메트릭 수집 상태 | 관측 누락을 오류 0으로 해석 금지 |
| DB·cache·외부 의존 자원 | 해당 서비스의 기존 계측 | 프록시 HTTP 지표에서 추정한 값과 구분 |

- 실행·비교·적재 counter의 단계별 정확히 한 번 갱신 필요
- 프로세스 종료 전 미수집 counter·저장 ACK 유실의 무손실 보장 불가
- W 재시도·집계 시각·구간 마감 경계: [비교 분모](coverage-and-analysis.md) 기준 적용

### 여러 프로세스의 집계

- 배치에 맞춘 프로세스별 수집 후 집계 또는 지원되는 다중 프로세스 수집 선택; worker 수의 선행 고정 제외
- 동일 수집 주소에 임의 worker가 응답하는 구성을 전체 집계로 간주 금지; counter·histogram의 누락·중복 수집 검증
- 프로세스별 queue depth·inflight: 살아 있는 worker의 합계, oldest age: 살아 있는 worker의 최대값으로 집계
- [Prometheus Python 다중 프로세스 모드](https://prometheus.github.io/client_python/multiprocess/) 선택 시: registry 중복 등록, 종료 worker의 잔존 gauge, 재시작 시 수집 파일 정리와 지원 기능 제한 확인
- worker 종료로 사라진 gauge를 정상 처리 완료로 해석 금지; 수집 유실·완전성 미확인과 함께 보고

## 차원과 고유값 수

| 사용 가능한 제한 차원 | 메트릭 label에서 제외할 값 |
| --- | --- |
| 등록된 route ID, v1/v2, serving/shadow, 환경, 제한된 결과·이유 코드 | raw URL·query·body, 사용자·테넌트 ID, trace·event ID, 계속 증가하는 revision |
| 미리 정한 요청·응답 크기 구간 | 실제 파일명·문서 ID·개별 입력값 |

- 설정·backend 배포 revision, 상세 cohort·데이터 문맥: 이벤트·배포 메타데이터로 연결
- 설정 변경·혼합 revision 구간: annotation 또는 로그로 표시
- 전체 테넌트 ID 대신 필요한 경우 사전 정의한 규모 구간 사용

## 지연의 측정 경계

| 구간 | 정의 | 주의 |
| --- | --- | --- |
| 호출자 전체 지연 | 호출자가 보낸 시점부터 응답 사용 가능 시점까지 | 프록시 단독 계측으로 전부 알 수 없음 |
| 프록시 사용자 요청 지연 | 프록시 수신부터 응답 전송 종료 또는 실패까지 | 느린 client의 수신·전송 영향 포함 가능 |
| backend 왕복 지연 | 실행 시작부터 body 수신 완료 또는 실패까지 | API 내부 처리 시간과 같지 않음 |
| 첫 응답 지연 | 실행 시작부터 첫 headers·byte까지 | 전체 body 완료 시간과 분리 |
| 비교 큐 대기·계산 | 큐 제출부터 처리 시작 / 비교 시작부터 종료 | 사용자 응답 완료 후에도 진행 가능 |
| 저장 지연 | 이벤트 생성부터 저장 확인까지 | 완료 구간 집계 시 지연 반영 필요 |

- 지속 시간: 단조 시계 기반 측정, 이벤트 상관관계용 벽시계 시각과 분리
- 양쪽 지연의 요청별 차이: 같은 요청의 두 완전 응답 사용, 빠른 성공 쌍만 남는 편향 동시 보고
- `p99(v2) - p99(v1)`과 `p99(v2 - v1)`의 구분
- 인스턴스별 p95/p99 평균 금지, 호환 histogram bucket을 합친 분포에서 계산

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-27 | P0 | 사용자·backend·비교·수집 지표 분리 | serving/shadow와 v1/v2별 실제 실행·오류·지연을 확인 가능 |

## 검증 계획

- 구현 후 검증: [T-29, T-30, T-37, T-45](../validation/test-catalog.md)
