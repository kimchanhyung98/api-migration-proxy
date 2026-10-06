# 서비스·런타임 후보의 기능과 제약

이 문서는 서비스와 런타임의 지원 기능을 설명하고, 그 기능을 프록시에 적용할 때 확인해야 할 조건을 정리한다. Python/FastAPI와 HTTPX는 현재 구현에 사용하는 스택이다. 아래의 배포 서비스와 PostgreSQL은 후보이며, 운영 인프라·운영 저장소·관측 플랫폼의 선택은 미정이다.

지원 기능은 인용한 자료의 설명이다. 적용 조건과 검증 항목은 이 프로젝트의 판단 기준이다. 채택 전에는 선택한 제품·버전의 지원 범위, 계정·리전의 이용 가능성, 신규 이용 제한, 유지보수 상태를 확인해야 한다. 이 문서의 후보 비교를 서비스 채택 또는 운영 검증 완료로 해석해서는 안 된다.

아래의 검증 항목은 각 기술의 도입 검증 담당자에게 적용한다. 검증 항목의 나열 순서는 실행 순서를 뜻하지 않는다. serving, shadow, cohort, revision의 의미는 [공통 용어](../_rules/glossary.md)를 따른다.

## 후보의 역할 구분

| 후보 | 검토 역할 | 프로젝트에서 별도 확인할 경계 |
| --- | --- | --- |
| ALB weighted target groups | 진입 트래픽을 대상 그룹의 가중치에 따라 분배한다. | 그룹 하나의 선택과 동일 요청의 양쪽 실행·응답 비교를 구분해야 한다. |
| API Gateway REST API canary | 같은 stage의 production과 canary 사이에 트래픽을 분배한다. | 사용자별 안정 배정과 같은 요청의 응답 쌍 수집을 제공하는지 별도로 확인해야 한다. |
| ECS/Fargate | 프록시와 내부 큐를 실행할 컨테이너 환경의 후보이다. | 정상 종료, 강제 종료, 남은 작업의 유실을 구분해야 한다. |
| Lambda 일반 함수 실행 환경 | 함수 호출 단위로 실행하는 환경의 후보이다. | client 응답, 함수 호출(invocation) 완료, 실행 환경의 동결 시점을 구분해야 한다. |
| FastAPI·Starlette·HTTPX | 현재 HTTP 진입, 비동기 실행, 응답 전달을 구성한다. | 양쪽 실행의 병렬 시작, 역할별 실행 기한, 연결 해제, 자원 예산을 함께 확인해야 한다. |
| PostgreSQL | 비교 이벤트 저장·조회에 사용할 후보이다. | 고유 ID로 중복 행을 막는 것과 저장 확인 응답(ACK)이 없는 상황의 처리를 구분해야 한다. |

## ALB 가중 분배와 stickiness

### ALB 지원 기능

ALB는 AWS의 Application Load Balancer를 뜻한다. ALB의 `forward` 규칙은 복수 target group에 가중치를 지정하고, 규칙에 일치하는 요청을 그 가중치에 따라 분배한다. target group은 요청을 전달할 대상을 묶은 그룹이다. 가중 분배 대상 중 한 그룹이 비어 있거나 그 그룹의 모든 target이 unhealthy여도, ALB가 다른 정상 그룹으로 자동 failover하지 않는다.

target group stickiness는 후속 요청을 같은 그룹에 유지하는 기능이다. 사용 시 `AWSALBTG` 쿠키를 이용하므로 client가 후속 요청에 해당 쿠키를 보내야 한다. 가중치만 설정해서는 같은 client의 그룹 유지가 보장되지 않는다. [ALB listener action 공식 문서](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-action-types.html)

등록 해제 중인 target에는 신규 요청 전달을 중지하고, 진행 중인 요청·연결에는 등록 해제 유예 시간(deregistration delay)을 적용한다. 한편 등록된 target이 모두 unhealthy이면 해당 그룹의 unhealthy target에도 요청을 전달하는 fail-open 동작이 있다. 이 동작은 다른 target group으로 자동 전환하는 기능과 구분해야 한다. [ALB target group 속성](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html)

