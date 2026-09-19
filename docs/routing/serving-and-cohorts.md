# 사용자 응답 선택과 cohort 배정

- 목적: 요청별 사용자 응답 backend 단일 선택 및 연관 API 그룹의 배정 일관성 유지

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 등록 route·cohort group, 신뢰할 수 있는 배정 키, v2 응답 비율과 고정 salt |
| 결과·출력 | 요청에 고정된 serving backend와 배정 이유 |
| 실패·제한 | 필수 cohort 키 누락 시 v1 배정; shadow의 속도·실패에 따른 선택 변경 금지 |

## route와 배정 기준

- route 식별: method·path template 및 안정적인 `route_id` 사용
- 중복 경로·모호한 wildcard 우선순위: 설정 거부
- `GET /items/{id}`와 `POST /items/search`: 별개 route; POST 여부만으로 복제 가능성 배제 금지
- 연관 목록·상세 API: 같은 버전 배정이 필요한 경우 동일 `cohort_group`·배정 키 사용
- 그룹 일관성 조건: 전환 활성 상태(`rollout_enabled`)·키 출처·배정 단위·해시·직렬화 규칙·salt·v2 비율 일치
- 동일 그룹 내부 정책 불일치: 활성화 전 설정 검증 실패
- 독립 route: 자체 group 사용
- MVP 범위: 무상태 API 우선; backend 전용 cursor·transaction을 요구하는 그룹 제외

| 배정 방식 | 사용 조건 | 키가 없을 때 |
| --- | --- | --- |
| 사용자 단위 | 인증된 사용자 식별을 신뢰할 수 있고 사용자별 일관성이 필요 | v1으로 배정하고 `missing_cohort_key` 기록 |
| 세션 단위 | 세션 동안 결과 버전이 같아야 함 | v1 배정; backend 종속 세션은 MVP 제외 |
| 테넌트 단위 | 테넌트 전체 전환이 필요한 업무 | v1 배정; 큰 테넌트의 트래픽 편중 별도 검토 |
| 요청 단위 | 키가 없는 완전 무상태 API | 요청별 독립 bucket 사용; 사용자 일관성 보장 제외 |

- bucket 생성: `cohort_group + 검증된 key + salt`를 고정 해시·직렬화 규칙으로 계산 후 `[0, 1)` 범위로 정규화
- 직렬화: 필드 경계가 모호하지 않은 규칙 사용; 값 단순 연결에 따른 충돌 방지
- 프로세스 재시작: seed가 바뀌는 언어 기본 hash 의존 금지
- v2 배정 조건: `bucket < v2_serve_ratio`
- 비율 상승: group·키·salt·알고리즘 유지 시 기존 v2 cohort 보존
- salt·배정 단위 변경: 별도 실험 epoch로 분리
- 같은 그룹의 배정 보장 범위: 동일 설정 revision·배정 조건
- 설정 적용 혼재·비율 변경 전후의 연속 요청: 서로 다른 backend 배정 가능; 요청당 스냅샷만으로 세션 전체의 고정 배정 보장 불가
- 전파 구간의 운영 수준: 연관 API의 버전 혼용 허용 여부·세션 수명·전파 시간·복귀 목표를 바탕으로 결정
- cohort 비율과 실제 요청 비율: 사용자별 활동량에 따른 차이 허용; 사용자 10% 배정을 QPS 10%로 해석 금지

## v1 통과와 배정 경계

- 전환 비활성: v1 통과만 수행
- 전환 활성 및 `v2_serve_ratio=0`: serving은 v1; shadow 활성 여부는 별도 실행 정책으로 판단
- `v2_serve_ratio=1`: 등록·활성화·배정 조건을 만족한 대상 전체의 v2 배정
- 필수 키 누락·미등록·제외 경로: 별도 집계
- 운영 확인: 설정 비율과 실제 사용자 응답 분포 함께 확인
- 배정 책임: 진입 gateway와 프록시 중 전환 판단 주체 명시; 독립적인 이중 비율 적용 시 실제 노출·cohort·복귀 계약 검증
- 기존 weighted routing·cookie stickiness의 현재 cohort 대체 여부: 같은 배정·관측·복귀 계약을 충족하는지 검증한 뒤 결정

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-02 | P0 | v1 통과 모드 제공 | shadow=0, v2 비율=0에서 v2 요청이 발생하지 않고 기존 계약 보존 |
| FR-03 | P0 | 요청마다 serving 백엔드를 한 번 선택 | 응답 속도나 shadow 결과가 선택을 바꾸지 않음 |
| FR-04 | P0 | 경로 또는 연관 경로 그룹별 v2 응답 비율 제어 | 0과 1의 경계값 동작, 같은 revision의 일관된 배정 확인 |
| FR-05 | P0 | 필요한 API에 안정적인 cohort 적용 | 고정 해시·직렬화에서 동일 키·그룹·salt의 bucket 재현, 그룹 정책 불일치 거부, 비율 상승 시 기존 v2 cohort 유지 |

## 검증 계획

- 구현 후 검증: [T-02, T-04, T-05, T-06, T-07](../validation/test-catalog.md)

## 관련 기능

- [HTTP 응답 전달](http-forwarding.md)
- [단계와 승격 판단](../rollout/stages-and-gates.md)
