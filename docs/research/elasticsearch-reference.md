# Elasticsearch 마이그레이션 방식의 API 적용

- 아이디어 참고: @raeperd
- 목적: Elasticsearch 마이그레이션의 실행·관측·전환 원리를 일반 API에 맞게 재구성
- 기획 원칙: 필요한 원리만 적용하고 특정 제품·배포 방식의 필수 채택으로 확대하지 않음

## API 마이그레이션에 적용하는 원리

| 원리 | 현재 기획에 적용할 내용 | 적용 경계 |
| --- | --- | --- |
| 데이터 준비 → 운영 요청 검증 → 사용자 전환 | 준비 상태를 확인한 뒤 양쪽 실행·비교와 단계적 노출 진행 | S0~S6의 세부 상태·진입 조건은 현재 제품에서 정의 |
| 데이터 동기화와 읽기 검증 분리 | 초기 적재·변경분 반영은 데이터 계층, 읽기 관측은 프록시의 책임 | 쓰기 복제는 ACK·순서·부분 실패·복구 계약을 정한 뒤 별도 검토 |
| 비교 전 데이터 문맥 확인 | 원천·최신성·권한·검증 범위를 확인 | 개수 일치·일부 응답 일치만으로 전체 데이터 동일성 판정 금지 |
| backend 품질과 사용자 품질 분리 | latency·error·CPU·메모리와 실제 serving 사용자 지표를 각각 검증 | shadow 결과만으로 사용자 경험·전체 서비스 성공 보장 불가 |
| 응답 선택과 양쪽 실행량의 독립 제어 | serving 비율을 바꾸면서 필요한 shadow 실행량 유지 | 표본율·전환 비율·QPS는 실제 대상과 운영 목표로 결정 |
| 부하·cache 조건의 관측 | 동일 조건에서 실제 실행량·warm-up·지연 확인 | 설정 비율만으로 부하 재현·성능 검증 완료 판정 금지 |
| 전체 v2 → v1 shadow 종료 → v1 종료 | 사용자 전환·검색 중지·데이터 동기화 중지·리소스 종료를 구분 | v1 검색 중지와 쓰기 동기화·복귀 준비 종료를 동일시하지 않음 |
| 프록시 수명과 가용성의 독립 관리 | 직접 v2 경로·관측·우회를 검증한 뒤 임시 프록시 제거 | 배포 방식·추가 지연·복구 시간은 실제 환경에서 검증 |

## 데이터 준비·관측·전환의 적용 조건

### 준비 상태와 warm-up

- 프로세스 readiness와 실제 부하를 처리할 성능 준비 상태의 구분
- 전환 판단: 대표 요청의 표본 수·관측 기간·실행량·지연 분포를 함께 확인
- 데이터 노드의 warm-up 절차·수치·제어 구성: 일반 API에 그대로 적용하지 않고 대상 API의 준비 조건으로 별도 정의

### Elastic의 데이터 이전·스냅샷 계약