### ALB 적용 조건

가중 분배는 serving 노출 제어의 후보이다. `forward`는 선택한 그룹으로 요청을 보내며, v1/v2 양쪽 응답의 비교·수집은 별도로 연결해야 한다. 쿠키 기반 그룹 유지와 신뢰된 사용자·테넌트 키에 기반한 cohort는 서로 다른 배정 계약이다.

단순 비율 변경을 장애 target group의 자동 복귀 정책으로 해석해서는 안 된다. 또한 health check 실패나 readiness 변경만으로 신규 요청의 차단이 완료됐다고 판단해서는 안 된다. 운영 담당자는 등록 해제 상태와 실제 신규 요청 수락의 중지를 확인해야 한다.

### ALB 검증 항목

ALB 도입을 검토할 경우 다음 조건별 결과를 확인하십시오.

- 쿠키를 지원하지 않는 client, 쿠키가 만료된 client, 비율 변경 이후 요청의 실제 배정 분포를 확인하십시오.
- v2 target group이 비정상인 동안의 사용자 오류를 확인하십시오. 명시적으로 v1 복귀를 적용한 뒤의 요청 경로를 확인하십시오.
- 기존 stickiness가 유지되는 요청의 복귀 시간을 측정하십시오. 새 연결과 기존 연결의 동작 차이를 기록하십시오.
- target 등록 해제 중 신규 요청 유입과 진행 요청 완료를 확인하십시오. 모든 target이 unhealthy인 조건의 실제 요청 경로도 확인하십시오.

## API Gateway canary의 적용 범위

### API Gateway 지원 기능

공식 제품 비교표의 canary release deployment는 REST API가 지원하고 HTTP API는 지원하지 않는다. [REST API·HTTP API 기능 비교](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)

