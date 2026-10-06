# 내부 관측과 저장소·외부 서비스의 역할

프록시는 비교 이벤트 생성, 지표 집계와 데이터 보호를 내부에서 수행한다. 제품은 `/metrics`·`/state` HTTP 조회와 이를 활성화하는 설정을 제공하지 않는다. 내부 계측과 `metric_snapshot()`·`observation_status()` 인터페이스는 유지한다. 외부 관측 SDK와 exporter는 연결되어 있지 않다.

이 문서는 2026-10-06에 확인한 공식 자료와 프로젝트의 설계 판단을 구분한다. 외부 서비스의 기능 설명은 연동 구현이나 운영 검증 완료를 뜻하지 않는다. 채택할 제품·버전의 설정은 [기술 평가 기준](../_rules/technology-evaluation.md)에 따라 다시 확인해야 한다.

## 수집·집계·저장·조회는 서로 다른 책임이다

| 책임 | 프록시의 처리 범위 | 외부 도구와의 구분 |
| --- | --- | --- |
| 이벤트 생성 | backend 실행, 비교 판정과 수집 결과를 허용된 메타데이터로 만든다. | 이벤트 생성 자체에는 필요하지 않다. |
| 지표 집계 | 요청·비교·저장 건수, 오류·드롭과 지연 분포를 계산한다. | 내부 집계 자체에는 필요하지 않다. |
| 영속 저장 | 선택된 비교 이벤트와 허용된 상세 표본을 저장한다. | 여러 호스트가 같은 저장소를 사용할 때 서버형 DB를 검토한다. |
| 조회 | 내부 메트릭·상태 인터페이스와 이벤트 저장소의 제한된 조회 계약을 제공한다. 관측 HTTP 조회는 제공하지 않는다. | 여러 인스턴스의 통합 화면이나 알림은 관측 플랫폼의 일반적인 용도이며, 현재 제품의 연결 기능이 아니다. |
| 외부 관측 전송 | 관측 SDK나 exporter를 연결하지 않는다. | 외부 도구의 전송 기능과 현재 제품의 지원 범위를 구분한다. |

여기서 내부 수집기는 프록시의 비동기 작업과 저장 처리 기능이다. OpenTelemetry Collector나 CloudWatch Agent 같은 별도 프로그램을 뜻하지 않는다. 내부 계측은 항상 수행하며, HTTP 조회나 외부 전송의 활성화를 요구하지 않는다.