| 기술 계약 | API 적용 조건 |
| --- | --- |
| [데이터 이전](https://www.elastic.co/docs/manage-data/migrate): 원본 재적재·dual ingest·snapshot/restore·reindex 등 데이터·배포 방식별 방법 | 데이터 동기화는 프록시 밖에서 처리하고, 원천·파이프라인별 준비 증거 확보 |
| [Snapshot and restore](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore): shard별로 시작·종료 사이의 상태 포함 | snapshot 이름·시각만으로 전체 데이터의 동일 시점 상태 확정 금지 |
| snapshot 복원 시 snapshot·cluster·index 버전 호환 필요 | v1 복귀에 필요한 데이터·계약 호환성을 라우팅 변경과 별도로 확인 |

- 데이터 원천별 checkpoint·세대·가시성의 의미 정의
- Elasticsearch의 snapshot·index 버전 규칙을 PostgreSQL·일반 API의 동등성 기준으로 대체 금지

### AWS·OpenSearch Migration Assistant

| 기술 계약 | API 적용 조건 |
| --- | --- |
| [구성](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/architecture-overview.html): metadata·backfill·capture/replay·비교·cutover·복귀 기간 분리 | 데이터 준비·검증·사용자 전환·종료의 별도 상태 유지 |
| [Traffic Capture and Replay](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/traffic-capture-replay.html): source 전달 전 Kafka 기록, 별도 Replayer에서 대상 요청 재구성·변환·실행 | 내구성이 필요한 재생용 기록과 유실 가능한 비교 관측 큐의 구분 |
| [Backfill](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/running-backfill.html): snapshot 적재 후 count·문서별 실패 확인 | 작업 종료·HTTP 성공과 데이터 준비 완료의 구분 |
| [Traffic Replayer](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/using-traffic-replayer.html): backfill 후 replay, 조기 replay 시 delete 순서 역전 위험 | 쓰기 재생의 순서·초기 적재 계약을 읽기 shadow와 분리 |
| Replayer의 at-least-once 전달·연결별 요청 순서·source/target HTTP tuple 기록 | 중복·순서·재시도 계약이 필요한 별도 기능으로 분류; 일반 업무 쓰기 재생 가능성 추정 금지 |
| tuple 로그에 인증 헤더·HTTP 본문 포함 가능 | 허용 필드·마스킹·보존 정책에 따라 수집 범위 제한 |
| [Cutover](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/switch-traffic.html): 사전 검증과 source 복귀 경로 유지 | 전환 완료와 기존 시스템 종료·복구 수단 제거의 구분 |

- 적용 대상 구분: Migration Assistant의 OpenSearch 이전 절차와 본 제품의 실시간 v1/v2 읽기 실행·응답 선택·비동기 비교
- 재생 지연·시간 배율이 있는 비교와 같은 시점의 병렬 API 비교의 구분
- count 일치만으로 내용·삭제·권한 정합성 판정 금지
- 적용 순서: 소수 route로 시작하고 데이터 준비·용량·실패 증거에 따라 확대
- Kafka·EKS·workflow·snapshot 도입: 현재 MVP의 필수 구성에서 제외

## 설계 연결과 결정 대기 항목

| 설계·결정 항목 | 상세 문서 |
| --- | --- |
| 응답 제공 비율과 양쪽 실행량의 독립 제어 | [Shadow 적격성·표본](../shadow/eligibility-and-sampling.md), [용량·비용](../infrastructure/capacity-and-cost.md) |
| 실제 dispatch·종료·timeout·drop을 포함한 부하 검증 | [승격 gate](../rollout/stages-and-gates.md) |
| 데이터 준비·최신성·복귀 조건의 확인 | [권한·데이터 경계](../_rules/identity-and-data-boundaries.md), [롤백](../rollout/rollback-and-bypass.md) |
| 쓰기 복제·영속 재생·원자적 dual-write의 MVP 제외 | [적용 범위와 확장 조건](../product/scope-and-workflows.md) |
| 프록시 제거 전 직접 호출·관측 계약 검증 | [종료](../rollout/retirement.md) |
| full-shadow 필수 여부·coverage·관측 기간·운영 임계값 결정 | [용량·비용](../infrastructure/capacity-and-cost.md), [승격 gate](../rollout/stages-and-gates.md), [품질 요구사항](../validation/quality-and-acceptance.md) |

## 실제 적용 전 확인 사항

- 첫 대상 API의 쓰기 ACK·유실·재처리 계약, 비교 정책·실제 운영 통계 미확정
- 대상 route·공개 계약·데이터 원천·부작용·대표 트래픽·복귀 목표·보존 정책 정의
- 기술 채택 조건: 대상 API 적합성·라이선스·보안 설정·선택 버전별 지원 범위 확인
- 운영 검증: 대표 요청·부하·장애 실험으로 정합성·canary·롤백·복구 시간·비용 확인
