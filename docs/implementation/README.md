# 구현 구성

이 문서는 Python/FastAPI Proxy의 현재 코드에서 각 구성 요소가 담당하는 기능을 정의한다. 기능 계약 문서는 요구되는 동작과 판정 기준을 설명하고, 아래 표는 그 계약을 처리하는 코드의 위치를 연결한다.

## 표의 해석 범위

- `구현된 책임`은 해당 구성 요소가 수행하는 처리이다. 호출자가 공급해야 하는 정책·문맥·증거까지 구성 요소가 자동으로 확보한다는 뜻은 아니다.
- 전환 정책 모듈은 제공받은 gate 증거와 revision 보고를 평가한다. 이 모듈의 존재만으로 배포, worker 전체의 상태 수집 또는 동적 비율 변경을 수행하는 제어기가 연결된 것으로 해석해서는 안 된다.
- 조회·보존과 품질 집계의 기능 존재를 모든 실행 진입점에서 자동으로 노출하거나 주기적으로 수행한다는 의미로 해석해서는 안 된다. 해당 기능의 입력과 실행 주체를 구분해야 한다.
- 실제 운영 환경의 선정과 검증은 구성 요소의 구현 여부와 별개의 판단이다. 운영 적용 조건은 각 기능 계약과 [검증 기준](../validation/README.md)을 따른다.

## 기능별 구현

| 구성 요소 | 구현된 책임 | 기능 계약 |
| --- | --- | --- |
| [라우팅](../../src/api_migration_proxy/routing/) | route·정책·자원 상한 검증, 불변 설정 snapshot·revision 적용, cohort별 serving 배정과 목적지 매핑 | [라우팅·설정](../routing/README.md) |
| [요청 실행](../../src/api_migration_proxy/proxy/runtime.py)·[HTTP 전달](../../src/api_migration_proxy/proxy/transport.py) | 요청별 설정 고정, serving 응답 전달, 적격 shadow 실행과 역할별 timeout·취소·자원 제한 | [HTTP 전달](../routing/http-forwarding.md), [Shadow 실행](../shadow/README.md) |
| [응답 비교](../../src/api_migration_proxy/comparison/) | 응답 계약·비교 문맥 확인, 정규화, 일치·차이·실행 오류·비교 불가 판정 | [비교 계약](../comparison/README.md) |
| [비교 처리](../../src/api_migration_proxy/proxy/pipeline.py)·[수집](../../src/api_migration_proxy/collection/) | 제한된 비교·수집 큐, 요약·상세 보호, 배치·ACK·재시도·유실 구분, 조회·보존 처리 | [수집·보존](../collection/README.md) |
| [관측](../../src/api_migration_proxy/observability/) | 사용자·backend·비교·수집 계측, 지표 snapshot 집계와 커버리지 계산 | [계측·분모](../observability/README.md) |
| [전환 정책](../../src/api_migration_proxy/rollout/) | gate 증거 평가, revision 전파 상태와 복귀·종료 조건 판단 | [전환·복귀·종료](../rollout/README.md) |
| [애플리케이션](../../src/api_migration_proxy/app.py)·[실행 진입점](../../src/api_migration_proxy/cli.py) | 설정과 실행 자원 조립, HTTP 수명주기, 사용자 API 전달과 loopback 전용 health listener의 분리 | [실행 구조](../infrastructure/deployment-and-stack.md) |

## 기본 실행 진입점의 연결 범위

[기본 CLI](../../src/api_migration_proxy/cli.py)는 단일 프로세스의 HTTP 프록시, 내부 메트릭, 비교 처리, 이벤트 저장과 자동 만료 정리를 조립한다. 메트릭·상태 HTTP 조회와 이를 활성화하는 설정은 제공하지 않는다. 별도 loopback listener는 `/healthcheck` 하나만 제공한다. 아래 표는 이 진입점으로 실행했을 때의 동작이다. 라이브러리에서 제공하는 모든 기능이 기본 CLI에 연결되어 있는 것은 아니다.