Prometheus는 계측 애플리케이션의 HTTP 경로를 주기적으로 읽어 시계열을 저장하는 도구다. `/metrics`는 그러한 조회 경로의 일반적인 예이며, 이 프록시가 제공하는 경로가 아니다. 프록시 내부의 카운터 계산과 SQLite 이벤트 저장에는 이 경로가 필요하지 않다. Prometheus 자체도 모든 요청의 완전한 기록이 필요한 용도의 저장소로 권장하지 않는다. [Prometheus 개요](https://prometheus.io/docs/introduction/overview/)

메모리에서만 집계한 수치는 프로세스 종료 시 사라진다. 보존된 이벤트로 다시 계산한 통계는 해당 저장 표본의 범위에 한정한다. 선택되지 않았거나 이벤트에 도달하기 전에 드롭된 요청은 이벤트 DB만으로 복원할 수 없다. 저장된 이벤트 수를 전체 요청 수로 해석해서는 안 된다. 분모와 누락 해석은 [커버리지와 분석](../observability/coverage-and-analysis.md)을 따른다.

## 기본 비공개와 데이터 최소 수집

요청량, 실패율과 내부 route 이름도 운영 정보를 드러낼 수 있다. 집계라는 이유만으로 공개해도 되는 데이터가 되지는 않는다. Prometheus 공식 보안 문서는 애플리케이션의 `/metrics`를 포함한 HTTP 엔드포인트를 적절한 보호 없이 인터넷 등 공개망에 노출하지 말라고 명시한다. [Prometheus 보안 모델](https://prometheus.io/docs/operating/security/)

프로젝트는 HTTP 메트릭·상세 상태 조회를 지원하지 않으며, 필요에 따라 다시 켜는 설정도 제공하지 않는다. 내부 메트릭·상태 인터페이스를 관측 HTTP 경로로 해석해서는 안 된다. 외부 도구의 접근 통제 기능이 이 제품에 HTTP 조회 기능을 추가하는 근거가 되지는 않는다.

| 데이터 | 기본 처리 원칙 |
| --- | --- |
| 비교 결과, 상태 분류, 소요 시간, 큐·저장 결과 | 관측 목적에 필요한 필드만 내부에서 수집한다. 현재 제품에는 외부 관측 전송 경로가 연결되어 있지 않다. |
| route와 메트릭 라벨 | 등록된 route 등 제한된 값 집합을 사용한다. 원본 URL이나 경로 변수 값을 라벨로 사용하지 않는다. |
| 요청·응답 본문, query string, 인증 헤더, cookie | 일반 로그와 메트릭에 넣지 않는다. 상세 표본은 별도 허용 필드·마스킹·보존 정책을 따른다. |
| request ID, 사용자 ID, tenant ID, event ID | 메트릭 라벨로 사용하지 않는다. 이벤트 상관관계에 필요한 식별자도 목적과 접근 범위를 따로 검토한다. |
| 예외 메시지, stack의 지역변수, diff 값·필드 경로 | 원문이나 식별자가 포함될 수 있으므로 자동 수집을 안전한 기본값으로 가정하지 않는다. |

라벨 조합마다 시계열이 생기므로 사용자 ID나 무제한 값 집합을 라벨로 사용하면 저장량이 크게 늘어난다. OpenTelemetry도 관측에 필요한 정보만 수집하고 집계 데이터로 대체할 수 있는지 검토하도록 권장한다. [Prometheus 라벨 지침](https://prometheus.io/docs/practices/naming/), [OpenTelemetry 민감정보 처리](https://opentelemetry.io/docs/security/handling-sensitive-data/)

stdout과 로컬 파일도 전송 경계를 확인해야 한다. 예를 들어 ECS의 `awslogs` 드라이버는 컨테이너 stdout·stderr를 CloudWatch Logs로 전달한다. 따라서 앱에서 외부 전송 코드를 호출하지 않았다는 사실만으로 로그가 로컬에 남는다고 판단해서는 안 된다. [ECS 로그 전달](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html)

제품별 암호화·접근 제어 기능은 이 프로젝트의 안전한 연동을 자동 보장하지 않는다. 연동 시에는 실제 전송 필드, 접근 주체, 전송 목적지, 보존·삭제와 배포 환경의 로그 전달 설정을 별도로 확인해야 한다. 상세 데이터의 제품 계약은 [상세 표본과 보호](../collection/detail-sampling.md)를 따른다.

## SQLite와 PostgreSQL의 선택 조건

SQLite는 로컬 개발뿐 아니라 짧은 쓰기를 순차 처리할 수 있는 단일 호스트 서버에서도 사용할 수 있다. DB 파일마다 동시에 한 writer만 허용하지만, 여러 프로세스가 접근할 수 없다는 뜻은 아니다. 운영 환경이라는 이유만으로 PostgreSQL로 교체할 필요는 없다. [SQLite 적합한 용도](https://www.sqlite.org/whentouse.html)

WAL은 읽기와 쓰기의 병행을 개선하지만 writer 수를 늘리지 않는다. WAL의 공유 메모리 제약 때문에 같은 DB를 사용하는 프로세스들은 같은 호스트에 있어야 한다. 네트워크 파일시스템에서 WAL DB를 공유하는 구성은 지원하지 않는다. [SQLite WAL](https://www.sqlite.org/wal.html)

| 조건 | 최소 구성의 판단 | 별도로 확인할 내용 |
| --- | --- | --- |
| 로컬 개발·검증 | SQLite를 사용한다. | 저장 파일 위치, 조회와 보존 정책을 확인한다. |
| 단일 호스트 운영 | 로컬 영구 저장소의 SQLite를 유지할 수 있다. | 쓰기 대기, 저장 지연, 디스크 용량, 백업과 재시작 후 조회를 확인한다. |
| 같은 호스트의 여러 worker | 짧은 트랜잭션을 직렬 처리할 수 있다면 SQLite를 검토할 수 있다. | worker별 메모리 지표가 자동 합산되지 않는 점과 쓰기 경합을 확인한다. |
| 여러 호스트가 중앙 이벤트 저장소에 직접 기록 | PostgreSQL 같은 서버형 DB를 검토한다. | 연결 수, 배치 쓰기, 장애 중 재시도와 고유 이벤트 저장 확인을 검증한다. |
| 필요한 쓰기량·저장 지연 목표를 SQLite가 충족하지 못함 | 대표 부하 결과를 근거로 서버형 DB 전환을 검토한다. | 고정된 HTTP RPS 수치를 일반적인 전환 기준으로 사용하지 않는다. |
| NFS 등 공유 파일시스템에 SQLite 파일을 두고 여러 호스트에서 접근 | 채택하지 않는 방향이다. | 파일 잠금·동기화 제약을 피하도록 서버형 DB 또는 파일이 있는 호스트에서만 접근하는 구조를 선택한다. |

SQLite 공식 지침은 네트워크 파일시스템의 잠금·동기화 차이로 문제가 생길 수 있다고 설명한다. PostgreSQL은 서버가 DB 파일을 관리하고 여러 클라이언트가 연결하는 구조이므로 중앙 저장소가 필요한 경우의 후보이다. [SQLite 네트워크 사용 지침](https://www.sqlite.org/useovernet.html), [PostgreSQL 구조](https://www.postgresql.org/docs/current/tutorial-arch.html)

DB 종류와 관계없이 큐 제출, 저장 확인, ACK 미확인을 구분해야 한다. 재시도와 중복 이벤트 처리의 계약은 [비동기 적재](../collection/async-storage.md)를 따른다. PostgreSQL의 고유 제약과 `ON CONFLICT` 적용 조건은 [서비스·런타임 후보](service-options-and-constraints.md#postgresql-이벤트-중복-방지)를 참고한다.

## 관측 도구와 서비스의 역할 비교

아래 표는 외부 도구의 일반 기능과 검토 목적을 비교한다. 현재 제품에 관측 HTTP 경로나 해당 도구의 SDK·exporter가 연결되어 있다는 뜻은 아니다. `EventStore`의 목적지만 바꾸는 하나의 설정으로 SQLite, Prometheus, Sentry와 Loggly를 같은 계약으로 교체할 수 있다고 가정해서는 안 된다.

| 도구·서비스 | 제공 기능 | 일반적인 검토 목적과 적용 경계 |
| --- | --- | --- |
| Prometheus | HTTP 수집과 수치 시계열 저장·질의를 제공한다. | 여러 인스턴스의 수치 추세·알림에 사용한다. 현재 프록시는 HTTP 수집 경로를 제공하지 않는다. 개별 비교 이벤트 저장을 대체하지 않는다. [공식 문서](https://prometheus.io/docs/introduction/overview/) |
| Grafana | Prometheus, Loki, SQL 등의 저장소를 조회해 화면을 구성한다. | 통합 화면이 필요할 때 선택한다. Grafana를 설치한 것만으로 프록시 로그가 수집되지는 않는다. [데이터 소스](https://grafana.com/docs/grafana/latest/datasources/) |
| Loki | 로그 저장·검색을 제공한다. | 여러 인스턴스의 안전한 구조화 로그를 검색할 때 검토한다. request ID 같은 값은 인덱스 라벨보다 구조화 메타데이터에 적합하다. [라벨 지침](https://grafana.com/docs/loki/latest/get-started/labels/) |
| OpenTelemetry Collector | 수신·변환·전송 파이프라인을 구성한다. | 선택한 연동의 프로토콜 변환과 전송 정책을 앱 밖에서 관리할 때 검토한다. 영속 이벤트 DB와 조회 화면을 제공하는 구성 요소는 아니다. [공식 문서](https://opentelemetry.io/docs/collector/) |
| CloudWatch Logs·Metrics | AWS의 로그와 지표 수집·조회 경로를 제공한다. | AWS 관측 환경을 사용할 때 검토한다. 로그 전달, 메트릭 변환과 비교 이벤트 저장의 의미를 각각 확인한다. [로그 전달](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html), [메트릭 변환](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/ContainerInsights-Prometheus-metrics-conversion.html) |
| Sentry | SDK가 오류와 진단 문맥 등을 수집한다. | 프록시 자체의 예외 분석을 보조할 때 검토한다. 일반 SDK 초기화를 비교 이벤트의 전수 저장 경로로 간주하지 않는다. [수집 데이터](https://docs.sentry.io/platforms/python/data-management/data-collected/) |
| Loggly | JSON 등 로그를 HTTP/S 또는 syslog로 수집한다. | 안전한 구조화 로그의 중앙 검색이 필요할 때 검토한다. 고유 event ID별 저장 확인 계약을 자동 제공한다고 가정하지 않는다. [로그 설정](https://documentation.solarwinds.com/en/success_center/loggly/content/admin/logging-setup.htm) |

Grafana k6는 부하·성능 시험 도구다. 상시 비교 이벤트 저장소나 내부 집계의 필수 구성 요소가 아니다. [k6 공식 문서](https://grafana.com/docs/k6/latest/)

K3s는 가벼운 Kubernetes 배포판이다. 로그 수집 도구가 아니며 이번 관측 기능 때문에 도입할 필요는 없다. [K3s 공식 문서](https://docs.k3s.io/)

## 외부 연동에서 확인할 차이

### CloudWatch

CloudWatch Agent의 Prometheus 메트릭 변환 경로는 counter, gauge와 summary를 지원하며 histogram은 버린다. 이는 해당 Agent 경로의 제약이다. 다른 CloudWatch 전송 경로도 같다고 일반화해서는 안 된다. 지연 분포를 전달하는 구성을 평가할 때에는 선택한 경로가 histogram 의미를 보존하는지 확인해야 한다. 현재 프록시는 이 Agent가 읽을 Prometheus HTTP 경로를 제공하지 않는다. [CloudWatch Agent 메트릭 변환](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/ContainerInsights-Prometheus-metrics-conversion.html)

CloudWatch Logs `PutLogEvents`는 HTTP 200 응답에도 `rejectedLogEventsInfo`로 일부 이벤트의 거부 정보를 반환할 수 있다. HTTP 상태만으로 배치 전체의 저장 완료를 판정해서는 안 된다. [PutLogEvents API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutLogEvents.html)

### Sentry

Sentry Python SDK 공식 문서는 전체 요청 URL과 기본 query string 수집, 크기·유형에 따른 JSON·form 본문 수집, 오류 발생 시 지역변수 수집을 설명한다. `send_default_pii=False`만으로 이 정보가 모두 차단된다고 판단해서는 안 된다. 선택한 SDK 버전에서 본문·지역변수·URL·query·헤더와 전송 전 필터를 함께 검토해야 한다. [Sentry 수집 데이터](https://docs.sentry.io/platforms/python/data-management/data-collected/)

`data_collection`을 사용하는 SDK에서는 일부 항목만 설정해도 나머지 항목에 더 허용적인 기본값이 적용된다. 이 설정은 함께 지정한 `send_default_pii`보다 우선한다. 따라서 부분 설정을 안전한 차단 구성으로 복사하지 말고, 수집 항목 전체와 실제 전송 payload를 확인해야 한다. [Sentry 설정](https://docs.sentry.io/platforms/python/configuration/options/#data_collection)

### Loggly

Loggly는 개별 이벤트와 줄 단위 배치용 HTTP/S 입력 경로를 제공한다. 전송 성공 응답과 검색 인덱스에서 이벤트를 확인한 상태는 구분해야 한다. 공식 문제 해결 문서도 전송 후 검색에 나타나기까지 시간이 걸릴 수 있다고 설명한다. [Loggly 전송 API](https://documentation.solarwinds.com/en/success_center/loggly/content/admin/api-sending-data.htm), [검색 반영 확인](https://documentation.solarwinds.com/en/success_center/loggly/content/admin/troubleshooting-rsyslog.htm)

### 표준 형식과 Collector를 사용하는 경우

일반적으로 Prometheus HTTP 경로를 제공하는 애플리케이션은 Collector의 Prometheus receiver로 수집할 수 있다. 현재 프록시에는 해당 경로가 없으므로 이 방식으로 내부 메트릭을 수집할 수 없다. JSON 로그 파일은 File Log receiver로 읽을 수 있다. File Log receiver는 파일을 읽는 구성 요소이며 앱 stdout을 자동으로 직접 읽는 기능은 아니다. 현재 공식 상태는 로그 지원 beta이며, 포함된 Collector 배포판과 버전을 확인해야 한다. [Collector 설정](https://opentelemetry.io/docs/collector/configuration/), [File Log receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/receiver/filelogreceiver/README.md)

Loki는 HTTP 기반 OTLP 로그 수신을 지원하므로 Collector의 OTLP HTTP exporter를 사용할 수 있다. 지원하는 신호·프로토콜·인증이 맞는 경우에는 Collector 설정으로 목적지를 연결할 수 있지만, 모든 서비스가 같은 신호와 저장 확인 계약을 제공하지는 않는다. [Loki OTLP 수신](https://grafana.com/docs/loki/latest/send-data/otel/)

Collector의 디스크 전송 큐도 무손실을 보장하지 않는다. 디스크 고장·공간 부족·재시도 한도 초과 시 손실될 수 있다. Collector가 데이터를 받았다는 사실을 프로젝트의 고유 이벤트 저장 ACK로 사용하려면 최종 저장소까지의 확인 계약을 별도로 정의해야 한다. [Collector 복원력](https://opentelemetry.io/docs/collector/resiliency/)

## 프로젝트 적용 판단

기본 흐름은 프록시 내부의 이벤트 생성, 제한된 비동기 처리, SQLite 저장과 내부 조회다. 저장 장애가 serving을 동기 대기시키지 않도록 하고, 저장 성공·실패·미확인과 감지된 드롭을 구분한다. 내부 계측과 조회 계약은 외부 관측 서비스 없이 유지한다.

여러 호스트의 중앙 이벤트 저장이 필요하면 서버형 DB를 검토한다. 통합 화면·알림과 중앙 로그 검색 도구의 일반 기능은 현재 제품의 연동 기능과 구분한다. 관측 HTTP를 다시 켜는 설정이나 연결된 외부 관측 SDK·exporter는 제공하지 않는다. 외부 도구의 기능 설명을 프록시 내부의 안전한 이벤트 생성과 저장 상태 판정 책임의 대체 근거로 사용해서는 안 된다.

이 문서에서는 서비스 계정을 생성하거나 운영 데이터를 전송하지 않았다. 외부 연동, 실제 운영 환경과 k6 부하 시험을 검증했다고 주장하지 않는다. 선택한 서비스의 채택과 검증 결과는 [부하 시험과 검증 증거](../validation/load-and-evidence.md)에 따라 별도로 기록한다.

[기술 자료](README.md)
