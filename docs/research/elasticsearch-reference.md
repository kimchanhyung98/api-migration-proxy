# Elasticsearch 마이그레이션 방식의 API 적용

이 문서는 @raeperd의 아이디어를 참고하여 Elasticsearch 마이그레이션의 실행·관측·전환 원리를 일반 API에 적용한 내용을 설명한다. 필요한 원리만 제품 설계에 반영하며, Elasticsearch나 아래에 소개한 도구·배포 방식의 채택을 요구하지 않는다.

serving, shadow, route의 의미는 [공통 용어](../_rules/glossary.md)를 따른다. 외부 자료의 데이터 이전 기능과 본 제품의 읽기 요청 실행·비교 책임을 구분해야 한다.

## API 마이그레이션에 적용하는 원리

| 원리 | 현재 기획에 적용할 내용 | 적용 경계 |
| --- | --- | --- |
| 데이터 준비 → 운영 요청 검증 → 사용자 전환 | 데이터 준비 상태를 확인한 뒤 양쪽 실행 결과를 비교하고, 그 증거로 사용자 노출 비율을 조정한다. | 단계 S0~S6와 각 단계의 진입 조건은 [전환 단계](../rollout/stages-and-gates.md)에서 정의한다. 외부 사례의 단계 이름을 그대로 적용하지 않는다. |
| 데이터 동기화와 읽기 검증 분리 | 초기 적재와 변경분 반영은 서비스·데이터 계층이 담당하고, 읽기 요청의 실행 관측은 프록시가 담당한다. | 쓰기 복제는 성공 ACK의 의미, 처리 순서, 부분 실패, 복구 계약을 정한 뒤 별도로 검토한다. 현재 읽기 프록시의 책임에 포함하지 않는다. |
| 비교 전 데이터 문맥 확인 | 비교 담당자는 데이터 원천, 최신성, 권한 의미, 검증 범위를 확인한다. | 데이터 개수나 일부 응답이 같다는 사실만으로 전체 데이터가 동일하다고 판정해서는 안 된다. |
| backend 품질과 사용자 품질 분리 | backend별 지연·오류·CPU·메모리와 실제 serving의 사용자 지표를 각각 확인한다. | shadow 결과만으로 실제 사용자 경험이나 전체 서비스의 성공을 보장해서는 안 된다. |
| 응답 선택과 양쪽 실행량의 독립 제어 | serving 비율을 변경하더라도 shadow 실행량은 별도 표본 정책과 자원 상한으로 제어한다. | 표본율, 전환 비율, 초당 요청 수(QPS)는 대상 API와 운영 목표에 따라 결정한다. |
| 부하·cache 조건의 관측 | 동일한 요청 분포와 cache 조건에서 실제 실행량, 준비 부하 실행(warm-up), 지연을 확인한다. | 설정 비율만으로 목표 부하를 재현했거나 성능 검증을 완료했다고 판정해서는 안 된다. |
| 전체 v2 → v1 shadow 종료 → v1 종료 | 전체 사용자 응답의 v2 전환, v1 읽기 실행 중지, 데이터 동기화 중지, 자원 종료를 별도 상태로 관리한다. | v1 읽기 실행의 중지가 쓰기 동기화의 중지나 v1 복귀 준비의 종료를 뜻하지 않는다. |
| 프록시 수명과 가용성의 독립 관리 | 직접 v2 호출 경로, 해당 경로의 관측, 프록시 장애 시 우회 경로를 확인한 뒤 임시 프록시를 제거한다. | 배포 방식, 프록시가 더하는 지연, 복구 시간은 대상 환경의 실행 증거로 판단한다. |

## 데이터 준비·관측·전환의 적용 조건

### 준비 상태와 warm-up

프로세스의 readiness는 요청 수락 준비 상태를 나타내며, 목표 부하를 처리할 성능이 확보됐다는 증거가 아니다. 마이그레이션 담당자는 전환 여부를 판단할 때 대표 요청의 표본 수, 관측 기간, 실제 실행량, 지연 분포를 모두 확인해야 한다.

