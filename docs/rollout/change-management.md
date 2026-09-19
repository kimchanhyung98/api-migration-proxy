# 전환 변경 적용과 이력 관리

- 전환 판단을 실제 설정 변경으로 연결
- 적용 요청·완료·실패·복귀 이력 기록

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 현재·다음 stage와 revision, gate 결과, 변경 주체·이유와 복귀 대상 |
| 결과·출력 | 적용 결과, 인스턴스별 revision·실제 비율과 새 관측 epoch |
| 실패·제한 | 배포 완료만으로 gate 통과 표시 금지, 관측 저장소 장애 중에도 긴급 중지 이력 추적 경로 확보 |

## 설정 변경 절차

1. 현재 stage·revision·실제 비율과 다음 변경 목적 확인
2. 필수 gate 결과·근거 기록; 수치·표본 미정 시 확대 보류
3. 이전 revision 확인 후 새 설정 검증; 같은 cohort group의 전환 활성 상태·배정 정책·비율 일치 확인
4. 정해진 배포·설정 적용 절차로 적용; 전파 시작 시각과 혼합 revision 구간 기록
5. 대상 인스턴스별 revision과 실제 serving·shadow 분포 확인
6. 적용 완료 후 안정된 관측 조건의 새 epoch 확정, 오류·지연·드롭 집중 관측
7. 기준 충족 시 유지·확대, 악화 시 중지·복귀

- 초기 단계 변경: 담당자의 수동 판단과 기존 변경 관리 방식 사용
- 별도 승인 제품·다단계 워크플로의 필수 도입 제외
- 전파 중 요청: 실제 요청 스냅샷 revision에 귀속, 전파 전후 데이터를 하나의 동일 조건으로 합산 금지
- backend 배포·비교 정책·비율의 동시 변경: 가능한 경우 분리; 혼합 시 원인 분리 한계 기록
- 일부 적용 실패: 확대 중지, 마지막 유효 revision과 인스턴스별 복구 결과 확인
- 긴급 중지·롤백: 일반 승격 표본 확보를 기다리지 않고 사전 정의한 중지 조건·복귀 준비 상태에 따라 수행

## 변경 기록 양식

```text
change_id / route_or_group / owner:
current_stage -> next_stage:
previous_revision -> next_revision:
v2_serve_ratio / shadow_sample_ratio / detail_sample_ratio:
backend deployment versions / comparison policy revision:
observation epoch / sample counts / missing intervals:
propagation started_at / mixed revision interval / stable_epoch_at:
G-01 through G-08 results and evidence:
stop conditions / rollback revision / v1 readiness:
requested_at / applied_at / applied_instances:
actual result / recovery time / follow-up:
```

- 양식의 목적: 필요한 변경 증거의 최소 목록 제시
- 기존 운영 이력으로 같은 정보 확인 가능 시 별도 데이터 입력 제품 불필요

## 검증 계획

- 구현 후 검증: [T-31, T-33](../validation/test-catalog.md)

## 관련 기능

- [설정 검증과 충돌 처리](../routing/configuration.md)
- [전환 이력 필드](../collection/event-model.md)
