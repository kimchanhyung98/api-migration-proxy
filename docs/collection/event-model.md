# 비교 이벤트와 전환 이력 모델

- 한 논리 요청의 양쪽 결과를 하나의 기록으로 연결
- 비교 이벤트와 전환 변경 이력의 저장 목적·필드 구분

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | request 식별, backend 결과, 비교 판정, 설정·정책 revision과 수집 문맥 |
| 결과·출력 | 고유 comparison event 및 별도 rollout change 기록의 논리 필드 |
| 실패·제한 | 외부 request ID의 고유 키 사용 금지, 비교 요약의 유실 허용을 중요한 변경 이력에 자동 적용 금지 |

## 수집 단위와 필드

- 초기 논리 엔터티: `comparison_event`, `rollout_change`
- `comparison_event`: shadow 표본으로 선택한 한 논리 요청당 최대 1개, 미실행·오류·비교 불가 결과 포함
- 표본 제외 요청: 메트릭만 유지, 비교 이벤트 필수 생성 대상 제외
- 최종 요약 확정: serving backend 결과, shadow 결과 또는 미실행 사유, 사용자 응답 전송 종료 결과 확보 후 수행
- 느린 client의 전송 종료 전: backend 결과를 사용자 응답 완료로 대체하지 않음; 상한 내 상태 유지 후 완료·취소·전송 실패 기록
- 설정 원문·원본 API 데이터·메트릭 시계열의 단일 DB 통합 저장은 전제하지 않음
- PostgreSQL 선택 시 관계형 필드·제한된 JSON으로 매핑 가능; 물리 스키마는 미정

| 이벤트 묶음 | 필수 내용 |
| --- | --- |
| 식별 | event ID, 요청 시작·사용자 요청 종료 시각, route ID, 환경·마이그레이션 식별 |
| 정책 | configuration revision, comparison policy revision, epoch 식별 |
| 배정 | serving backend, 응답 출처·사용자 요청 종료 결과, shadow 선택·실제 시작 여부, 배정 방식과 선택적 비민감 cohort 분류 |
| 양쪽 결과 | [backend 결과 모델](../comparison/context-and-outcomes.md)의 버전·역할·배포 revision·실행 결과·계약 class·status·지연·크기·캡처 상태 |
| 비교 | result, reason, comparison_class, 차이 개수·집계 제한 여부, 제한된 차이 경로·목록 절단 여부, 정책 적용 내역 |
| 문맥 | 데이터 source·revision 또는 freshness 수준 등 비교에 필요한 비민감 요약 |
| 수집 | 양쪽 종료·비교 완료·이벤트 생성·저장 시각, schema version, 상세 표본 선택 여부·저장 상태, 목적별 보존 만료 기준 |
| 상관관계 | 내부 request ID, 선택적 검증된 trace ID |

- event ID: 저장 재시도·배치 일부 성공 확인의 중복 방지 키
- 저장 성공 응답 유실 후 재시도: 같은 ID 사용, 동일 이벤트의 복수 행·중복 완료 집계 방지
- 외부 request ID 중복·재사용과 무관한 내부 식별자 생성
- 식별 시각: UTC 등 공통 형식 사용; 요청 시작과 늦게 끝난 shadow·비교·저장 시각 구분
- 지속 시간: 단조 시계 기반 값 보존; 다른 호스트 벽시계 차이로 backend 지연 계산 금지
- backend 배포 revision: 확인 가능한 실제 실행 버전 기록, 확인 불가 시 `unknown`; 프록시 설정 revision으로 대체 금지
- 필수 문맥 누락: 빈 문자열로 정상 상태 표현 금지, `unknown` 또는 이유 코드 기록
- 차이·경로 개수 제한 도달: 정확한 총개수로 표현 금지, 제한 여부 병기
- 요약·상세 보존 만료: 최초 생성 기준 유지, 저장 재시도로 무기한 연장 금지

### 비교 이벤트 예시

- 양쪽 성공 응답의 값 차이에 대한 합성 예시
- 실제 DB 컬럼 정의·완전한 전송 schema와 구분

```json
{
  "schema_version": 1,
  "event_id": "example-event-001",
  "route_id": "catalog_detail",
  "configuration_revision": "example-revision-003",
  "comparison_policy_revision": "catalog-compare-001",
  "epoch_id": "example-epoch-003",
  "serving_backend": "v1",
  "shadow_selected": true,
  "backends": {
    "v1": {
      "role": "serving",
      "deployment_revision": "example-v1-build-007",
      "execution_outcome": "http_response",
      "contract_class": "success",
      "status_code": 200,
      "duration_ms": 42
    },
    "v2": {
      "role": "shadow",
      "deployment_revision": "example-v2-build-012",
      "execution_outcome": "http_response",
      "contract_class": "success",
      "status_code": 200,
      "duration_ms": 55
    }
  },
  "comparison": {
    "result": "different",
    "reason": "json_value_mismatch",
    "comparison_class": "success",
    "difference_paths": ["/price"]
  },
  "detail_sampled": false
}
```

### 전환 이력

- `rollout_change` 필드: 변경 ID·route 또는 그룹·변경 주체·시각·이전/다음 revision
- 운영 문맥: 이전/다음 stage·비율·변경 사유·gate evidence 참조·적용 결과·복귀 대상 revision
- 비교 요약 유실 허용 정책의 변경 이력 자동 적용 금지
- 저장소 장애 시에도 기존 배포 이력 등 추적 가능한 경로로 긴급 중지·복귀 기록 유지

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-21 | P0 | 요청별 양쪽 결과를 하나의 요약으로 수집 | request 식별·revision·역할·오류·지연·비교 결과가 연계됨 |

## 검증 계획

- 구현 후 검증: [T-24, T-28, T-29, T-41](../validation/test-catalog.md)

## 관련 기능

- [적재와 중복 방지](async-storage.md)
- [운영 변경 기록](../rollout/change-management.md)
