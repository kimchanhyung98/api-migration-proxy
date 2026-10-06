# 공통 품질 요구사항과 기능 추적

기능 요구사항은 FR, 여러 기능에 공통으로 적용되는 품질 요구사항은 NFR로 식별한다. FR의 동작과 수용 조건은 아래 표에 연결된 담당 기능 문서에서 정의한다. 이 문서의 시나리오 연결은 어떤 계약을 검증하는지 나타내며, 해당 검증이 이미 통과했다는 뜻은 아니다.

요구사항의 우선순위는 다음 의미로 사용한다.

- **P0**는 해당 기능을 사용하는 첫 운영 단계 전에 검증해야 하는 필수 요구사항이다.
- **P1**은 실제 서비스에서 필요가 확인된 경우 추가하는 요구사항이다. 필요가 확인되지 않은 기능을 모두 필수 범위로 확대해서는 안 된다.

## 품질 요구사항과 미정 수치

아래 목표값은 대상 API의 기준선, 대표 부하, 허용 지연과 오류 예산에 따라 결정해야 한다. 이 문서는 공통 숫자를 제품 성능 보장으로 제시하지 않는다. 요구사항을 채택했더라도 목표 또는 측정값이 없으면 충족 여부는 `unknown`이다.

| ID | 요구사항 | 대상 서비스에서 결정해야 할 값·범위 | 검증 기준 |
| --- | --- | --- | --- |
| NFR-01 | 사용자 응답 성능 | Route별 p95/p99 목표와 프록시 추가 지연 허용치를 결정해야 한다. | 동일한 대표 부하에서 v1 직접 호출, 프록시 v1 통과와 shadow 실행의 사용자 지연을 비교해야 한다. |
| NFR-02 | 가용성 | Serving 오류 예산과 복귀 목표 시간을 결정해야 한다. | Backend·저장소·프록시 장애를 각각 주입하여 사용자 영향과 복귀 시간을 측정해야 한다. |
| NFR-03 | 처리 용량 | 평시·피크 QPS, 동시 요청 수와 본문 크기 분포를 정해야 한다. | 정상·피크·긴 응답·cold start 조건에서 실제 도착률, 완료율과 자원 상한 준수 여부를 확인해야 한다. |
| NFR-04 | 수집 완전성 | 허용 이벤트 유실과 저장 지연을 결정해야 한다. | 큐·저장 실패와 재시작 조건에서 메트릭·이벤트 건수의 차이, 확정 유실과 ACK 미확인을 구간별로 보고해야 한다. |
| NFR-05 | 메모리 상한 | 요청 복제·응답 캡처·큐·비교 CPU의 자원 예산을 결정해야 한다. | 큰 본문과 느린 소비자 조건에서 정한 메모리·작업 상한을 넘지 않는지 확인해야 한다. |
| NFR-06 | 설정 일관성 | 적용 전파 목표 시간과 revision 편차의 허용 범위를 결정해야 한다. | 일부 인스턴스·worker의 적용 지연을 감지하고, 그 상태에서 확대를 중지할 수 있는지 확인해야 한다. |
| NFR-07 | 데이터 취급 | 수집·저장 허용 필드, 목적별 보존 기간과 조회 권한을 결정해야 한다. | 메모리 캡처, 마스킹, 저장, 조회와 만료 정리가 해당 정책을 지키는지 확인해야 한다. 프록시 API의 인터넷 직접 접근을 차단하고, health 바인드 주소와 실제 접속 peer를 모두 loopback으로 제한하는지도 검증해야 한다. |
| NFR-08 | 운영 단순성 | 필수 배포 단위와 별도 연동의 책임 범위를 구분해야 한다. | 외부 수집기 없이 내부 계측·비교·데이터 보호가 동작해야 한다. 메트릭·상태 HTTP endpoint와 활성화 설정이 없고 내부 snapshot 조회가 유지되는지 확인해야 한다. 별도 listener의 health는 단일 `GET /healthcheck`로 검증해야 한다. |
| NFR-09 | 비용 | Shadow 실행, 관측 저장과 네트워크에 허용할 추가 비용을 결정해야 한다. | 단계별 입력량·실행 증폭·수집량에 근거한 추정값과 실측값을 대조해야 한다. |
| NFR-10 | 재현성 | Fixture, 정책과 환경의 버전 기록 범위를 정해야 한다. | 같은 합성 입력과 같은 정책으로 비교·배정 결과가 재현되는지 확인해야 한다. |

검증 담당자는 각 요구사항에 다음 판단 절차를 적용하십시오.

1. 대상 route와 운영 단계를 정하십시오.
2. 선택한 단계에서 사용하는 요구사항을 식별하십시오.
3. 위 표의 결정 항목에 필요한 기준선·부하·정책을 확보하십시오.
4. 값이나 범위가 미정이면 판단할 수 없는 항목을 기록하십시오.
5. 기준이 결정됐으면 같은 조건에서 [검증 시나리오](test-catalog.md) 또는 [부하 시험](load-and-evidence.md)을 실행하십시오.
6. 실제 동작·측정값을 기준과 대조하십시오.
7. 아래 조건에 따라 충족 여부와 근거를 기록하십시오.

