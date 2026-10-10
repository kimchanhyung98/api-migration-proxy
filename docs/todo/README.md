# 정리할 작업

## 사용자·세션·테넌트별 고정 배정 요구 제거

- 상태: 미착수. 해당 기능을 지원하지 않는다는 제품 범위는 확정됐다.
- 기준: 클라이언트는 기존 요청을 그대로 보내며, 프록시는 내부 요청 식별자를 사용해 요청별로 v1/v2를 선택한다. 같은 사용자의 다음 요청이 같은 backend로 배정된다는 보장은 하지 않는다.
- 현재 기본 CLI는 요청별 배정을 사용하지만, 기획·설정 모델·내부 연결점·테스트에는 신원별 고정 배정 지원이 남아 있다. 기본 CLI에서 사용하지 않는 것과 기능 정리가 완료된 것은 다르다.

### 정리 범위

- [D-06](../product/decisions.md), [FR-05와 배정 계약](../routing/serving-and-cohorts.md), [설정 예시](../routing/configuration.md), [공통 용어](../_rules/glossary.md)의 신원별 고정 배정 요구를 요청별 배정 기준으로 수정한다.
- [Shadow 적격성](../shadow/eligibility-and-sampling.md), [관측 분모](../observability/coverage-and-analysis.md), [전환 기준](../rollout/stages-and-gates.md), [설정 변경](../rollout/change-management.md), [복귀 절차](../rollout/rollback-and-bypass.md)에 전파된 신원 키·고정 집단 전제를 정리한다.
- [제품 개요](../product/overview.md), [도구 평가](../research/proxy-patterns-and-tools.md), [서비스 평가](../research/service-options-and-constraints.md)의 사용자 단위 배정 표현을 확인한다. 외부 제품의 기능 설명과 이 제품의 필수 지원 요건을 구분한다.
- [설정 모델](../../src/api_migration_proxy/routing/configuration.py), [선택 로직](../../src/api_migration_proxy/routing/selection.py), [요청 처리](../../src/api_migration_proxy/proxy/runtime.py)의 신원별 모드와 배정용 공급 함수 연결점을 정리한다. 관련 설정·단위·통합 테스트도 함께 수정한다.
- [검증 목록](../validation/test-catalog.md)의 T-06·T-07 및 연계 항목에서 사용자 고정 배정·재시작 간 동일 배정·연관 route 간 동일 배정 요구를 제거한다.

### 완료 기준과 유지할 기능

- 지원 설정과 문서 예시가 요청별 배정만 설명하며, 신원별 배정 모드를 선택할 수 없다.
- v2 비율 0·1과 중간 비율의 요청 분포, 한 요청에서 설정과 응답 출처의 일관성, serving 비율과 Shadow 비율의 독립성을 검증한다.
- 기존 인증 헤더·쿠키·본문 전달, 응답 비교의 권한·데이터 의미 판단, 시간 구간별 관측 집계는 유지한다. 이름에 사용자·신원·cohort가 있다는 이유로 함께 삭제하지 않는다.
- 이 문서 작성은 정리 작업의 완료를 뜻하지 않는다. 기획 본문과 제품 코드의 변경은 별도 작업이다.

## 운영 인프라 기능·서비스 조사와 연동

- 상태: 조사 결과와 후속 작업 정리. 실제 운영 배치·외부 관측 연동은 미완료이며, 이 문서 작성으로 배포나 서비스 도입을 시작하지 않는다.
- 운영 환경은 특정 클라우드·서비스·기술로 한정하지 않는다. 과거에 거론한 ECS, EC2, Lambda, ALB, CloudWatch, PostgreSQL, S3는 검토 사례이며, 채택 확정이나 우선순위를 뜻하지 않는다.
- 현재 Python/FastAPI 구현과 SQLite·PostgreSQL 저장 어댑터는 재사용할 수 있는 출발점이다. 이미 구현됐다는 이유로 다른 후보를 제외하지 않으며, 교체가 필요하면 추가 구현 범위와 이점을 함께 평가한다.