기본 요청·응답 전달은 아래 공급 함수 없이 동작한다. 공급자(provider)는 프록시 코드에 전달하는 Python 함수이며 외부 서비스 연결을 뜻하지 않는다. 신원별 배정, 권한·데이터의 의미 비교, 업무 결과 분류 또는 상세 수집이 필요한 경우에 해당 함수를 제공한다. 비교 문맥이 없어도 요청·응답 전달과 설정에 따른 shadow 실행은 유지하며, 확인하지 못한 의미 비교는 `not_comparable`로 남긴다.

| 기능 | 기본 CLI의 동작 | 기능별로 필요한 입력·내부 함수 |
| --- | --- | --- |
| Serving·shadow | 등록 route의 request cohort, serving 비율과 shadow 정책을 적용한다. | 연결할 backend 주소, route 계약, 복제 안전성 검토 결과와 자원 예산을 설정한다. |
| 사용자·세션·테넌트 cohort | `identity_provider`가 없다. 필요한 신원 키를 얻지 못한 route는 v1 serving을 사용하고 shadow를 실행하지 않는다. | 신원별 배정이 필요하면 검증된 신원을 반환하는 함수를 `ProxyRuntime`에 전달한다. 클라이언트가 보낸 임의 header를 검증된 신원으로 간주하지 않는다. |
| 응답의 의미 비교 | `context_provider`가 없다. 권한·데이터 동등성을 확인하지 못한 정상 응답 쌍은 `not_comparable`이다. | 의미 일치를 판정하려면 대상 API의 권한·데이터 조건을 확인하는 함수를 제공한다. HTTP 성공이나 응답 일치만으로 동등성 값을 참으로 설정하지 않는다. |
| 업무상 성공·거절 분류 | route에 등록한 HTTP status 계약을 사용한다. | status만으로 판정할 수 없는 API는 `response_classifier`를 연결한다. |
| 상세 수집 | 상세 정책과 공급자를 연결하지 않아 요약만 수집한다. | 상세 수집이 필요하면 `detail_policy`, `detail_provider`와 허용 필드의 마스킹 함수를 함께 연결한다. |
| 배포·데이터 이력 | backend 배포 revision과 데이터 원천·revision·최신성 문맥은 `unknown`으로 저장한다. | 해당 기준으로 결과를 분석하려면 실제 메타데이터를 취득해 이벤트에 전달하는 코드를 추가한다. 비교 문맥 공급자만 연결해도 이 메타데이터가 자동 기록되는 것은 아니다. |
| 이벤트 저장·조회·보존 | 기본 SQLite 또는 명시한 PostgreSQL에 기록한다. 기본 60초 간격으로 만료 상세와 요약을 각각 최대 1,000개 정리한다. `purge-events`는 한 배치를 수동 정리한다. HTTP 이벤트 조회 API는 제공하지 않는다. | 저장 위치·DB 계정, 조회 권한, 보존 기간과 정리 처리량을 검증한다. 저장소 공유가 프로세스별 메트릭을 자동 합산하지는 않는다. |
| 관측·전환 | 현재 프로세스의 지표와 상태를 내부에서 집계한다. 지표·상태의 HTTP 조회 경로는 없다. 프로세스 내부의 `metric_snapshot()`과 `observation_status()`로 확인한다. 외부 전송 SDK는 연결하지 않는다. 전환 정책 평가와 실행 제어는 기본 CLI에 연결되어 있지 않다. | 복수 인스턴스의 지표·revision 집계와 관측 구간 마감은 별도로 연결한다. 기본 CLI에는 설정 변경 API나 파일 변경 감시 기능이 없으므로 변경된 설정은 프로세스 재시작·재배포로 적용한다. |

선택한 기능에 공급 함수가 필요하면 [ProxyRuntime](../../src/api_migration_proxy/proxy/runtime.py)의 해당 인자로 전달한다. 기본 CLI의 객체 조립 코드를 참고하여 서비스 진입점을 구성한다. 설정 파일에 신뢰 여부를 임의로 추가하는 것은 실제 신원·권한·데이터 검증을 대신하지 않는다.