| 확인 결과 | 판정과 기록 |
| --- | --- |
| 기준 충족 | 충족을 확인한 환경과 범위를 기록해야 한다. |
| 기준 위반 | 위반한 기준과 실제 동작·측정값을 기록해야 한다. |
| 목표·표본·관측 부족 | 충족 여부를 `unknown`으로 남겨야 한다. |
| 기준 결정을 위한 선행 측정 | 측정값은 기록하되 요구사항 충족 판정은 보류해야 한다. |

필수 증거가 부족하면 해당 [승격 gate](../rollout/stages-and-gates.md)를 통과로 표시해서는 안 된다.

## 기능 요구사항의 담당 문서

| 요구사항 | 담당 기능 | 검증 시나리오 |
| --- | --- | --- |
| FR-01 | [API 경로 등록과 지원 범위](../routing/route-registration.md) | T-01, T-48 |
| FR-02 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-02 |
| FR-03 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-04 |
| FR-04 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-05 |
| FR-05 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-06, T-07 |
| FR-06 | [설정 스냅샷 검증과 적용](../routing/configuration.md) | T-01, T-08 |
| FR-07 | [HTTP 요청 전달과 사용자 응답](../routing/http-forwarding.md) | T-02, T-09, T-10, T-15, T-43, T-47, T-48 |
| FR-08 | [API 경로 등록과 지원 범위](../routing/route-registration.md) | T-03 |
| FR-09 | [복제 적격성 판단과 표본 선택](../shadow/eligibility-and-sampling.md) | T-11 |
| FR-10 | [복제 적격성 판단과 표본 선택](../shadow/eligibility-and-sampling.md) | T-12 |
| FR-11 | [양쪽 API 실행과 응답 경로 분리](../shadow/parallel-execution.md) | T-09, T-17, T-43 |
| FR-12 | [양쪽 API 실행과 응답 경로 분리](../shadow/parallel-execution.md) | T-04, T-13 |
| FR-13 | [실행 수명·취소·자원 제한](../shadow/lifecycle-and-limits.md) | T-14, T-16, T-17, T-18, T-23, T-38, T-42, T-44 |
| FR-14 | [실행 수명·취소·자원 제한](../shadow/lifecycle-and-limits.md) | T-13, T-14, T-36, T-42 |
| FR-15 | [양쪽 API 실행과 응답 경로 분리](../shadow/parallel-execution.md) | T-12, T-15 |
| FR-16 | [Shadow 중지·serving 롤백·프록시 우회](../rollout/rollback-and-bypass.md) | T-32, T-33 |
| FR-17 | [비교 문맥과 결과 판정](../comparison/context-and-outcomes.md) | T-02, T-21, T-22 |
| FR-18 | [응답 정규화와 의미 비교](../comparison/normalization.md) | T-10, T-18, T-19 |
| FR-19 | [응답 정규화와 의미 비교](../comparison/normalization.md) | T-20 |
| FR-20 | [비교 문맥과 결과 판정](../comparison/context-and-outcomes.md) | T-16, T-17, T-21, T-22 |
| FR-21 | [비교 이벤트와 전환 이력 모델](../collection/event-model.md) | T-24, T-29, T-41 |
| FR-22 | [비동기 적재·재시도·유실 처리](../collection/async-storage.md) | T-23, T-24, T-36, T-44 |
| FR-23 | [상세 표본 선택과 데이터 마스킹](../collection/detail-sampling.md) | T-25 |
| FR-24 | [상세 표본 선택과 데이터 마스킹](../collection/detail-sampling.md) | T-26, T-27, T-39 |
| FR-25 | [이벤트 조회와 보존 기간 관리](../collection/query-and-retention.md) | T-28 |
| FR-26 | [응답 정규화와 의미 비교](../comparison/normalization.md) | T-40 |
| FR-27 | [실행·비교·수집 메트릭 계측](../observability/metrics.md) | T-29, T-45, T-47 |
| FR-28 | [비교 커버리지와 실험 분석](../observability/coverage-and-analysis.md) | T-14, T-24, T-29, T-30, T-41, T-45 |
| FR-29 | [설정 스냅샷 검증과 적용](../routing/configuration.md) | T-01, T-07, T-31, T-46, T-48 |
| FR-30 | [점진적 전환 단계와 승격 판단](../rollout/stages-and-gates.md) | T-30, T-33, T-34, T-37, T-41 |
| FR-31 | [Shadow 중지·serving 롤백·프록시 우회](../rollout/rollback-and-bypass.md) | T-35 |
| FR-32 | [마이그레이션 종료와 프록시 제거](../rollout/retirement.md) | T-39 |