### 이전 대화에서 확인한 요구와 미정 사항

- 초기·로컬 임시 확인용 SQLite는 필수로 유지한다. 외부 관측 연동을 로컬 실행의 선행 조건으로 두지 않는다.
- 운영에서는 실제 메트릭 수집·조회·알림을 실행 인프라와 연결해야 한다. CloudWatch는 그 예시이며 다른 클라우드, 자체 운영 도구, SaaS도 검토한다.
- 프론트의 API 호출 주소는 변경할 수 있지만 v1 endpoint와 요청·응답 계약은 유지한다. v2와 Proxy는 독립 배치·확장을 고려하며, 기존 컨테이너 구성을 출발점으로 검토한다.
- 네트워크 관리 권한이 있는 환경과 없는 환경을 모두 고려한다. 권한이 있다는 이유로 기존 v1 VPC 재사용이나 내부 endpoint 추가까지 허용된 것으로 간주하지 않는다.
- 여러 Proxy와 안정된 도메인·endpoint를 고려한다. 실제 인스턴스 수, 리전, 공개·내부 진입 구성, 계정·권한과 예산은 미정이다.
- 보존 기간 안의 저장 완료 데이터는 배포·재시작·증설 후에도 조회할 수 있어야 한다. 메모리에 남은 미저장 이벤트의 유실 가능성과는 구분한다.

### 기능별 후보 조사

2026-10-08 기준 공식 자료를 확인했다. 아래의 제공 기능은 출처에 근거하며, 적용 조건과 추가 작업은 현재 프로젝트 구조에 대한 판단이다. 대표 후보를 비교한 목록으로, 선택 가능한 제품 전체를 제한하지 않는다. 실제 계정·리전 배포, 성능·비용 측정과 운영 적합성 검증은 후속 작업이다.

#### 실행 환경

