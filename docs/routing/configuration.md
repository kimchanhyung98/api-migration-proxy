# 설정 스냅샷 검증과 적용

- 목적: route·실행·비교 정책을 유효한 revision으로 묶고 요청 동안 일관되게 적용

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 이전·다음 revision, 등록 경로·목적지·정책·운영 상한, 변경 사유 |
| 결과·출력 | 검증된 불변 스냅샷, 인스턴스별 적용 상태, 설정 변경 이력 |
| 실패·제한 | 잘못된 갱신 거부 및 마지막 유효 설정 유지; 최초 유효 설정 부재 시 readiness 실패 |

## 설정 모델

- 설정 키: 논리 모델 기준; 선택한 구현에 맞춰 구체화
- 필수 운영값 누락 route: 활성화 금지
- `rollout_enabled=false`: v1 통과만 수행; shadow 미실행
- S2 단계: `rollout_enabled=true`, `v2_serve_ratio=0`, `shadow_sample_ratio>0`으로 v1 응답·v2 비교
- 전체 전환 비활성화와 반대편 shadow 중지: 별도 조작

| 설정 묶음 | 필요한 값 |
| --- | --- |
| 식별 | schema version, revision, 이전 revision, 변경 사유 |
| 목적지 | 기본 v1, route별 v1/v2, 필요할 때만 명시적 path 매핑 |
| 범위 | route ID, method·template, 전환 활성 여부, shadow eligibility |
| 배정 | v2 비율, cohort 방식·그룹·키 출처·salt 식별자 |
| 복제 | shadow 비율, shadow 중지 상태, 실행 한도 |
| 비교·수집 | comparison policy revision, 캡처 상한, 상세 표본율·허용 필드·보존 정책 |
| 자원 | 역할별 timeout·동시 실행, 큐 길이·작업 수명, batch·저장 재시도 예산 |

- 예시 범위: 비활성 route의 논리 설정; 실행 가능한 완전 설정 파일 아님
- `null` 의미: 검증 전 미정 값; 무제한·자동 기본값 의미 아님

```yaml
schema_version: 1
revision: example-draft-001
default_backend: v1
routes:
  - route_id: catalog_detail
    method: GET
    path_template: /catalog/{id}
    rollout_enabled: false
    backends:
      v1: https://v1.example.invalid
      v2: https://v2.example.invalid
    v2_serve_ratio: 0
    cohort:
      mode: user
      group: catalog
      key_source: trusted_identity
      salt_ref: null
    shadow:
      eligible: false
      sample_ratio: 0
      timeout_ms: null
      max_inflight: null
    comparison:
      policy_revision: null
      capture_limit_bytes: null
    collection:
      detail_sample_ratio: 0
      retention_days: null
```

- credential 처리: 원문 저장 금지; 기존 비밀 관리 방식의 참조 사용

## 활성화 전 검증

| 검증 대상 | 거부 조건 |
| --- | --- |
| 비율 | 0~1 범위 밖 값·숫자가 아닌 값 |
| 시간·동시 실행·메모리·큐 상한 | 활성 기능에 필요한 값 누락·유효하지 않은 상한 |
| route·목적지 | 중복·우선순위 모호·허용하지 않은 backend |
| cohort group | 그룹 내부의 전환 활성 상태·배정 단위·키 출처·해시·직렬화·salt·v2 비율 불일치 |
| shadow | 실제 효과·권한·데이터 계약의 검토 없이 eligibility 활성화 |
| 비교·상세 수집 | 활성 기능에 필요한 정책 revision·허용 필드·캡처 상한·보존 정책 누락 |

## 설정 변경과 충돌

- 초기 반영: 버전 있는 파일과 기존 배포·재적용 절차 사용
- 필수 구성 제외: 동적 설정 서버·DB polling
- 요청 처리: 시작 시 읽은 유효 스냅샷만 사용
- 동시 변경: 이전 revision 확인 후 오래된 변경의 덮어쓰기 거부
- 변경 이력: 요청 revision·실제 적용 시작·완료·실패 인스턴스 구분
- 부분 적용 기간: 인스턴스별 revision 구분; 동일 실험 조건으로 합산 금지
- worker가 설정을 독립적으로 읽는 구성: 각 worker의 신규 요청용 revision까지 확인; 임의 worker 하나의 응답으로 인스턴스 전체 적용 완료 판정 금지. 진행 중 요청은 기존 스냅샷 유지
- 잘못된 갱신: 마지막 유효 설정 유지
- 최초 기동의 유효 설정 부재: readiness 실패
- shadow 중지·v1 롤백: 실제 배포 방식의 전파 시간을 목표 복귀 시간과 대조하여 검증

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-06 | P0 | 요청당 하나의 설정 스냅샷 사용 | 처리 중 변경이 있어도 목적지·비율·비교 정책이 섞이지 않음 |
| FR-29 | P0 | 설정 검증·적용 현황·변경 이력 제공 | 유효하지 않은 설정 거부, 인스턴스별 적용 revision과 변경 주체 추적 |

## 검증 계획

- 구현 후 검증: [T-01, T-07, T-08, T-31](../validation/test-catalog.md)

## 관련 기능

- [비율의 구분](../shadow/eligibility-and-sampling.md)
- [운영 변경 절차](../rollout/change-management.md)
