# 서비스·런타임 후보의 기능과 제약

- 목적: 서비스별 지원 기능과 프록시 적용 조건 정리
- 기술 후보: AWS·FastAPI·HTTPX·PostgreSQL; 채택 전 대상 API와 운영 환경에서 검증
- 채택 시 확인: 제품·버전별 지원 범위, 리전·신규 이용 제한, 유지보수 상태

## 후보의 역할 구분

| 후보 | 검토 역할 | 프로젝트에서 별도 확인할 경계 |
| --- | --- | --- |
| ALB weighted target groups | 진입 트래픽의 가중 분배 | 동일 요청의 양쪽 실행·응답 비교와의 구분 |
| API Gateway REST API canary | stage 내 production·canary 트래픽 분배 | 사용자별 안정 배정·paired response 수집 여부 |
| ECS/Fargate | 프록시·내부 큐를 실행할 컨테이너 환경 | 정상 종료·강제 종료·남은 작업의 유실 |
| Lambda 일반 함수 실행 환경 | 호출 단위 실행 후보 | 응답 반환과 invocation 종료·환경 동결 경계 |
| FastAPI·Starlette·HTTPX | HTTP 진입·비동기 실행·응답 전달 | 병렬 시작·독립 deadline·연결 해제·자원 예산 |
| PostgreSQL | 비교 이벤트 저장·조회 후보 | 고유 ID 중복 방지와 저장 ACK 미확인 처리 |

## ALB 가중 분배와 stickiness

### ALB 지원 기능

- `forward` 규칙의 복수 target group에 가중치 지정 가능, 일치 요청을 해당 가중치에 따라 분배
- weighted target group 중 하나가 비어 있거나 모두 unhealthy인 경우 다른 정상 target group으로 자동 failover하지 않는 동작
- target group stickiness 사용 시 `AWSALBTG` 쿠키로 그룹 유지, 후속 요청에서 client의 쿠키 전달 필요
- 가중치 설정만으로 sticky session 보장 불가. [ALB listener action 공식 문서](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-action-types.html)
- 등록 해제 중인 target에는 신규 요청 전달 중지, 진행 중 요청·연결에는 deregistration delay 적용
- 등록된 target이 모두 unhealthy인 경우 해당 그룹의 unhealthy target에도 요청을 보내는 fail-open 동작 존재. [ALB target group 속성](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-target-group-attributes.html)

### ALB 적용 조건

- 가중 분배: serving 노출 제어 후보
- `forward`의 역할: 선택한 그룹으로 요청 분배; v1/v2 양쪽 응답의 비교·수집은 별도 구현 필요
- 쿠키 기반 그룹 유지와 신뢰된 사용자·테넌트 키 기반 cohort를 서로 다른 배정 계약으로 취급
- 단순 비율 변경을 장애 target group의 자동 복귀 정책으로 간주 금지
- health check 실패·readiness 변경만으로 신규 요청 차단 완료를 보장하지 않음; 등록 해제와 실제 요청 수락 중지 확인

### ALB 검증 항목

- 쿠키 미지원 client·쿠키 만료·비율 변경 시 실제 배정 분포
- v2 target group 비정상 시 사용자 오류와 명시적 v1 복귀 결과
- 기존 stickiness가 유지되는 요청의 복귀 시간, 새 연결·기존 연결의 동작 차이
- 등록 해제 중 신규 요청 유입·진행 요청 완료와 모든 target이 unhealthy인 상황의 실제 요청 경로

## API Gateway canary의 적용 범위

### API Gateway 지원 기능