| 후보군 | 제공 기능·공식 자료 | 프로젝트 적용 조건과 추가 작업 |
| --- | --- | --- |
| 가상머신·물리 서버 + Docker Compose | 단일 서버의 컨테이너 배치와 운영용 설정을 구성할 수 있다. [Compose 운영 배치](https://docs.docker.com/compose/how-tos/production/), [종료 유예](https://docs.docker.com/reference/compose-file/services/#stop_grace_period) | 현재 컨테이너 구성을 활용할 수 있다. 서버 관리, 영구 볼륨, 재시작·배포, 여러 호스트의 진입 분배·확장을 누가 맡을지 정한다. |
| 컨테이너 오케스트레이션: ECS, Kubernetes 등 | 컨테이너 실행·상태 확인·종료 절차를 구성한다. [ECS 상태 확인](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/healthcheck.html), [ECS task 설정](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html), [Kubernetes 종료](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) | 여러 Proxy의 배치 후보이다. 실행 자원·운영 책임을 비교하고, 종료 유예 안에 serving·Shadow·저장을 정리하는지 확인한다. ECS나 Fargate를 기본 선택으로 두지 않는다. |
| 관리형 컨테이너: Cloud Run, Azure Container Apps 등 | 컨테이너 실행과 수명 관리를 제공하지만 CPU 할당·백그라운드 실행·종료 조건은 서비스와 설정에 따라 다르다. [Cloud Run CPU·과금 모드](https://docs.cloud.google.com/run/docs/configuring/billing-settings), [실행 계약](https://docs.cloud.google.com/run/docs/container-contract), [Container Apps 백그라운드 작업](https://learn.microsoft.com/en-us/azure/architecture/best-practices/background-jobs#container-apps), [종료](https://learn.microsoft.com/en-us/azure/container-apps/application-lifecycle-management#shutdown) | client 응답 이후에도 남은 Shadow·저장 작업에 CPU가 제공되는지, 축소·재배포 때 작업이 끝나는지 확인한다. 컨테이너 이미지를 실행할 수 있다는 사실만으로 현재 작업 수명과 호환된다고 판단하지 않는다. |
| 함수 실행: Lambda 등 | 일반 Lambda 실행 환경은 invocation 완료 후 동결될 수 있다. [실행 환경 생명주기](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html) | 현재 서버 실행 방식의 교체 범위, client 응답과 invocation 종료 시점, 응답 후 잔여 작업을 검토한다. streaming·extension·별도 작업 실행을 검토한다면 각각의 수명과 비용을 따로 확인한다. 다른 함수 서비스도 해당 계약을 개별 조사한다. |

#### 진입 경로·트래픽 분배·복제

| 기능·후보 | 제공 기능·공식 자료 | 프로젝트 적용 조건과 추가 작업 |
| --- | --- | --- |
| 가중 분배: ALB weighted target groups, API Gateway REST API canary 등 | 설정한 비율로 요청을 대상 그룹 또는 production·canary에 분배한다. [ALB forward](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-action-types.html#forward-actions), [REST API canary](https://docs.aws.amazon.com/apigateway/latest/developerguide/canary-release.html) | 한 요청의 serving 선택 기능이다. 동일 요청의 양쪽 실행·응답 비교·이벤트 저장은 별도로 필요하다. 외부 분배와 Proxy 내부 분배를 함께 쓸 경우 비율의 적용 지점과 분모를 정한다. |
| 요청 복제: NGINX mirror, Envoy request mirroring 등 | NGINX는 백그라운드 subrequest를 만들고 응답을 무시한다. Envoy도 주 요청과 분리한 복제 요청을 지원한다. [NGINX mirror](https://nginx.org/en/docs/http/ngx_http_mirror_module.html), [Envoy mirror policy](https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/route/v3/route_components.proto#config-route-v3-routeaction-requestmirrorpolicy) | 요청 복제만으로 응답 쌍 비교가 완성되지 않는다. 본문 버퍼링, 헤더·Host 처리, 복제 실행 기한과 비교 결과 회수 방법을 검토한다. 기존 Proxy의 Shadow와 중복 실행되지 않게 책임을 정한다. |

이 기능들은 기존 Proxy와 함께 쓰거나 일부 역할을 대체하는 후보이다. 사용자·세션별 고정 배정은 위 TODO에서 제외한 기능이며, 외부 제품이 지원한다는 이유로 요구사항에 다시 추가하지 않는다.

#### 메트릭·로그·오류 분석·화면

| 기능·후보 | 제공 기능·공식 자료 | 프로젝트 적용 조건과 추가 작업 |
| --- | --- | --- |
| 관측 전송: OpenTelemetry SDK·Collector | Collector는 메트릭·로그·trace를 수신·처리하고 여러 목적지로 전송한다. [Collector](https://opentelemetry.io/docs/collector/) | 현재 내부 계측을 지원 신호로 내보내는 연결이 필요하다. Collector 자체를 이벤트 DB나 조회 화면으로 간주하지 않는다. |
| 메트릭: Prometheus | 수치 시계열 수집·질의와 알림 규칙을 제공하며 대표 수집 방식은 HTTP pull이다. [Prometheus 개요](https://prometheus.io/docs/introduction/overview/) | 현재 Proxy에는 수집용 HTTP 경로가 없다. 필요한 전송 방식과 접근 통제를 설계한다. 전체 요청의 원장이나 비교 이벤트 DB를 대체하지 않는다. |
| 조회 화면: Grafana | Prometheus, Loki, SQL 등 데이터 소스를 조회한다. [Grafana 데이터 소스](https://grafana.com/docs/grafana/latest/datasources/) | 수집·저장 경로를 먼저 연결하고 분모·revision별 화면과 알림을 구성한다. 설치만으로 Proxy 내부 메트릭이 수집되지는 않는다. |
| 클라우드 관측: CloudWatch, Google Cloud Monitoring, Azure Monitor | 운영 메트릭 수집·조회·알림을 제공한다. [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html), [Cloud Monitoring](https://docs.cloud.google.com/monitoring/docs/monitoring-overview), [Azure Monitor](https://learn.microsoft.com/en-us/azure/azure-monitor/fundamentals/overview) | 기존 운영 환경·권한·비용과 함께 비교한다. 컨테이너 로그 수집과 애플리케이션 사용자 정의 메트릭 전송을 구분하고, 지연 분포·재시작·여러 인스턴스 집계의 의미를 확인한다. |
| SaaS 관측: Datadog 등 | OpenTelemetry 계측과 Collector exporter를 통한 관측 데이터 수집을 지원한다. [OTel 메트릭](https://docs.datadoghq.com/metrics/open_telemetry/), [Collector exporter](https://docs.datadoghq.com/opentelemetry/setup/collector_exporter/install/) | SaaS 운영 편의와 전송량·보존 비용, 허용 필드와 목적지를 비교한다. Proxy의 내부 지표를 표준 신호로 변환하는 구현은 별도로 필요하다. |
| 로그 검색: Loki, Loggly 등 | Loki는 로그 집계·검색을, Loggly는 HTTP/S 단건·배치 로그 수집을 제공한다. [Loki 개요](https://grafana.com/docs/loki/latest/get-started/overview/), [Loggly 전송 API](https://documentation.solarwinds.com/en/success_center/loggly/content/admin/api-sending-data.htm) | 허용된 구조화 로그의 중앙 조회 후보이다. 로그 접수 성공을 고유 비교 이벤트의 최종 저장 확인으로 사용하려면 별도 계약·검증이 필요하다. |
| 오류 분석: Sentry 등 | SDK가 오류와 요청·실행 문맥을 수집한다. [Sentry 수집 데이터](https://docs.sentry.io/platforms/python/data-management/data-collected/) | Proxy 자체 오류 조사에 활용할 수 있다. 본문·query·인증 정보·지역변수의 자동 수집 설정과 실제 전송 필드를 확인한다. 비교 이벤트 저장과 전체 메트릭 수집의 대체 수단으로 간주하지 않는다. |

#### 이벤트 영속 저장·보관·전송 버퍼

| 기능·후보 | 제공 기능·공식 자료 | 현재 구현과 추가 작업 |
| --- | --- | --- |
| 로컬·단일 호스트 저장: SQLite | 파일 기반 DB로 단일 호스트의 적절한 부하에 사용할 수 있다. [SQLite 적합한 용도](https://www.sqlite.org/whentouse.html) | 현재 어댑터가 있으며 초기·로컬 확인에는 유지한다. 운영 사용은 영구 디스크·백업·쓰기 부하를 확인한다. 여러 호스트가 같은 DB 파일을 공유하는 구성을 전제로 하지 않는다. |
| 공유 SQL 저장: PostgreSQL 자체 운영 또는 관리형 서비스 | PostgreSQL은 백업·복구 방법을 제공하며 RDS, Cloud SQL, Azure Database에서도 운영할 수 있다. [PostgreSQL 백업](https://www.postgresql.org/docs/current/backup.html), [RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html), [Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/introduction), [Azure Database](https://learn.microsoft.com/en-us/azure/postgresql/overview) | 현재 PostgreSQL 어댑터를 재사용할 수 있는 후보군이다. 관리형 제공자 선택은 새 DB 엔진 도입과 구분한다. 연결·TLS·인증·지원 버전·pool·백업·복구를 검증한다. |
| 다른 SQL·키 값 저장: MySQL, DynamoDB 등 | MySQL InnoDB는 트랜잭션을, DynamoDB는 조건부 쓰기를 제공한다. DynamoDB TTL 만료 항목은 실제 삭제 전 조회될 수 있다. [InnoDB](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html), [DynamoDB PutItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html), [TTL 조회 처리](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ttl-expired-items.html) | 새 저장 어댑터가 필요하다. event ID 중복·내용 충돌·ACK 미확인, 조회 필터·결과 제한·보존 만료를 현재 저장·조회·보존 계약에 맞춰 구현·검증한다. |
| 객체 보관: S3, Cloud Storage, Azure Blob Storage 등 | 객체 저장·보관을 제공한다. [S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html), [Cloud Storage](https://docs.cloud.google.com/storage/docs/introduction), [Blob Storage](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blobs-introduction) | 백업·장기 보관 후보이다. 운영 이벤트 조회 저장소로 쓰려면 쓰기 어댑터, 검색용 색인·조회, 보존·삭제를 추가 설계한다. 객체 업로드만으로 현재 조회 계약이 충족되지는 않는다. |
| 선택적 내구성 큐·전송 버퍼 | 예를 들어 SQS Standard는 중복 전달 가능성이 있고, Collector는 전송 큐·재시도·디스크 보관을 구성할 수 있다. [SQS Standard](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues.html), [Collector 복원력](https://opentelemetry.io/docs/collector/resiliency/) | 저장 장애 중 미저장 데이터 보존 요구가 있을 때 검토한다. 업무 큐와 관측 전송 버퍼의 역할은 다르며, 둘 다 최종 이벤트 DB와 같은 계약이 아니다. 생산·소비·재처리와 최종 저장 확인이 추가로 필요하다. |

#### 배포 정의와 변경 관리

기존 배포 도구와 선언형 구성으로 요구를 충족할 수 있는지 먼저 확인한다. 인프라 생성·변경을 코드로 관리할 필요가 있으면 [Terraform provider](https://developer.hashicorp.com/terraform/registry/providers), [OpenTofu](https://opentofu.org/docs/v1.6/intro/), [Pulumi](https://www.pulumi.com/docs/iac/concepts/) 등을 비교한다. 특정 도구는 확정하지 않았으며, 선정할 경우 상태 저장·잠금·권한·비밀 관리·변경 검토·복구를 운영 절차에 포함한다.

### 후속 작업과 완료 기준

| 작업 | 확인·구현할 내용 | 완료 기준 |
| --- | --- | --- |
| 환경 제약 확인과 후보 선정 | v1의 실제 위치·접속 주소, 네트워크 변경 권한, 기존 진입·관측·저장 서비스, 담당자와 예산을 확인한다. 위 후보를 기능 충족, 추가 구현량, 운영 부담, 비용·보존·복구 조건으로 비교한다. | 선정 구성과 선택·제외 이유, 미정 사항을 기록한다. 필요한 후보만 검증하며, 모든 서비스의 PoC를 필수 과제로 만들지 않는다. |
| 실행 환경과 배포 구성 | 독립적인 v2·Proxy 배치·확장, 도메인, 설정·비밀 공급, 영구 저장 위치를 정하고 선택한 구성의 배포 정의를 작성한다. | 실제 대상 환경에서 진입 경로 → Proxy → v1/v2 전달과 재배포를 확인한다. 비용은 실제 트래픽·전송·보존량과 운영 부담에 근거해 산정한다. |
| 네트워크·TLS·상태 확인 | TLS 종료와 backend 인증을 연결한다. 전달 포트의 접근 범위와 Forwarded header의 생성·검증 책임을 정한다. 컨테이너 내부와 진입 계층의 상태 확인을 구분한다. | 요청·응답·인증 전달 계약과 DNS·인증서·접속 변경을 검증한다. 기존 loopback 전용 `/healthcheck`를 외부에 노출하지 않고 배포 환경의 상태 확인이 동작한다. |
| 이벤트 영속 저장 | 선택한 저장소에 기존 어댑터를 연결하거나 필요한 어댑터를 구현한다. 저장 확인·중복·조회·만료 계약과 보존·백업·복구 방법을 검증한다. | 재시작·재배포·증설 후 저장 완료 이벤트를 조회한다. 격리 환경에서 이미지 교체·이전 이미지 복귀 시 저장 형식 호환성과 복구를 확인한다. |
| 메트릭 수집·조회·알림 | 내부 계측을 선정한 수집 경로에 연결한다. 사용자 오류·지연, backend 역할별 실행, 비교·저장·유실 지표의 단위·유형·분모를 매핑한다. 인스턴스·관측 구간·revision별 조회와 알림을 구성한다. | 실제 요청·오류와 수집 값을 대조한다. 여러 인스턴스 합산, counter 초기화, 지연 분포와 중복·누락을 확인한다. 수집 중단을 정상 0건과 구분하고 알림 기준·담당자·통보 경로 및 실제 알림을 확인한다. |
| 설정 배포와 변경 기록 | 기본 CLI의 재시작·재배포 적용 방식에 맞춰 revision과 인스턴스별 적용 상태, 변경·복귀 시각과 사유를 기록한다. | 일부 적용 실패와 revision 혼재를 구분하고 실제 요청 분포로 적용·복귀 결과를 확인한다. CLI 재시작만으로 과거 변경 이력이 복원된다고 가정하지 않는다. |
| 종료·장애·우회 | 진입 요청 중지, 기존 연결과 serving·Shadow·저장 작업의 종료 순서·유예를 플랫폼과 맞춘다. Shadow 중지, v1 serving 복귀, Proxy 자체 우회를 각각 준비한다. | 응답 후 잔여 작업이 있는 상태에서 종료·축소·장애를 검증한다. 사용자 영향과 유실·미확인 범위를 기록하고, 실제 client의 Proxy 우회와 복구 시간을 확인한다. |
| 기획·검증 기준 정합성 | D-08, 인프라·기술 조사·관측 문서, NFR-08·T-26·T-47에 요구, 후보, 현재 구현, 검증 완료 범위를 구분해 반영한다. | 초기 SQLite 필수와 운영 관측 연동 필요가 일치한다. 현재 미연결을 영구적인 연동 금지로 설명하거나, 기존 서비스 예시를 채택 확정으로 설명하지 않는다. |

### 적용 시 유지할 경계

- 현재 메트릭은 프로세스 메모리에 있고 이벤트 저장소에는 선택된 비교 이벤트가 저장된다. 이벤트 DB 공유나 로그 수집 설정만으로 전체 메트릭 집계가 완료됐다고 판단하지 않는다.
- 현재 공개 `/metrics`나 연결된 외부 관측 SDK·exporter는 없다. 필요한 수집 경로를 선정한 환경에 맞춰 구현하며, 특정 endpoint·SDK·수집기·대시보드를 자동으로 필수화하지 않는다.
- 원문 요청·응답·인증 정보와 자유 형식 예외를 관측 전송 필드에 추가하지 않는다. 필드 허용 범위, 접근 권한과 보존 조건을 확인한다.
- 새 설정 서버·이력 DB·자동 승격·자동 롤백은 확정 과제가 아니다. 중대한 지연·고갈·교착 근거 없이 로컬 SQLite 선제 튜닝·교체나 브로커·별도 worker 도입으로 확대하지 않는다. 대표 부하·가용성·복귀 검증은 실제 운영 적용 단계에서 수행한다.
- 의미 비교의 권한·데이터 문맥은 대상 API 적용 작업이다. 인프라 배포나 관측 연결만으로 기본 CLI의 `not_comparable`이 해소되지는 않는다.

### 관련 문서

- [실행 구조와 저장소](../infrastructure/deployment-and-stack.md), [배포 설정](../../deploy/README.md), [현재 구현·연결 범위](../implementation/README.md)
- [메트릭과 분모](../observability/metrics.md), [운영 조회·알림](../observability/dashboards-and-alerts.md), [이벤트 조회·보존](../collection/query-and-retention.md)
- [설정 변경](../rollout/change-management.md), [복귀·우회](../rollout/rollback-and-bypass.md), [운영 검증](../validation/load-and-evidence.md), [D-08](../product/decisions.md)
