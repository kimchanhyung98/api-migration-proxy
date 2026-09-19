# 공통 품질 요구사항과 기능 추적

- P0: 해당 기능을 사용하는 첫 운영 단계 전에 검증할 필수 요구사항
- P1: 실제 필요가 확인된 경우 추가할 요구사항
- 기능별 FR: 담당 기능 문서에서 정의·관리

## 품질 요구사항과 미정 수치

- 요구사항 채택 후에도 측정값이 없으면 충족 여부 `unknown`
- 실제 성능 목표: 대상 API의 기준선·대표 부하·허용 지연·오류 예산을 확보한 뒤 확정
- 임의 수치의 제품 성능 보장 금지

| ID | 요구사항 | 정해야 할 값 | 검증 기준 |
| --- | --- | --- | --- |
| NFR-01 | 사용자 응답 성능 | route별 p95/p99, 추가 지연 허용치 | 동일한 대표 부하에서 v1 직접 호출·v1 통과·shadow 실행 비교 |
| NFR-02 | 가용성 | serving 오류 예산과 복귀 목표 시간 | backend·저장소·프록시 장애를 분리해 주입하고 사용자 영향 측정 |
| NFR-03 | 처리 용량 | 평시·피크 QPS, 동시 요청·본문 분포 | 정상·피크·긴 응답·cold start에서 자원 상한과 완료율 확인 |
| NFR-04 | 수집 완전성 | 허용 이벤트 유실·저장 지연 | 큐·저장 실패, restart 시 메트릭과 이벤트 차이를 구간별 보고 |
| NFR-05 | 메모리 상한 | 요청 복제·응답 캡처·큐·비교 CPU 예산 | 큰 본문과 느린 소비자에서도 예상 상한을 넘지 않음 |
| NFR-06 | 설정 일관성 | 적용 전파 목표 시간과 revision 편차 허용 | 일부 인스턴스 지연을 감지하고 확대를 멈출 수 있음 |
| NFR-07 | 데이터 취급 | 허용 필드, 보존 기간, 조회 권한 | 메모리 캡처·마스킹·저장·조회·만료 정리까지 확인 |
| NFR-08 | 운영 단순성 | 필수 배포 단위와 담당 범위 | 초기에는 프록시 서비스·관측 저장소·기존 모니터링으로 운영 가능 |
| NFR-09 | 비용 | shadow·관측 저장·네트워크의 추가 예산 | 단계별 입력량·실행 증폭·수집량에 근거해 추정하고 실측 |
| NFR-10 | 재현성 | fixture·정책·환경 버전 기록 | 같은 합성 입력과 같은 정책에서 비교·배정 결과 재현 |

## 기능 요구사항의 담당 문서

| 요구사항 | 담당 기능 | 검증 시나리오 |
| --- | --- | --- |
| FR-01 | [API 경로 등록과 지원 범위](../routing/route-registration.md) | T-01 |
| FR-02 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-02 |
| FR-03 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-04 |
| FR-04 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-05 |
| FR-05 | [사용자 응답 선택과 cohort 배정](../routing/serving-and-cohorts.md) | T-06, T-07 |
| FR-06 | [설정 스냅샷 검증과 적용](../routing/configuration.md) | T-01, T-08 |
| FR-07 | [HTTP 요청 전달과 사용자 응답](../routing/http-forwarding.md) | T-02, T-09, T-10, T-15, T-43 |
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
| FR-27 | [실행·비교·수집 메트릭 계측](../observability/metrics.md) | T-29, T-45 |
| FR-28 | [비교 커버리지와 실험 분석](../observability/coverage-and-analysis.md) | T-14, T-24, T-29, T-30, T-41, T-45 |
| FR-29 | [설정 스냅샷 검증과 적용](../routing/configuration.md) | T-01, T-07, T-31 |
| FR-30 | [점진적 전환 단계와 승격 판단](../rollout/stages-and-gates.md) | T-30, T-33, T-34, T-37, T-41 |
| FR-31 | [Shadow 중지·serving 롤백·프록시 우회](../rollout/rollback-and-bypass.md) | T-35 |
| FR-32 | [마이그레이션 종료와 프록시 제거](../rollout/retirement.md) | T-39 |
