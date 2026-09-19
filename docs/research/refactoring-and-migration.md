# API 리팩토링·마이그레이션 패턴과 적용 경계

- 범위: 점진적 교체·비노출 실행·임시 전환 구성의 API 적용 조건

## 적용 원리

| 원리 | 의미 | 적용 조건 |
| --- | --- | --- |
| 점진적 기능 교체 | 기존 진입점 앞의 프록시에서 기존·신규 구현으로 경로 분기, 내부 호출·데이터 의존 처리 필요 | 대상 route를 좁혀 전환하되 외부 HTTP 관측만으로 내부 의존 제거 판정 금지 |
| Dark launching | 신규 동작을 사용자 결과에 노출하지 않고 실행; 병렬 실행 결과 비교 가능 | shadow 실행·비교 단계의 근거; 실제 사용자 노출 이후 영향은 canary에서 별도 확인 |
| 임시 전환 구성 | 기존·신규 공존에 필요한 구성의 도입과 목적 달성 후 제거 | 프록시 제거·직접 v2 경로 검증을 완료 조건으로 유지 |

- 패턴 참고: [Strangler fig](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html), [Dark Launching](https://martinfowler.com/bliki/DarkLaunching.html), [Transitional Architecture](https://martinfowler.com/articles/patterns-legacy-displacement/transitional-architecture.html)
- 마이크로서비스 분해: 필수 전제 아님; 같은 서비스의 언어·내부 구조·인프라 교체에도 적용할 범위 검토

## 서로 다른 네 가지 제어

| 제어 | 답하려는 질문 | 그 자체로 증명하지 못하는 것 |
| --- | --- | --- |
| 경로 분기 | 이 기능을 기존·신규 중 어디에서 처리할지 | 같은 입력의 응답 일치 |
| 사용자 canary | 일부 사용자에게 신규 응답을 제공해도 되는지 | 나머지 요청의 신규 동작·전체 용량 |
| shadow 실행 | 사용자 응답 선택을 유지하면서 반대편도 실행할지 | 양쪽 결과 수집·응답 비교 완료 |
| 비교·수집 | 실행 결과가 어떻게 다르고 관측이 얼마나 남았는지 | 전체 데이터 정합성·업무 효과·운영 성공 |

- 본 제품의 조합: 사전 serving 배정 + 선택된 반대편 실행 + 양쪽 결과 연계 + 독립적인 상세 저장 표본
- 도구 검토 기준: 위 제어 중 제공하는 범위와 별도 구현이 필요한 범위 확인
- 운영자 관점: 프록시의 구성 요소 개수보다 사용자 영향·차이·누락·복귀 가능성 중심의 판단
- 기능 계약: [serving 배정](../routing/serving-and-cohorts.md), [shadow 실행](../shadow/parallel-execution.md), [비교 결과](../comparison/context-and-outcomes.md)

## 리팩토링 유형별 선행 조건

| 변경 유형 | 전환 전 확인 | 프록시 밖의 담당 범위 |
| --- | --- | --- |
| 같은 계약의 코드 재구현 | 같은 입력·권한·오류 계약, 의도적 차이 목록 | v1/v2 구현과 계약 fixture |
| 배포 환경·런타임 교체 | 인증·네트워크·전송 동작, 대표 부하와 종료 수명 | 실행 환경 구성·배포·용량 확보 |
| 서비스 경계 분리 | 외부 요청뿐 아니라 내부 호출·batch·consumer 의존 | 호출자 변경, 필요한 adapter, 데이터 소유권 |
| 공개 응답 변경 | 비교용 매핑과 호출자 호환 처리의 구분 | API 호환 계층 또는 클라이언트 마이그레이션 |
| 독립 데이터 저장소 도입 | 초기 적재·변경분·삭제 반영·시차와 복귀 준비 | 데이터 파이프라인·원천 소유자·복구 계약 |

- 호환 adapter가 필요한 경우: API·호출자 소유 계층에서 위치·책임·제거 조건 결정
- 비교용 정규화의 사용자 응답 자동 변환 금지
- 외부 route 전환 완료를 모든 내부 호출·데이터 의존의 종료로 간주 금지
- 관련 계약: [적용 범위](../product/scope-and-workflows.md), [데이터 책임](../_rules/identity-and-data-boundaries.md), [종료](../rollout/retirement.md)

## 서비스 선택 시 적용 조건

- AWS의 Strangler Fig 예제에 등장하는 Refactor Spaces와 패턴 자체를 구분
- Refactor Spaces: 2025-11-07부터 신규 고객 수용 중단, 기존 고객 이용 허용; [AWS 이용 조건 안내](https://docs.aws.amazon.com/migrationhub-refactor-spaces/latest/userguide/migrationhub-availability-change.html)
- 이용 자격이 확인되지 않은 계정에서 신규 구축 기본 후보로 선정하지 않음
- AWS 안내의 다른 현대화 서비스 명칭만으로 요청별 양쪽 응답 비교 기능까지 제공한다고 추정 금지
- 서비스 채택 전 확인: 실제 계정·리전의 이용 가능성, 제공 기능, 비용, 유지보수·종료 공지

## 적용 범위와 남은 결정

- 유지: 단일 serving 응답, 제한된 shadow, 비교·관측 분리, 수동 점진 전환과 종료
- 구현 전 결정: 실제 배치 위치·호환 처리·내부 의존과 도구의 응답 수집 능력
- 기술 선택: AWS·FastAPI·PostgreSQL의 후보 상태 유지
- 기존 인프라 재사용·배정 담당·실행 환경: [실행 구조](../infrastructure/deployment-and-stack.md)의 조건과 실제 환경을 바탕으로 결정
- 운영 적합성 판정: 첫 API 계약과 [실행 검증 계획](../validation/test-catalog.md)의 증거 확보 후 진행
