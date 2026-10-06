# 실행·비교·수집 메트릭 계측

프록시는 사용자 응답, 각 backend의 실행, 비교 판정, 이벤트 수집을 서로 다른 지표로 계측한다. 각 지표를 해석할 때는 무엇을 한 건으로 세는지와 언제 처리가 끝난 것으로 판단하는지를 구분해야 한다. Serving, shadow, 논리 요청과 backend attempt의 의미는 [공통 용어](../_rules/glossary.md)를 따른다.

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 요청·backend·비교·적재 작업의 상태 변화에 따라, 등록된 label 값과 해당 구간의 측정 시계를 사용한다. |
| 결과·출력 | 프록시 내부에 누적 횟수인 counter, 관측값의 분포인 histogram, 현재 상태값인 gauge를 유지한다. 설정·배포 revision은 별도 메타데이터로 연결한다. |
| 실패·제한 | raw URL·query·사용자 ID·계속 증가하는 revision을 label로 사용해서는 안 된다. 측정 시작·종료 지점이나 단위가 다른 지연을 하나의 지표로 합쳐서는 안 된다. |

## 내부 계측과 노출 경계

프록시는 메모리의 counter·histogram·gauge를 갱신하고 요청·비교·수집의 단계별 분모를 유지해야 한다. 메트릭 snapshot과 `observation_status()`는 프로세스 내부의 계측·상태 확인 인터페이스이다. 관측 HTTP API나 메트릭 조회 CLI를 제공하는 기능이 아니다.

프록시는 `/metrics`·`/state` 관측 경로와 이를 활성화하는 설정을 제공하지 않는다. 외부 관측 서비스의 SDK나 exporter도 연결하지 않는다. 내부 계측·비교·이벤트 저장에는 이들 기능이 필요하지 않다.

Health는 전용 loopback listener의 `GET /healthcheck` 하나로 확인한다. 요청 수용 가능 상태이면 200과 `{"ready":true}`, 그렇지 않으면 503과 `{"ready":false}`만 반환한다. 다른 health 경로의 별칭이나 상세 응답은 제공하지 않는다.

Health listener의 바인드 주소와 실제 접속 peer는 모두 loopback이어야 한다. 인증 token을 외부 접근의 허용 근거로 사용해서는 안 된다. Health의 성공을 backend 호출이나 이벤트 저장의 성공으로 해석해서는 안 된다.