- 공식 제품 비교표 기준 canary release deployment: REST API 지원, HTTP API 미지원. [REST API·HTTP API 기능 비교](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)
- 동일 stage의 production·canary로 트래픽을 설정 비율에 따라 무작위 분리
- canary별 deployment ID·트래픽 비율·stage variable override·stage cache 사용 여부 설정 가능. [API Gateway canary 공식 문서](https://docs.aws.amazon.com/apigateway/latest/developerguide/canary-release.html)

### API Gateway 적용 조건

- 지원 제품 유형을 특정하지 않은 채 “API Gateway canary 지원”으로 일반화 금지
- 무작위 요청 분배를 동일 사용자·연관 route의 안정된 cohort 보장으로 간주 금지
- canary 트래픽 분배와 별도로 v1/v2 동시 실행·응답 비교·이벤트 저장 필요
- stage cache 사용 시 backend 실제 실행량과 API 진입 요청량의 차이를 별도 확인

### API Gateway 검증 항목

- 현재 API 제품 유형·통합 대상·stage 변수·캐시 설정 확인
- canary 배정과 프록시 배정을 함께 적용할 경우 이중 분배·관측 분모 확인
- 기존 stage 복귀 이후 실제 요청 경로와 비교 이벤트의 설정 revision 일치 여부

## ECS/Fargate 종료와 작업 수명

### ECS/Fargate 지원 기능

- ECS `StopTask`: 컨테이너 stop signal 전달 후 종료 대기, 유예 초과 시 강제 종료
- 기본 stop signal은 `SIGTERM`, 이미지의 `STOPSIGNAL`로 변경 가능. [ECS StopTask 공식 문서](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_StopTask.html)
- Fargate Linux task의 `stopTimeout`: 미설정 기본 30초, 최대 120초, 지원 platform version 조건 존재. [Fargate task definition 공식 문서](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html)

### ECS/Fargate 적용 조건

- 요청 응답 이후에도 같은 프로세스의 제한된 shadow·비교 작업을 유지하는 실행 후보
- 컨테이너 실행만으로 메모리 큐의 내구성·작업 완료 보장 불가
- 서버 종료 유예·shadow 최대 수명·큐 배출 예산과 플랫폼 강제 종료 시간을 함께 설계
- ALB의 HTTP 연결 배출과 애플리케이션의 응답 후 shadow·큐 배출을 별도 완료 조건으로 관리
- 진행 중 HTTP 요청이 없어도 내부 작업은 남을 수 있음; 등록 해제·stop signal·애플리케이션 종료의 실제 순서와 잔여 작업 예산 확인. [ECS connection draining](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/load-balancer-connection-draining.html)
- `stopTimeout` 최대값을 프로젝트 기본 운영값으로 자동 채택 금지

### ECS/Fargate 검증 항목

- 정상 배포 종료: 신규 수락 중지 → 진행 중 serving 처리 → 잔여 shadow·큐의 제한된 배출
- serving 완료 후 느린 shadow·큐가 남은 상태에서 등록 해제·stop signal·유예 만료 검증; 진행 중 연결의 조기 종료 오류와 수집 유실 구분
- stop signal 미처리·유예 초과·강제 종료 시 수집 유실과 관측 미확인 구간
- 인스턴스·worker 프로세스 증감에 따른 총 shadow 실행·연결·DB pool 예산 변화

## Lambda invocation 종료 경계

### Lambda 지원 기능

- 일반 Lambda 실행 환경: invocation 완료 후 동결 가능
- 함수 종료까지 완료되지 않은 background process·callback: 환경 재사용 시 재개 가능
- 공식 지침: 코드 종료 전 필요한 background 작업 완료 확인. [Lambda 실행 환경 생명주기](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html)

### Lambda 적용 조건

- 일반 handler 반환 후 detached task가 계속 실행된다는 전제로 내부 큐·shadow 설계 적용 금지
- client 응답 완료와 Lambda invocation 완료의 일치 여부를 실제 adapter·runtime에서 확인
- response streaming·extension·별도 비동기 실행: 현재 범위 밖의 대안으로, 채택 전 수명 계약·복잡도 평가 필요
- 컨테이너에서 확인한 응답 후 작업 동작을 Lambda 완료 보장으로 확대 금지

### Lambda 검증 항목

- v1 빠른 응답·v2 느린 응답 조합에서 client 반환 이후 shadow 종료·저장 확인
- 환경 재사용 없음·함수 timeout·강제 종료에서 잔여 작업 처리
- 잔여 작업 완료를 위해 handler 종료를 늦추는 경우 사용자 지연·실행 자원 영향

## FastAPI·Starlette의 응답 후 작업

### FastAPI·Starlette 지원 기능

- FastAPI `BackgroundTasks`: 응답 이후 실행할 작업 등록 기능. [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)
- Starlette background task: 같은 프로세스에서 응답 전송 이후 실행
- 복수 background task는 순서대로 실행, 앞 작업 예외 발생 시 이후 작업 미실행. [Starlette Background](https://starlette.dev/background/)

### FastAPI·Starlette 적용 조건

- shadow 호출을 `BackgroundTasks`에만 등록하면 사용자 응답 이후 시작하는 구조; 요청 시점 양쪽 병렬 실행 요구와 불일치
- backend 실행 시작과 응답 후 비교·저장의 수명을 구분하여 설계
- 단순 background task 등록을 제한 큐·동시 실행 제한·독립 deadline·내구성 저장의 대체 기능으로 간주 금지
- 정리 작업을 실패 가능한 비교·저장 작업 뒤에만 연결하지 않도록 예외·취소 경로 확인

### FastAPI·Starlette 검증 항목

- serving 응답 이전 shadow 시작 여부, serving 정상 완료 뒤 shadow 유지 여부
- client 연결 종료·background 예외·서버 종료에서 task 정리와 관측 결과
- JSON 비교·마스킹 부하 증가 시 사용자 응답 지연과 메모리 변화

## HTTPX 연결·streaming·timeout

### HTTPX 지원 기능

- `AsyncClient` 재사용을 통한 connection pool 활용 가능
- async streaming 지원, 수동 streaming 사용 시 `Response.aclose()` 호출 책임 존재
- `aiter_raw()`는 content decoding 전 바이트 제공. [HTTPX Async Support](https://www.python-httpx.org/async/)
- timeout 구분: connect·read·write·pool; read timeout은 다음 데이터 chunk 수신 대기 기준. [HTTPX Timeouts](https://www.python-httpx.org/advanced/timeouts/)
- `max_connections`·`max_keepalive_connections`·`keepalive_expiry`로 pool 한도 제어 가능. [HTTPX Resource Limits](https://www.python-httpx.org/advanced/resource-limits/)

### HTTPX 적용 조건

- role별 전체 실행 deadline과 HTTPX의 단계별 timeout을 별도 계약으로 유지
- 작은 chunk가 계속 도착하는 응답의 총 수명이 read timeout만으로 제한된다고 가정 금지
- connection pool 한도와 논리 요청·task·비교 큐·캡처 메모리 한도를 각각 관리
- 원본 바이트 전달과 디코딩된 비교 캡처를 구분; body와 `Content-Encoding`·`Content-Length`의 의미 일치 검증
- 정상 완료·파싱 실패·client 취소·deadline·서버 종료 경로에서 연결 해제 확인
- 공유 client의 자원 효율과 serving/shadow 격리 예산을 함께 평가

### HTTPX 검증 항목

- 느린 chunk 응답·pool 포화·연결 timeout의 분류와 총 deadline 적용
- 반복 취소·캡처 상한 초과 이후 연결·task·메모리 누적 여부
- 압축·중복 헤더·큰 body의 원본 전달, 부분 수신의 정상 비교 방지
- 공유 client의 cookie 저장·재전송이 다른 호출자·tenant 요청에 섞이지 않는지 [HTTP 전달 계약](../routing/http-forwarding.md) 검증

## PostgreSQL 이벤트 중복 방지

### PostgreSQL 지원 기능

- `INSERT ... ON CONFLICT`: 고유 제약·인덱스 충돌에 대해 `DO NOTHING` 또는 `DO UPDATE` 지정 가능
- `DO UPDATE`: 독립 오류가 없는 경우 동시성 아래 원자적 insert 또는 update 결과 보장
- `RETURNING`: 실제 insert·update된 행 기준 반환. [PostgreSQL 18 INSERT 공식 문서](https://www.postgresql.org/docs/18/sql-insert.html)

### PostgreSQL 적용 조건

- `event_id` 고유 제약과 동일 ID 재시도를 활용한 중복 행 방지 후보
- 요약 이벤트의 불변성·업데이트 허용 여부에 따라 충돌 동작 결정; 무조건 기존 이벤트 덮어쓰기 금지
- `DO NOTHING RETURNING`의 빈 결과를 저장 실패로 단정 금지; 기존 동일 ID의 존재·내용 확인 필요
- DB 중복 행 방지와 네트워크 ACK 수신·counter의 정확히 한 번 증가는 별개
- ACK 유실 시 저장소 고유 행 확인과 재시도 예산을 적용, 실제 저장 여부 미확인 상태 보존

### PostgreSQL 검증 항목

- 같은 event ID의 동시 삽입·저장 성공 후 ACK 유실·배치 재시도
- 다른 내용이 같은 ID로 들어오는 오류의 감지
- 재시도 후 보존 만료 시각 유지, W 중복 증가 방지, 저장 장애 중 serving 지속

## 검증 계획

- 구현 후 검증: [T-10·T-12·T-24·T-36·T-42](../validation/test-catalog.md)
- 채택 조건: 실제 서비스 구성·대표 부하·통합 동작·배포 종료·운영 비용 검증
- 관련 설계: [실행 구조](../infrastructure/deployment-and-stack.md), [실행 수명](../shadow/lifecycle-and-limits.md), [비동기 적재](../collection/async-storage.md)