REST API canary는 같은 배포 stage의 production과 canary에 트래픽을 설정 비율에 따라 무작위로 분리한다. canary의 deployment ID, 트래픽 비율, stage 변수 덮어쓰기(stage variable override), stage cache 사용 여부를 설정할 수 있다. [API Gateway canary 공식 문서](https://docs.aws.amazon.com/apigateway/latest/developerguide/canary-release.html)

### API Gateway 적용 조건

REST API와 HTTP API를 구분하지 않고 “API Gateway가 canary를 지원한다”고 일반화해서는 안 된다. 무작위 요청 분배를 같은 사용자 또는 연관 route의 안정된 cohort 배정으로 해석해서도 안 된다.

canary 분배를 사용하더라도 v1/v2 동시 실행, 응답 비교, 이벤트 저장은 별도로 연결해야 한다. stage cache를 사용하면 진입 요청이 backend 실행으로 이어지지 않을 수 있으므로, API 진입 요청량과 backend 실제 실행량을 각각 확인해야 한다.

### API Gateway 검증 항목

- 선택한 API의 제품 유형, 통합 대상, stage 변수, cache 설정을 확인하십시오.
- API Gateway의 canary 배정과 프록시 배정을 함께 적용한다면, 두 배정이 겹치는 요청 경로를 확인하십시오. 각 지표의 분모를 확인하십시오.
- 기존 stage로 복귀한 뒤 실제 요청 경로를 확인하십시오. 비교 이벤트의 설정 revision이 기대한 복귀 상태와 일치하는지 확인하십시오.

## ECS/Fargate 종료와 작업 수명

### ECS/Fargate 지원 기능

ECS `StopTask`는 컨테이너에 stop signal을 전달하고 종료를 기다린 뒤, 유예 시간을 넘으면 강제 종료한다. 기본 signal은 `SIGTERM`이며 이미지의 `STOPSIGNAL`로 바꿀 수 있다. [ECS StopTask 공식 문서](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_StopTask.html)

Fargate Linux task의 `stopTimeout`은 미설정 시 기본 30초이며 최대 120초다. 지원되는 platform version 조건도 있으므로, 채택하는 버전에서 사용할 수 있는지 확인해야 한다. 이 수치는 서비스의 지원 범위이며 프록시의 운영 기본값이 아니다. [Fargate task definition 공식 문서](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html)

### ECS/Fargate 적용 조건

ECS/Fargate는 사용자 응답 이후에도 같은 프로세스의 제한된 shadow·비교 작업을 유지할 실행 환경의 후보이다. 컨테이너에서 실행한다는 사실만으로 메모리 큐의 내구성이나 남은 작업의 완료를 보장하지 않는다.

운영 담당자는 서버 종료 유예, shadow 최대 수명, 큐 배출 예산을 플랫폼의 강제 종료 시간 안에서 함께 검토해야 한다. `stopTimeout`의 최대값을 프로젝트의 기본 운영값으로 자동 채택해서는 안 된다.

ALB가 진행 중 HTTP 연결을 배출한 상태와 애플리케이션이 응답 이후의 shadow·큐를 배출한 상태는 별도 완료 조건이다. 진행 중 HTTP 요청이 없어도 내부 작업은 남을 수 있다. 등록 해제, stop signal, 애플리케이션 종료의 실제 순서와 각 시점의 잔여 작업 예산을 확인해야 한다. [ECS connection draining](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/load-balancer-connection-draining.html)

### ECS/Fargate 검증 항목

- 정상 배포 종료에서는 신규 요청 수락의 중지를 확인하십시오. 수락 중지 후에는 진행 중 serving의 처리 결과를 확인하십시오. 잔여 shadow와 큐의 제한된 배출 결과도 확인하십시오. 위 순서는 완료 상태의 확인 순서이며, backend 실행 순서가 아니다.
- serving 완료 후 느린 shadow 또는 큐 작업이 남은 상태에서 등록 해제, stop signal, 유예 만료의 동작을 확인하십시오. 연결 조기 종료로 발생한 사용자 오류와 관측 데이터의 유실을 구분하십시오.
- stop signal 미처리, 유예 초과, 강제 종료 조건에서 수집 유실을 기록하십시오. 관측 여부를 확정할 수 없는 구간을 기록하십시오.
- 인스턴스 또는 worker 프로세스 수가 바뀔 때 전체 shadow 실행량, 연결 수, DB pool 예산의 변화를 확인하십시오.

## Lambda invocation 종료 경계

### Lambda 지원 기능

일반 Lambda 실행 환경은 invocation이 완료된 뒤 동결될 수 있다. 함수 종료 시 끝나지 않은 background process나 callback은 환경을 재사용할 때 재개될 수 있다. 공식 지침은 코드 종료 전에 필요한 background 작업의 완료를 확인하도록 한다. [Lambda 실행 환경 생명주기](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html)

### Lambda 적용 조건

일반 handler가 반환한 뒤에도 응답 처리와 분리한 task가 계속 실행된다는 전제로 내부 큐나 shadow 설계를 적용해서는 안 된다. 채택 담당자는 실제 adapter와 runtime에서 client 응답 완료 시점이 Lambda invocation 완료 시점과 일치하는지 확인해야 한다.

response streaming, extension, 별도 비동기 실행은 현재 범위 밖의 대안이다. 채택을 검토한다면 작업 수명 계약과 구성 복잡도를 별도로 평가해야 한다. 다른 컨테이너 환경에서 응답 이후 작업이 실행됐다는 증거를 Lambda에서의 작업 완료 보장으로 확대해서는 안 된다.

### Lambda 검증 항목

- v1 serving이 빠르고 v2 shadow가 느리게 응답하는 조건에서 client 응답 이후 shadow의 종료를 확인하십시오. 결과의 저장 여부를 확인하십시오.
- 환경을 재사용하지 않는 조건, 함수 timeout, 강제 종료에서 남은 작업의 결과와 유실 여부를 확인하십시오.
- 잔여 작업을 완료하기 위해 handler 반환을 늦춘다면 사용자 지연과 실행 자원 사용량에 미치는 영향을 측정하십시오.

## FastAPI·Starlette의 응답 후 작업

### FastAPI·Starlette 지원 기능

FastAPI의 `BackgroundTasks`는 응답 이후 실행할 작업을 등록하는 기능이다. [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)

Starlette의 background task는 같은 프로세스에서 응답 전송 이후 실행된다. 복수 task는 등록한 순서대로 실행하며, 앞 task에서 예외가 발생하면 뒤 task는 실행하지 않는다. [Starlette Background](https://starlette.dev/background/)

### FastAPI·Starlette 적용 조건

shadow 호출 자체를 `BackgroundTasks`에만 등록하면 사용자 응답을 보낸 뒤 shadow가 시작한다. 이는 요청 시점에 양쪽을 병렬 실행하는 제품 요구와 일치하지 않는다. backend 실행을 시작하는 시점과 응답 이후 비교·저장이 계속될 수 있는 수명을 구분해야 한다.

background task를 등록했다는 사실을 제한된 큐, 동시 실행 제한, 독립 실행 기한, 내구성 저장의 대체 기능으로 해석해서는 안 된다. 정리 작업을 실패할 수 있는 비교·저장 task 뒤에만 등록해서는 안 된다. 예외와 취소 경로에서도 정리 작업이 실행되는지 각각 확인해야 한다.

### FastAPI·Starlette 검증 항목

- 실제 실행한 shadow의 시작 시점이 serving 응답 이전인지 확인하십시오. serving이 정상 완료했는데 shadow가 실행 중이라면, shadow가 허용한 실행 기한 안에서 계속되는지 확인하십시오.
- client 연결 종료, background 예외, 서버 종료 조건에서 task 정리 결과와 관측 기록을 확인하십시오.
- JSON 비교와 마스킹의 부하를 늘렸을 때 사용자 응답 지연과 메모리 사용량이 정한 예산을 충족하는지 확인하십시오.

## HTTPX 연결·streaming·timeout

### HTTPX 지원 기능

HTTPX는 `AsyncClient`를 재사용하여 connection pool을 활용할 수 있고, 비동기 streaming을 지원한다. 수동 streaming 모드의 호출자는 `Response.aclose()`로 응답을 닫을 책임이 있다. `aiter_raw()`는 content decoding 전의 바이트를 제공한다. [HTTPX Async Support](https://www.python-httpx.org/async/)

timeout은 connect, read, write, pool로 구분한다. read timeout은 전체 응답 완료 기한이 아니라 다음 데이터 chunk를 기다리는 시간의 제한이다. [HTTPX Timeouts](https://www.python-httpx.org/advanced/timeouts/)

`max_connections`, `max_keepalive_connections`, `keepalive_expiry`로 연결 pool의 개수와 유지 한도를 제어할 수 있다. [HTTPX Resource Limits](https://www.python-httpx.org/advanced/resource-limits/)

### HTTPX 적용 조건

serving과 shadow 각각의 전체 실행 기한(deadline)은 HTTPX의 단계별 timeout과 별도 계약으로 유지한다. 작은 chunk가 read timeout보다 짧은 간격으로 계속 도착하면 전체 응답 수명이 길어질 수 있으므로, read timeout만으로 총 실행 시간을 제한한다고 가정해서는 안 된다.

연결 pool 한도와 논리 요청 수, task 수, 비교 큐, 캡처 메모리의 한도는 각각 관리해야 한다. client를 공유하는 구성을 검토할 때에는 자원 효율뿐 아니라 serving과 shadow의 예산 격리를 함께 평가해야 한다.

사용자에게 전달할 원본 바이트와 비교를 위해 디코딩한 캡처를 구분해야 한다. 전달 body가 `Content-Encoding` 및 `Content-Length`의 의미와 일치하는지 확인해야 한다. 정상 완료, 파싱 실패, client 취소, 전체 기한 초과, 서버 종료의 모든 종료 경로에서 연결이 해제되는지도 확인해야 한다.

### HTTPX 검증 항목

- 느린 chunk 응답, pool 포화, 연결 timeout을 구분하여 기록하십시오. 각 조건에서 전체 실행 기한이 적용되는지 확인하십시오.
- 취소 또는 캡처 상한 초과가 반복된 뒤 연결, task, 메모리의 미정리 자원이 누적되지 않는지 확인하십시오.
- 압축 응답, 중복 헤더, 큰 body의 원문 전달을 확인하십시오. 일부만 수신한 응답을 완전한 응답의 정상 비교로 처리하지 마십시오.
- 공유 client가 저장한 cookie를 다른 호출자나 tenant의 요청에 재전송하지 않는지 [HTTP 전달 계약](../routing/http-forwarding.md)에 따라 확인하십시오.

## PostgreSQL 이벤트 중복 방지

### PostgreSQL 지원 기능

`INSERT ... ON CONFLICT`는 고유 제약이나 인덱스 충돌에 대해 `DO NOTHING` 또는 `DO UPDATE`를 지정할 수 있다. `DO UPDATE`는 독립적인 다른 오류가 없다면 동시 실행에서도 원자적인 insert 또는 update 결과를 보장한다. `RETURNING`은 실제로 insert 또는 update한 행을 기준으로 반환한다. [PostgreSQL 18 INSERT 공식 문서](https://www.postgresql.org/docs/18/sql-insert.html)

### PostgreSQL 적용 조건

PostgreSQL을 채택한다면 `event_id`의 고유 제약과 같은 ID의 재시도로 중복 행을 방지하는 방식을 검토한다. 충돌 시 동작은 요약 이벤트의 불변성과 업데이트 허용 여부에 따라 결정해야 하며, 기존 이벤트를 무조건 덮어써서는 안 된다.

`DO NOTHING RETURNING`의 반환 행이 없다는 사실만으로 저장 실패를 단정해서는 안 된다. 같은 ID의 기존 행이 있는지, 그 내용이 재시도한 이벤트와 같은지 확인해야 한다.

DB의 중복 행 방지, 네트워크를 통한 저장 확인 응답(ACK)의 수신, 관측 counter의 정확히 한 번 증가는 각각 다른 조건이다. ACK가 유실되면 저장소의 고유 행 확인 또는 정한 예산 안의 재시도로 결과를 확인해야 한다. 실제 저장 여부를 확인하기 전에는 미확인 상태를 유지해야 하며, 저장 성공이나 확정 실패로 임의 변환해서는 안 된다.

### PostgreSQL 검증 항목

- 같은 event ID의 동시 삽입, 저장 성공 후 ACK 유실, 배치 재시도에서 고유 행과 저장 결과를 확인하십시오.
- 내용이 다른 이벤트가 같은 ID로 들어왔을 때 충돌을 감지하는지 확인하십시오.
- 재시도 후에도 원래 보존 만료 시각을 유지하는지 확인하십시오. 저장 확인된 고유 이벤트 수인 [W](../observability/coverage-and-analysis.md)가 중복 증가하지 않는지 확인하십시오.
- 저장소 장애 중에도 serving이 저장 완료를 기다리지 않고 계약에 따라 진행되는지 확인하십시오.

## 검증 기준

관련 수용·회귀 기준은 [T-10·T-12·T-24·T-36·T-42](../validation/test-catalog.md)이다. 서비스를 채택하려면 실제 서비스 구성, 대표 부하, 통합 동작, 배포 종료, 운영 비용의 증거를 확보해야 한다. 지원 기능만 확인했거나 실제 환경의 증거가 없는 항목은 미확인으로 남겨야 한다.

각 조건의 제품 계약은 [실행 구조](../infrastructure/deployment-and-stack.md), [실행 수명](../shadow/lifecycle-and-limits.md), [비동기 적재](../collection/async-storage.md)를 따른다.