warm-up은 목표 부하를 받기 전에 cache나 실행 자원이 요청 처리에 필요한 상태를 갖추도록 하는 준비 과정을 뜻한다. Elasticsearch 데이터 노드의 warm-up 절차와 수치를 일반 API에 그대로 적용해서는 안 된다. 대상 API의 의존성, cache 상태, 목표 부하에 맞는 준비 조건을 정해야 한다.

### Elastic의 데이터 이전·스냅샷 계약

| 기술 계약 | API 적용 조건 |
| --- | --- |
| [데이터 이전](https://www.elastic.co/docs/manage-data/migrate)은 원본 재적재, 양쪽 대상에 입력하는 dual ingest, snapshot/restore, reindex 등 데이터와 배포 방식에 따른 방법을 설명한다. | 데이터 동기화는 프록시 밖에서 수행한다. 데이터 담당자는 원천과 파이프라인별로 이전 완료 범위와 남은 차이를 확인할 증거를 확보해야 한다. |
| [Snapshot and restore](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore)의 snapshot은 각 shard에서 snapshot 시작부터 종료 사이의 상태를 포함한다. | snapshot 이름이나 대표 시각만으로 모든 데이터가 하나의 시점에 고정됐다고 판단해서는 안 된다. |
| snapshot을 복원하려면 snapshot, cluster, index의 버전이 호환돼야 한다. | v1 복귀에 필요한 데이터와 API 계약의 호환성을 라우팅 변경 가능 여부와 별도로 확인해야 한다. |

데이터 담당자는 원천별 checkpoint가 어느 변경까지 반영했는지, 데이터 세대가 어느 범위를 식별하는지, 반영한 변경이 API 조회에 언제 보이는지를 정의해야 한다. 서로 다른 시스템의 checkpoint 숫자가 같다는 이유로 동일한 데이터 상태라고 판단해서는 안 된다. Elasticsearch의 snapshot·index 버전 규칙을 PostgreSQL이나 일반 API의 데이터 동등성 기준으로 사용해서도 안 된다.

### AWS·OpenSearch Migration Assistant

| 기술 계약 | API 적용 조건 |
| --- | --- |
| [구성](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/architecture-overview.html)은 metadata, 초기 적재(backfill), 요청 기록·재생(capture/replay), 비교, 사용자 경로 전환(cutover), 복귀 기간을 구분한다. | 제품에서도 데이터 준비, 검증, 사용자 전환, 종료를 별도 상태로 관리한다. |
| [Traffic Capture and Replay](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/traffic-capture-replay.html)는 source 전달 전에 요청을 Kafka에 기록하고, 별도 Replayer에서 대상 요청을 재구성·변환·실행한다. | 재생을 위해 내구성이 필요한 요청 기록과 유실을 허용하는 비교 관측 큐를 구분해야 한다. 본 제품의 관측 큐를 재생용 내구 기록으로 해석해서는 안 된다. |
| [Backfill](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/running-backfill.html)은 snapshot 적재 후 데이터 개수와 문서별 실패를 확인하도록 한다. | 적재 작업 종료나 HTTP 성공만으로 데이터 준비 완료를 판정해서는 안 된다. |
| [Traffic Replayer](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/using-traffic-replayer.html)는 backfill 이후 replay를 수행한다. replay를 먼저 시작하면 delete의 적용 순서가 뒤집힐 위험이 있다. | 쓰기 재생의 순서와 초기 적재 계약은 읽기 shadow의 실행 계약과 별도로 정의해야 한다. |
| Replayer는 at-least-once 전달, 연결별 요청 순서, source/target의 HTTP tuple 기록을 제공한다. at-least-once는 같은 요청이 중복 전달될 수 있음을 포함한다. | 중복, 순서, 재시도의 계약이 필요한 재생 기능으로 분류한다. 이 지원 사실만으로 일반 업무 쓰기의 재생이 안전하다고 추정해서는 안 된다. |
| source/target의 요청·응답을 묶는 tuple 로그에는 인증 헤더와 HTTP 본문이 포함될 수 있다. | 수집 담당자는 허용 필드, 마스킹, 보존 정책에 맞춰 기록 범위를 제한해야 한다. |
| [Cutover](https://docs.aws.amazon.com/solutions/latest/migration-assistant-for-amazon-opensearch-service/switch-traffic.html)는 사전 검증과 source 복귀 경로 유지를 설명한다. | 사용자 경로의 전환 완료, 기존 시스템 종료, 복구 수단 제거를 같은 완료 상태로 처리해서는 안 된다. |

Migration Assistant의 대상은 OpenSearch 이전이다. 본 제품의 대상은 현재 들어온 요청을 v1/v2에 실행하고, 선택한 serving 응답을 반환하며, 양쪽 결과를 비동기로 비교하는 읽기 프록시다. 기록 시점과 다른 시점에 지연 또는 시간 배율을 적용해 재생한 결과를 요청 시점의 병렬 API 비교와 동일한 조건으로 해석해서는 안 된다.

데이터 개수가 같더라도 내용, 삭제 반영, 권한 의미가 같은지는 별도로 확인해야 한다. 적용은 소수 route에서 시작하고, 해당 범위의 데이터 준비·용량·실패 증거를 확인한 뒤 확대한다. Kafka, EKS, 외부 workflow, snapshot 기능의 도입은 현재 MVP의 필수 구성이 아니다.

## 설계 연결과 결정 대기 항목

아래 표는 이미 정의된 기능 계약과 서비스별로 값을 정해야 할 운영 항목을 연결한다. 운영값의 미정 상태가 해당 기능의 미구현을 뜻하지는 않는다.

| 설계·결정 항목 | 상세 문서 |
| --- | --- |
| 응답 제공 비율과 양쪽 실행량의 독립 제어 | [Shadow 적격성·표본](../shadow/eligibility-and-sampling.md), [용량·비용](../infrastructure/capacity-and-cost.md) |
| 실제 dispatch·종료·timeout·drop을 포함한 부하 검증 | [승격 gate](../rollout/stages-and-gates.md) |
| 데이터 준비·최신성·복귀 조건의 확인 | [권한·데이터 경계](../_rules/identity-and-data-boundaries.md), [롤백](../rollout/rollback-and-bypass.md) |
| 쓰기 복제·영속 재생·원자적 dual-write의 MVP 제외 | [적용 범위와 확장 조건](../product/scope-and-workflows.md) |
| 프록시 제거 전 직접 호출·관측 계약 검증 | [종료](../rollout/retirement.md) |
| 적격 요청 전체를 shadow로 선택하는 full-shadow 구간의 필요 여부, 커버리지 기준, 관측 기간, 운영 임계값 결정 | [용량·비용](../infrastructure/capacity-and-cost.md), [승격 gate](../rollout/stages-and-gates.md), [품질 요구사항](../validation/quality-and-acceptance.md) |

## 실제 적용 전 확인 사항

첫 대상 API의 비교 정책과 실제 운영 통계는 미확정이다. API 개발자, 데이터 담당자, 운영 담당자는 각 책임 범위에서 대상 route, 공개 계약, 데이터 원천, 부작용, 대표 트래픽, 복귀 목표, 보존 정책을 정의해야 한다. 쓰기 확장을 검토할 경우에는 아직 확정하지 않은 쓰기 ACK, 유실, 재처리 계약도 별도로 정해야 한다. 읽기 프록시의 현재 범위를 쓰기 복제로 확대하는 결정은 이 문서에 포함하지 않는다.

기술을 채택하기 전에는 대상 API와의 적합성, 라이선스, 보안 설정, 선택 버전의 지원 범위를 확인해야 한다. 운영 적합성은 대표 요청·부하·장애 조건에서 얻은 데이터 정합성, canary, 롤백, 복구 시간, 비용의 증거로 판단해야 한다. 증거가 없는 항목은 미확인으로 남겨야 하며, 외부 사례의 성공으로 대체해서는 안 된다.