메트릭은 원문 body를 포함하지 않아도 내부 API 구조, 트래픽 규모와 장애 시점을 드러낼 수 있다. Label 값이 유한하다는 사실은 공개해도 안전하다는 뜻이 아니다. [Prometheus 보안 모델](https://prometheus.io/docs/operating/security/)도 계측 애플리케이션의 `/metrics`를 포함한 관측 endpoint의 공개 인터넷 노출을 경고한다.

현재 메트릭의 누적값과 분포는 프로세스 메모리에만 존재하며, 재시작하면 초기화된다. SQLite는 선택된 shadow 비교 이벤트를 저장한다. 이벤트의 개별 HTTP status와 지연 값은 전체 요청의 counter·histogram이 아니므로, 해당 기록만으로 재시작 전 전체 분모를 복원해서는 안 된다.

## 관측에서 답해야 할 질문

운영 판단에 사용하는 지표는 다음 질문에 각각 답할 수 있어야 한다. 이 목록은 확인 항목이며, 작업의 실행 순서를 뜻하지 않는다.

- 사용자 요청은 어느 backend에 배정됐으며, 실제 응답 출처와 사용자 전송의 종료 결과는 무엇인가?
- v1과 v2는 serving 또는 shadow 역할로 각각 몇 번 시작하고 종료했는가?
- 같은 논리 요청의 응답 중 비교 가능한 쌍은 몇 건이며, 어떤 차이가 발생했는가?
- 비교 또는 저장이 완료되지 않은 요청은 몇 건이며, 그 원인은 확인됐는가?
- Shadow·캡처·비교·저장 작업은 사용자 요청의 지연, 공유 자원과 비용에 어떤 영향을 주는가?
- 해당 관측값은 어떤 설정 revision, backend 배포와 전환 구간에 속하는가?

사용자·backend·비교 지표와 관측 시스템 자체의 상태를 분리해야 한다. 저장된 비교 이벤트만으로 프록시 전체 요청 수나 가용성을 계산해서는 안 된다.

장기 보존이나 복수 프로세스의 집계가 필요하면 별도의 연결과 검증이 필요하다. 현재 내부 snapshot을 제공한다는 사실만으로 이 기능이 연결된 것으로 해석해서는 안 된다. 어떤 방법을 사용해도 아래 지표의 의미와 데이터 보호 기준을 유지해야 한다. 프로세스별 수집 범위와 재시작 전후의 counter 경계를 표시해야 하며, 한 프로세스의 상태 조회를 전체 시스템 집계나 전환 준비 완료로 해석해서는 안 된다.

## 지표 목록

아래 이름은 애플리케이션에 등록된 자체 지표 이름이다. 외부 계측 체계에 연결할 때 이름을 매핑하더라도 측정 단위, label의 의미와 발생 시점을 보존해야 한다.

- **Counter**는 발생 횟수를 누적한다. 구간 증가량을 계산할 때는 프로세스 재시작으로 누적값이 초기화된 경계를 구분해야 한다.
- **Histogram**은 같은 단위와 bucket 경계로 측정한 값의 분포를 보관한다. 서로 다른 bucket 경계를 사용하는 분포를 그대로 합쳐서는 안 된다.
- **Gauge**는 조회 시점의 상태를 나타낸다. 값의 감소만으로 작업의 정상 완료를 판단해서는 안 된다.

| 이름 | 종류 | 차원과 측정 의미 |
| --- | --- | --- |
| `proxy_assignments_total` | counter | route·serving별 사용자 응답 backend 배정 수다. 배정 뒤 실패하거나 client가 취소한 요청도 포함한다. |
| `proxy_requests_total` | counter | route·serving·outcome·source별 사용자 요청 종료 수다. `source`는 `backend`·`proxy`·`none`을 구분하며, 배정 전 종료한 요청의 `serving`은 `unknown`이다. |
| `proxy_request_duration_seconds` | histogram | route·serving별로 요청 처리 시작부터 응답 전송 종료·실패 후 요청 측 작업 정리가 끝날 때까지 측정한 초 단위 지연이다. Serving 연결 정리 시간이 포함될 수 있다. |
| `backend_started_total` | counter | route·backend·role별 backend attempt 시작 수다. 논리 요청 수와 구분한다. |
| `backend_completed_total` | counter | route·backend·role·outcome·contract_class별 실행 종료 수다. HTTP 응답 여부와 해당 backend의 계약 분류를 함께 확인한다. |
| `backend_duration_seconds` | histogram | route·backend·role별 실행 시작부터 응답 수신 또는 실패 후의 연결 정리가 끝날 때까지 측정한 초 단위 지연이다. |
| `backend_inflight` | gauge | backend·role별로 현재 실행 중인 요청 수다. |
| `shadow_decisions_total` | counter | route·decision별 shadow 선택 판정 수다. `selected`는 표본 선택을 나타낸다. `sampled_out`은 적격이지만 중지·표본 비율로 선택하지 않은 상태다. `ineligible`은 E 조건 미충족을 나타낸다. |
| `shadow_not_dispatched_total` | counter | route·reason별로 표본 선택 이후 shadow 실행을 생략한 수다. |
| `comparison_pipeline_total` | counter | route·step별 처리 수다. `step`은 `eligible`·`selected`·`dispatched`·`terminal`·`comparable`·`stored`를 구분하며, 각각 [E·S·D·T·C·W](coverage-and-analysis.md)에 대응한다. |
| `comparison_results_total` | counter | route·result·comparison_class·reason별 최종 비교 판정 수다. |
| `comparison_duration_seconds` | histogram | route·phase별 초 단위 지연이다. `phase=queue`는 큐 대기, `phase=compute`는 비교 계산을 나타낸다. |
| `collection_queue_depth` | gauge | 저장 처리가 끝나지 않은 수집 이벤트 수다. 메모리 사용량을 확인할 때는 이벤트 개수와 바이트 수를 별도로 구분해야 한다. |
| `collection_oldest_age_seconds` | gauge | 저장 처리가 끝나지 않은 이벤트 중 가장 오래된 이벤트의 나이를 초 단위로 나타낸다. |
| `collection_dropped_total` | counter | 큐 포화·만료·저장 재시도 소진 등 `reason`별로 감지한 드롭 수다. |
| `collection_write_failures_total` | counter | 이벤트 배치 저장의 실패 수다. Backend 실행 실패 수와 구분한다. |
| `collection_expired_pending` | gauge | 보존 기한이 지났으나 삭제가 완료되지 않은 기록 수다. |

`outcome`·`reason`·`contract_class`·`comparison_class`·`step`에는 등록된 유한한 값만 사용해야 한다. Backend 하나의 `contract_class`와 응답 쌍의 `comparison_class`는 [결과 모델](../comparison/context-and-outcomes.md)에 따라 구분해야 한다. 예외 메시지를 그대로 label 값에 넣어서는 안 된다.

실제 HTTP status code는 유한한 범위에서 별도로 계측할 수 있다. 이 경우에도 사용 목적이 없는 모든 차원 조합을 생성해서는 안 된다. Serving 배정 횟수와 backend 응답 제공 성공 횟수는 같지 않다. 프록시가 생성한 gateway 오류나 응답 시작 전 취소를 v2 응답 제공 성공으로 집계해서는 안 된다. 또한 프록시가 응답 전송을 완료했다는 사실만으로 호출자가 응답을 소비했다고 판단해서는 안 된다.

### 표준 HTTP 계측과의 연결

[OpenTelemetry HTTP 메트릭 규약](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/)의 표준 이름과 위의 자체 지표 이름을 구분해야 한다. 표준 `http.server.request.duration`·`http.client.request.duration`은 초 단위 histogram이며, `http.route`에 raw URL을 넣어서는 안 된다.

표준 계측의 도입 여부와 버전은 미정이다. 도입하는 경우에는 선택한 instrumentation의 완료 시점과 본문 수신 경계를 확인한 뒤 자체 지표와의 대응 관계를 정해야 한다. 이름이 비슷하다는 이유만으로 같은 측정 구간으로 간주해서는 안 된다.

## 계측 위치와 수집 책임

| 대상 | 계측·확인 위치 | 저장 장애가 발생했을 때의 해석 |
| --- | --- | --- |
| 사용자·backend 실행, E·S·D·T | 프록시의 요청·실행 생명주기에서 갱신한다. | 비교 이벤트 저장 성공 여부와 독립적으로 유지해야 한다. |
| C·M·X와 비교 결과 | 비교 판정이 완료된 지점에서 갱신한다. | 요약·상세 표본이 저장되지 않아도 완료된 판정 수를 제거해서는 안 된다. |
| W·저장 실패·드롭 | 저장 확인 또는 유실 감지 지점에서 갱신한다. | 저장 확인 응답인 ACK의 미확인과 확정 드롭을 구분하고, 고유 event ID로 중복을 확인해야 한다. |
| 프로세스·계측 경로 가용성 | 배포 환경의 상태 점검과 메트릭 수집 상태를 확인한다. | 관측 누락을 오류 0건으로 해석해서는 안 된다. |
| DB·cache·외부 의존 자원 | 해당 서비스의 기존 계측에서 확인한다. | 프록시 HTTP 지표에서 추정한 값과 구분해야 한다. |

한 요청의 실행·비교 단계와 한 이벤트의 저장 확인은 각 해당 counter에 정확히 한 번 반영해야 한다. 재시도를 별개의 저장 성공으로 세어서는 안 된다. 프로세스 종료 전 수집되지 않은 counter나 유실된 저장 ACK까지 무손실로 보장하는 것은 아니다. W의 재확인과 구간 마감에는 [비교 분모 기준](coverage-and-analysis.md)을 적용해야 한다.

### 여러 프로세스의 집계

여러 프로세스의 값을 함께 해석하려면 프로세스별 수집 후 집계하거나, 해당 구성을 지원하는 다중 프로세스 수집 방식을 선택해야 한다. 이 요구사항이 외부 수집기 도입을 뜻하지는 않는다. Worker 수를 미리 고정한 것으로 가정해서는 안 된다. Snapshot은 특정 시점의 지표 묶음이다.

집계 검증 담당자는 다음 순서로 집계 범위와 결과를 확인하십시오.

1. 수집 대상 worker와 각 프로세스의 실행 세대를 식별하십시오.
2. 대상 worker의 snapshot이 모두 수집됐는지 확인하십시오.
3. 같은 worker의 snapshot을 중복 반영했는지 확인하십시오.
4. Counter와 histogram의 합산 결과를 확인하십시오.
5. 현재 부하를 나타내는 gauge의 집계 결과를 확인하십시오.
6. Worker 누락·종료·재시작과 미수집 counter의 영향을 기록하십시오.

각 단계의 판정에는 다음 규칙을 적용해야 한다.

| 확인 대상 | 판정 규칙 |
| --- | --- |
| 프로세스 실행 세대 | 재시작 전후의 프로세스를 같은 누적 counter로 취급해서는 안 된다. |
| 수집 범위 | 일부 worker의 snapshot만 확보한 결과를 전체 수집 완료로 판단해서는 안 된다. |
| Counter·histogram | 수집 가능한 counter와 호환되는 histogram을 합산해야 한다. 미수집 구간이나 histogram 경계 불일치가 있으면 완전한 집계가 아니다. |
| Queue depth·inflight | 살아 있는 worker의 값을 합산해야 한다. 종료된 worker의 잔존 gauge는 현재 부하에서 제외해야 한다. |
| Oldest age | 살아 있는 worker의 값 중 최대값을 사용해야 한다. |
| Worker 종료 | 종료 때문에 gauge가 사라진 경우를 작업 정상 완료로 해석해서는 안 된다. |

[Prometheus Python 다중 프로세스 모드](https://prometheus.github.io/client_python/multiprocess/) 같은 외부 계측 방식을 검토할 때에는 registry 중복 등록, 종료 worker의 잔존 gauge, 재시작 시 수집 파일 정리와 지원 기능 제한을 확인해야 한다. 현재 제품은 이 모드나 Prometheus HTTP 수집 연동을 제공하지 않는다.

## 차원과 고유값 수

아래는 계측 차원을 선정할 때의 허용 기준이다. 모든 차원이 현재의 모든 지표에 label로 등록됐다는 뜻은 아니다.

| 사용할 수 있는 제한 차원 | 메트릭 label에서 제외해야 할 값 |
| --- | --- |
| 등록된 route ID, v1/v2, serving/shadow, 환경, 제한된 결과·이유 코드 | raw URL·query·body, 사용자·테넌트 ID, trace·event ID, 계속 증가하는 revision |
| 사전에 정한 요청·응답 크기 구간 | 실제 파일명·문서 ID·개별 입력값 |

설정·backend 배포 revision과 상세 cohort·데이터 문맥은 이벤트 또는 배포 메타데이터로 연결해야 한다. 설정 변경 시점과 혼합 revision 구간은 허용된 필드만 담은 annotation 또는 로그로 표시해야 한다. 테넌트 규모별 분석이 필요한 경우에는 개별 테넌트 ID 대신 사전에 정의한 규모 구간을 사용해야 한다.

메트릭 수집을 위해 원문 body·header·cookie·query·raw URL이나 자유 형식의 예외 메시지를 로그로 남겨서는 안 된다. 내부 공급자가 실패한 경우에도 고정된 진단 코드만 기록해야 한다. 표준 출력과 표준 오류에도 같은 보호 기준을 적용해야 한다. [Docker의 로그 처리](https://docs.docker.com/engine/logging/)처럼 배포 환경의 로그 드라이버가 출력을 외부로 전송할 수 있기 때문이다.

[OpenTelemetry의 민감 데이터 처리 지침](https://opentelemetry.io/docs/security/handling-sensitive-data/)에 따라 관측에 필요한 정보만 수집하고, 계측 라이브러리가 추가로 내보내는 데이터도 확인해야 한다.

## 지연의 측정 경계

| 구간 | 시작과 종료 | 해석할 때의 제한 |
| --- | --- | --- |
| 호출자 전체 지연 | 호출자가 요청을 보낸 시점부터 응답을 사용할 수 있는 시점까지다. | 프록시만으로 전체 구간을 관측할 수 없다. |
| 프록시 사용자 요청 지연 | 요청 처리 시작부터 응답 전송 종료·실패 후 요청 측 작업 정리까지다. | 느린 client의 입력 수신·응답 전송과 serving 연결 정리 시간이 포함될 수 있다. 순수 응답 전송 완료 시간과 구분해야 한다. |
| backend 왕복 지연 | 실행 시작부터 body 수신 또는 실패 후 연결 정리가 끝날 때까지다. | 연결 정리 시간이 포함되므로 body 수신 시간이나 API 내부 처리 시간과 같지 않다. |
| 첫 응답 지연 | 실행 시작부터 첫 headers 또는 첫 byte 수신까지다. | 어떤 지점을 사용하는지 표시하고, 전체 body 완료 지연과 분리해야 한다. |
| 비교 큐 대기·계산 | 큐 제출부터 처리 시작까지와 비교 시작부터 비교 종료까지를 각각 측정한다. | 사용자 응답이 완료된 뒤에도 진행할 수 있다. |
| 저장 지연 | 이벤트 생성부터 저장 확인까지다. | 요청 시작 구간을 마감할 때 이 지연을 반영해야 한다. |

사용자 응답을 거절하는 경로는 거절 처리 시작부터 오류 응답 전송이 끝나거나 실패할 때까지 측정한다. 별도로 남기는 사용자 전송 종료 시각과 지연 histogram의 측정 종료 시점이 같다고 가정해서는 안 된다.

지속 시간은 단조 시계로 측정해야 하며, 이벤트 사이의 시각을 연결하는 벽시계 시각과 구분해야 한다. 요청별 v1/v2 지연 차이는 같은 논리 요청의 완전한 응답 쌍으로 계산해야 한다. 이때 실패·미완료 요청이 제외되어 빠른 성공 쌍만 남는 편향을 함께 보고해야 한다.

p95와 p99는 지연 분포의 95번째·99번째 백분위수다. `p99(v2) - p99(v1)`은 두 분포의 p99 차이다. `p99(v2 - v1)`은 요청별 지연 차이 분포의 p99다. 두 값을 같은 지표로 해석해서는 안 된다.

여러 인스턴스의 p95/p99를 평균해서는 안 된다. 호환되는 histogram bucket을 합친 분포에서 해당 백분위수를 계산해야 한다.

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-27 | P0 | 사용자·backend·비교·수집 지표를 분리해야 한다. | Serving/shadow 역할과 v1/v2 backend별 실제 실행·오류·지연을 각각 확인할 수 있어야 한다. |

## 검증 기준

[T-29, T-30, T-37, T-45, T-47](../validation/test-catalog.md)을 적용한다. 각 결과에는 측정 시작·종료 지점, 프로세스 집계 범위와 재시작 경계를 함께 기록해야 한다.