## 배포 구성과 추가 구현의 구분

아래 항목은 현재 애플리케이션을 대상 실행 환경에 연결할 때 확인할 계약이다. 특정 클라우드나 운영 저장소를 선정한 것으로 해석해서는 안 된다.

| 항목 | 현재 지원 범위와 적용 조건 |
| --- | --- |
| 실행 설정 | 환경 파일보다 프로세스 환경변수를 우선하고, 명시한 CLI 인자를 가장 우선한다. JSON 설정을 선택하면 route·비교·자원 정책은 JSON에서 읽는다. listener, 이벤트 저장소와 자동 정리 등 실행 옵션은 별도로 적용한다. |
| 컨테이너 실행 | 서비스 실행 시 `serve` 하위 명령을 지정한다. 승인된 진입 경로의 API 요청을 받을 listener 주소를 설정한다. 이 listener를 인터넷에 직접 공개하지 않도록 배포 구성을 제한한다. 이미지의 실행 사용자는 UID 10001이며, 저장 경로에 대한 쓰기 권한과 필요한 영속 마운트를 배포 구성에서 제공한다. |
| 상태 확인 | 별도 loopback listener에 `/healthcheck` 하나만 등록한다. 요청 수락 준비 여부에 따라 200 또는 503과 `ready` 값만 반환한다. 외부 주소 바인딩과 외부 peer의 접근은 token 유무와 관계없이 거부한다. 메트릭·상태 조회 경로나 이전 health 경로의 별칭은 제공하지 않는다. |
| 진입 TLS | 기본 서버 진입점에는 TLS 설정이 없다. 기존 TLS 종료 계층에서 HTTP listener로 연결하거나 서버 구성을 확장한다. |
| Backend 연결 | 일반 HTTP와 기본 신뢰 인증서로 검증 가능한 HTTPS를 사용한다. 원래 요청의 인증 정보를 전달한다. 사설 CA·mTLS·상위 HTTP proxy·backend별 credential 변환이 필요하면 transport를 확장한다. 환경변수의 HTTP proxy 설정은 사용하지 않는다. |
| 준비 상태 | readiness는 유효한 설정을 가지고 새 요청을 받는 상태를 나타낸다. backend의 연결·권한과 저장소 쓰기 성공은 별도로 확인한다. |

구체적인 설정 공급은 [환경 설정](../../src/api_migration_proxy/environment.py)과 [JSON 설정](../../src/api_migration_proxy/settings.py), 서버 구성은 [실행 서버](../../src/api_migration_proxy/server.py)를 따른다. HTTP 연결의 지원 범위는 [BackendTransport](../../src/api_migration_proxy/proxy/transport.py)에 구현되어 있다. 저장소·관측 관련 실행값은 [배포 안내](../../deploy/README.md#이벤트-저장소-선택)를 따른다. 실제 환경의 처리량, 종료 시간과 복귀 조건은 [검증 기준](../validation/README.md)에 따라 확인한다.

## 적용 기준

구성 요소의 책임이나 완료 범위를 확인할 때에는 다음 순서로 읽는다.

1. [사용자 흐름](../product/scope-and-workflows.md)에서 대상 API의 적용 범위를 확인하십시오.
2. 위 표에서 수행하려는 작업의 구성 요소와 기능 계약을 확인하십시오.
3. 여러 구성 요소를 거치는 작업이면 다음 구성 요소로 전달되는 조건을 확인하십시오.
4. [검증 기준](../validation/README.md)에서 해당 기능의 수용 조건과 회귀 시나리오를 확인하십시오. 구현 위치의 존재는 수용 조건 충족의 증거가 아닙니다.
5. 비율 변경, 복귀 또는 최종 종료를 판단하면 [전환 관리](../rollout/README.md)의 해당 절차와 완료 조건을 적용하십시오.

[전체 문서](../README.md)
