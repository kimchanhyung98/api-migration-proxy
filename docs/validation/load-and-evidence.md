# 부하 시험과 검증 증거 기록

## 검증 계층

| 계층 | 확인 가능한 것 | 확인할 수 없는 것 |
| --- | --- | --- |
| 문서·정적 검사 | 필드·용어·참조·문법, 설정 schema | 실제 HTTP 전달·지연·운영 상태 |
| 순수 로직 테스트 | cohort·비교 정책·결과 분류·계산 | 네트워크·취소·프로세스 수명 |
| 로컬 HTTP 통합 | 제어된 v1/v2에서 전달·timeout·caps·양쪽 실행 | 실제 권한·데이터·클라우드 부하 |
| 실제 API 계약 | 서비스 입력·출력·권한·예상 오류 | 전체 피크·장애·장기 운영 |
| 부하·장애 시험 | 대표 도착률·크기·실패에서 자원·가용성 | 시험 밖의 모든 workload |
| 제한된 운영 | 실제 사용자·데이터·주기의 영향 | 관측하지 않은 구간과 제외된 경로 |

- 결과별 기록: source revision, 설정·비교 revision, 실제 backend 배포 버전, 실행 환경
- 입력 증거: fixture 또는 비민감 표본 설명, 요청 분포·관측 시간
- 결과 증거: 성공·실패·누락과 관련 gate

## 부하 시험 설계

- 대표 입력 정의: method·route·요청/응답 크기·정상/거절/오류 비중·도착률
- 측정 항목: 동시 사용자 수뿐 아니라 실제 QPS·inflight·완료율 포함
- 합성 입력과 실제 workload 분포의 구분

| 실험 | 목적 | 비교 조건 |
| --- | --- | --- |
| v1 직접 vs 프록시 v1 통과 | 추가 hop·전달 비용 분리 | 같은 입력 분포·backend 배포·cache 조건 |
| shadow 0 vs 일부 vs 전체 | 추가 실행·수집 비용 | serving은 v1으로 고정 |
| v1 serving vs v2 serving | 실제 사용자 경로 성능 | shadow 조건·관측 policy를 맞추고 역할별 비교 |
| 상세 0 vs 허용 표본 | 상세 저장·마스킹 비용 | 같은 shadow 실행량과 입력 분포 |
| warm vs cold·restart | 배포·복귀 시 악화 | warm-up 과정·노드·cache 상태 기록 |
| 저장소 정상 vs 지연·중단 | 관측 장애 격리 | queue·유실·사용자 지표 동시 확인 |

- 부하 생성기의 응답 대기로 목표 도착률이 낮아지는 현상 확인
- 목표 도착률·실제 전송량·완료율과 부하 생성기에서 발행하지 못한 요청의 동시 보고
- 생성기 CPU·네트워크·동시 실행 부족과 대상 응답 지연에 따른 미발행을 구분; 목표 부하가 실제로 도달하지 않은 구간의 용량 통과 판정 금지
- k6 채택 시 예: [dropped_iterations](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/dropped-iterations/)와 실제 HTTP 요청 수를 함께 기록; iteration 수와 요청 수의 차이는 시나리오에 따라 해석
- timeout·취소·미완료 포함; 빠르게 완료된 표본만의 성능 요약 금지
- full shadow 검증 시: 설정 비율 1과 실제 시작·종료·오류 비율을 함께 기록
- 자원 보호로 실행이 줄어든 구간: full-load 검증 통과 근거에서 제외

## 검증 결과 기록 양식

```text
test_id / requirement_ids / relevant_gates:
source_revision / config_revision / comparison_policy_revision:
environment / backend_versions / dependency_state:
input_distribution / request_count / duration / actual_qps:
expected_behavior:
actual_behavior / metrics / event_sample:
status: pass | fail | not_run | inconclusive
limitations / missing_evidence:
owner / next_action:
```

- `not_run`·`inconclusive`: gate에 필요한 증거가 없는 상태
- comparator 단위 검증·HTTP 200·프로세스 readiness만으로 전환 완료 판정 금지
