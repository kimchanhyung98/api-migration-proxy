# 구현·검증·종료 작업

- 범위: 현행 기능 기획에 포함된 M0~M7의 실행 작업과 완료 조건
- 현재 상태: 전 단계 미착수; 문서 작성·검토를 구현 완료로 처리하지 않음
- 기능 계약·검증 시나리오: 담당 기획 문서를 기준으로 적용; 별도 요구사항 정의를 중복 관리하지 않음

## 개발 원칙

- 첫 개발 범위: API 하나의 v1 통과 → shadow → 비교 → 일부 v2 응답 → 롤백 연결
- 검증 순서: 실제 요청 한 흐름의 동작 확인 후 공통 구성 확장
- 실제 대상 부재 시: 합성 fixture·제어 가능한 v1/v2 stub으로 로컬 계약 검증
- 증거 구분: 합성 검증과 실제 API·부하·운영 검증을 별도 기록

## 개발 단계와 완료 조건

| 단계 | 산출물 | 완료 조건 | 의존 |
| --- | --- | --- | --- |
| M0 대상 확정 | 첫 route 계약, 부작용·데이터·권한 목록, 운영 수치와 실행 스택 선택 근거 | 해당 단계의 필수 미정 항목과 검증 방법 확보 | 사용자·서비스 환경 입력 |
| M1 v1 통과 | 최소 HTTP 프록시, 기본 메트릭, 유효 설정 로딩 | 공개 계약·오류·전송·우회 검증 | M0, 실행 스택 결정 |
| M2 양쪽 실행 | shadow 선택·자원 상한·수명 관리 | 양쪽 한 번 실행, 선택 응답 유지, 느린 shadow 격리 | M1 |
| M3 비교·수집 | comparator, 요약 이벤트·제한 큐·저장 | 오류·비교 불가·유실 분리, 마스킹·중복·보존 검증 | M2, 수집 계약 |
| M4 점진적 전환 | cohort·비율·revision·전환 이력 | 배정 일관성·설정 적용·중지·롤백 검증 | M3 |
| M5 대표 환경 검증 | 실제 API contract·통합·부하·장애 결과 | G-01~G-08에 해당하는 사전 gate 충족 | M4, 실제 테스트 환경 |
| M6 제한된 운영 전환 | S1~S4 실행 기록과 관측 | 정한 사용자·비교·자원 기준 및 복귀 실측 | M5 |
| M7 종료 | v1 잔존 의존·복귀 기간 확인, v2 직접 경로와 최종 종료 기록 | 직접 호출·우회 검증, 잔존 의존 정리와 보존 기한별 기록 관리 | M6 결과와 종료 조건 충족 |

- 일정·인력: 미정; M0에서 API 복잡도·환경 준비 시간을 바탕으로 산정
- P0의 의미: 해당 기능을 사용하는 운영 단계 전 필수 검증, 첫 코드 변경에 모두 포함하는 의미 아님
- 단계별 미결정 입력: 필수 운영값·담당자·검증 방법을 해당 개발·운영 단계 진입 전에 확정
- 미정 값의 확보·결정은 선행 작업; 기술 후보·담당자·운영 수치가 이미 확정되었다는 의미 아님
- 두 번째 API·업무별 비교 확장: FR-26의 실제 필요 조건에 따르며, 이 확정 작업 목록에는 포함하지 않음

## 단계별 기획·검증 연결

| 단계 | 기준 문서 |
| --- | --- |
| M0 | [적용 범위](../docs/product/scope-and-workflows.md), [권한·데이터 책임](../docs/_rules/identity-and-data-boundaries.md), [실행 환경](../docs/infrastructure/deployment-and-stack.md), [품질 목표](../docs/validation/quality-and-acceptance.md) |
| M1 | [라우팅·HTTP·설정](../docs/routing/README.md), [프록시 우회](../docs/rollout/rollback-and-bypass.md) |
| M2 | [Shadow 실행·적격성·수명](../docs/shadow/README.md) |
| M3 | [응답 비교](../docs/comparison/README.md), [수집·보존](../docs/collection/README.md), [계측·분모](../docs/observability/README.md) |
| M4 | [배정·cohort](../docs/routing/serving-and-cohorts.md), [설정 적용](../docs/routing/configuration.md), [변경·복귀 절차](../docs/rollout/README.md) |
| M5 | [요구사항·검증 시나리오](../docs/validation/README.md), [부하·장애 증거](../docs/validation/load-and-evidence.md) |
| M6 | [운영 단계·gate](../docs/rollout/stages-and-gates.md), [사용자·관측 상태](../docs/observability/dashboards-and-alerts.md) |
| M7 | [종료 계약](../docs/rollout/retirement.md), [보존 기간 관리](../docs/collection/query-and-retention.md) |

- 완료 기록: 해당 FR/NFR·검증 시나리오와 실제 결과·미확인 범위 연결
- 합성 검증만 수행한 작업: 실제 API·부하·운영 완료 상태와 구분

## MVP 완료 정의

- 운영 진입 조건: 해당 단계의 P0·NFR 목표를 실제 API 계약·제한된 부하·장애 시험으로 검증
- 단계별 완료: [G-01~G-08](../docs/rollout/stages-and-gates.md)의 적용 대상 gate와 실제 관측 기록으로 판정
- 미사용 기능의 gate: 적용 제외 이유 기록; 미검증 상태를 통과로 표시 금지
- 최종 완료 조건: 전체 v2 응답, 복귀 기간·v1 잔존 의존·직접 연결·관측 기록 보존 정리
- 별도 상태 관리: 코드 구현 완료 / 운영 전환 완료 / 프록시 종료 완료
