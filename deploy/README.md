# 애플리케이션 배포 설정

[compose.yaml](../compose.yaml)은 애플리케이션 한 인스턴스의 실행 예시이다. 현재 backend 주소는 더미로 유지한다. 코드 동작은 로컬 합성 backend로 검증하며, 실제 backend 연결과 AWS 등 운영 인프라의 선정·설정·검증은 별도 작업이다. 실제 배포 시 실행 용량과 가용성은 대상 환경의 부하로 판단한다.

## 적용한 기본값

| 항목 | 설정 |
| --- | --- |
| 초기 전달 | 모든 요청을 v1으로 전달한다. 등록 route가 없으므로 v2 serving과 shadow는 실행하지 않는다. |
| Backend 더미 주소 | v1은 `http://127.0.0.1:8001`, v2는 `http://127.0.0.1:8002`이다. 컨테이너 내부 주소이며 실제 backend를 생성하지 않는다. |
| 사용자 요청 | 컨테이너의 `0.0.0.0:8080`을 호스트 `127.0.0.1:8080`에 연결한다. 호스트의 접근 제한된 기존 TLS 종료 계층이 이 포트로 전달한다. 프록시 포트를 인터넷에 직접 공개하지 않는다. |
| 내부 관측 | 메트릭은 프로세스 내부에서 집계한다. `/metrics`·`/state`와 이를 활성화하는 설정은 제공하지 않는다. 별도 수집기나 SaaS를 요구하지 않는다. |
| Health 접근 | 컨테이너 내부 `127.0.0.1:9090`만 사용하며 호스트에 공개하지 않는다. 컨테이너 healthcheck는 이 주소의 `/healthcheck`를 사용한다. |
| 저장 | UID 10001로 `/data/events.sqlite`에 기록한다. 새 데이터 디렉터리는 `0700`, DB 파일은 `0600`이다. Compose의 `event-data` volume을 사용하여 컨테이너 재생성 후에도 유지한다. |
| 자원 상한 | CPU 1개, 메모리 512 MiB, 프로세스·스레드 128개, 임시 파일 64 MiB를 시작값으로 사용한다. |
| 종료·재시작 | 종료 유예는 30초이며 `unless-stopped`로 재시작한다. healthcheck 실패만으로 재시작하는 것은 아니다. |
| 파일·로그 | 루트 파일시스템은 읽기 전용이다. 쓰기는 데이터 volume과 임시 파일 영역에서 수행한다. 로그는 파일당 10 MiB, 최대 3개로 회전한다. |

[proxy.json](proxy.json)은 route, backend 허용 목록, timeout, 비교·수집 예산의 기준이다. 실행 환경 식별자는 `deployment`이다. 요약 보존 기간은 1시간이다. 프록시 내부 작업이 기본 60초마다 만료 상세와 요약을 각각 최대 1,000개 정리한다. 보존 만료 시점부터 조회에서 제외되며, 실제 정리는 다음 배치부터 진행하며, 배치 상한과 적체에 따라 완료가 늦어질 수 있다. 기본 CLI의 전달 기능과 선택 기능에 필요한 내부 함수는 [구현 구성](../docs/implementation/README.md#기본-실행-진입점의-연결-범위)을 따른다.

## 실행과 설정 변경

아래 명령은 저장소 루트에서 실행한다.

1. 코드 개발·검증에서는 [proxy.json](proxy.json)의 더미 주소를 유지한다. 실제 backend 연결을 수행할 때만 `default_v1`과 `allowed_backends`를 해당 주소로 변경한다. 지정한 주소에 backend가 없으면 요청은 전달 오류로 종료된다.
2. Compose 구성을 확인한다.

   ```sh
   docker compose config --quiet
   ```

3. 애플리케이션 이미지를 빌드한다.

   ```sh
   docker compose build proxy
   ```

4. 애플리케이션 설정을 확인한다.

   ```sh
   docker compose run --rm --no-deps proxy check-config --explain
   ```

5. 애플리케이션을 실행한다.

   ```sh
   docker compose up -d --wait
   ```

6. 실제 서비스에 배포할 때에는 backend로 요청을 전달할 수 있는지 확인한 뒤 기존 진입 경로를 연결한다. readiness는 애플리케이션의 요청 수락 상태만 확인하며 backend 연결이나 저장 성공을 증명하지 않는다.

JSON 설정은 환경변수의 backend·route 값보다 우선한다. 이 Compose 구성은 호스트의 `.env`를 컨테이너에 전달하지 않는다. 설정을 변경할 때에는 `revision`, `previous_revision`, 변경 사유와 해당 관측 구간의 `epoch_id`를 함께 관리한다. 변경 적용은 `docker compose up -d --force-recreate --wait`로 수행한다.

특정 route에서 전환을 시작하려면 해당 API의 method·경로·응답 계약을 등록한다. shadow는 복제 안전성 검토 후 활성화한다. `matched`·`different` 의미 판정을 하려면 내부 함수로 권한·데이터 비교 문맥을 제공한다. 이 함수가 없어도 기본 전달과 설정에 따른 shadow 실행은 가능하며, 확인하지 못한 의미 비교는 `not_comparable`로 남긴다. [라우팅 설정 계약](../docs/routing/configuration.md)을 따른다.

업로드 완료 전에 backend가 반환한 413 등의 최종 응답도 status·body·전달 대상 헤더를 보존해야 한다. [업로드 중 조기 응답 전달](../docs/routing/http-forwarding.md#업로드-중-조기-응답-전달)을 따른다.

## 이벤트 저장소 선택

메트릭은 메모리에서 집계하고 비교 이벤트는 DB에 저장한다. SQLite·PostgreSQL 설정은 이벤트 저장소만 선택한다. CloudWatch·Sentry·Loggly는 역할과 정보 전송 경계가 다른 관측 서비스이며 이 설정의 대체값이 아니다. 각 서비스의 용도와 제약은 [조사 문서](../docs/research/observability-and-storage.md)를 따른다.

| 환경변수 | 기본값·의미 |
| --- | --- |
| `API_PROXY_EVENT_STORE_BACKEND` | `sqlite`. 여러 호스트가 같은 이벤트 DB에 기록해야 하면 `postgresql`을 선택한다. |
| `API_PROXY_EVENT_STORE` | SQLite 파일 경로. 이 Compose에서는 `/data/events.sqlite`이다. |
| `API_PROXY_POSTGRES_DSN` | PostgreSQL 선택 시 필요한 접속 문자열. 배포 플랫폼의 비밀 환경변수로 공급한다. 설정 파일·명령 인자·로그에 credential을 기록하지 않는다. |
| `API_PROXY_RETENTION_INTERVAL_SECONDS` | `60`. 내부 만료 정리 주기(초). 양수이며 실행 중인 정리와 겹치지 않는다. |
| `API_PROXY_RETENTION_BATCH_SIZE` | `1000`. 주기당 삭제하는 상세·요약 각각의 최대 행 수. |
| `API_PROXY_CONTROL_ENABLED` | `true`. `/healthcheck` 전용 loopback listener. 꺼도 내부 집계·저장은 유지되지만 이 Compose의 healthcheck는 사용할 수 없다. |

SQLite는 단일 호스트의 영속 저장소에서 운영할 수 있다. writer 경합과 삭제 처리량이 요구를 충족하는지 확인한다. 여러 호스트에서 같은 SQLite 파일을 공유하지 않는다. PostgreSQL은 사설망의 제한된 계정으로 접속하고, 원격 연결에는 서버 인증서 검증 등 조직의 연결 정책을 적용한다. `check-config`는 문자열 설정만 검사하며 DB 연결·권한·내구성은 확인하지 않는다.

기본 Compose는 SQLite를 명시한다. PostgreSQL 배포는 서비스 환경에 backend와 비밀 DSN을 공급하는 override를 적용한다. 일반 실행에서는 다음처럼 비밀 저장소에서 이미 주입한 DSN을 사용한다.

```sh
API_PROXY_EVENT_STORE_BACKEND=postgresql api-migration-proxy serve
```

위 명령은 `API_PROXY_POSTGRES_DSN`이 프로세스 환경에 설정되어 있어야 한다. 앱은 선택한 PostgreSQL 스키마에 이벤트 테이블과 인덱스를 초기화한다. 전용 스키마를 사용하고 앱 계정에 필요한 초기화·기록·조회·삭제 권한을 제한해서 부여한다. 저장소 선택은 기존 데이터를 이동하지 않는다. 기존 데이터의 보존·백업과 새 저장소의 저장 ACK·조회·만료 정리를 별도로 확인한다.

## 관측 정보의 접근 경계

메트릭과 상태도 트래픽 규모·route·revision을 드러낼 수 있어 공개 데이터로 취급하지 않는다. 프록시는 이를 내부에서 처리하며 HTTP 조회 경로나 활성화 옵션을 제공하지 않는다. `/healthcheck`는 별도 loopback listener에서 요청 수락 준비 여부에 따라 200 또는 503과 `ready` 값만 반환한다. token이 있어도 외부 주소 바인딩이나 외부 peer 접근을 허용하지 않는다. 이 응답은 DB 저장 성공이나 backend 상태를 대신하지 않는다.

애플리케이션은 관측용 외부 전송 SDK를 연결하지 않는다. 다만 배포 플랫폼의 로그 드라이버나 에이전트는 stdout/stderr를 외부로 보낼 수 있다. 로그 목적지와 접근 권한은 배포 구성에서 확인한다. 요청·응답 원문과 credential을 로그 수집 대상으로 추가하지 않는다.

## AWS의 health check와 적용 범위

이 절은 향후 AWS 배포를 선택할 때의 참고자료이며, 현재 코드 검증의 선행 조건이 아니다.

AWS ALB의 HTTP/HTTPS health check는 대상 그룹에서 경로를 지정하며 기본값은 `/`이다. `/healthcheck`는 AWS가 강제하는 경로가 아니라 이 애플리케이션에서 선택한 이름이다. [ALB health check 설정](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html)

ECS의 컨테이너 health check는 컨테이너 내부 명령으로 loopback에 접속할 수 있다. 이 프로젝트의 단일 `/healthcheck`는 이 내부 점검 방식에 맞는다. ECS를 선택한다면 task definition에 health check 명령을 지정해야 하며, Compose의 healthcheck가 자동으로 ECS 설정에 반영되는 것은 아니다. [ECS 컨테이너 health check](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/healthcheck.html)

ALB가 다른 호스트에서 이 loopback listener를 직접 검사할 수 있는 구성은 제공하지 않는다. 실제 AWS 대상 그룹·보안 그룹·task definition은 이번 작업에서 확인하거나 변경하지 않았다.

## 보존과 종료

만료 정리는 프록시 안에서 자동 실행한다. 외부 스케줄러는 필수가 아니다. 유입량에 비해 정리량이 부족하면 주기·배치 크기를 조정하고 삭제 적체를 확인한다. 수동 진단이나 복구가 필요할 때 다음 명령으로 한 배치를 정리한다.

```sh
docker compose exec -T proxy api-migration-proxy purge-events --event-store /data/events.sqlite --batch-size 1000
```

이벤트 파일이 아직 없으면 삭제 명령은 실패하며 새 파일을 만들지 않는다. PostgreSQL은 backend·DSN 환경을 적용한 상태에서 `purge-events --batch-size 1000`을 사용한다. `--event-store`를 명시하면 SQLite 파일을 대상으로 한다. 보존·접근 조건은 [조회와 보존](../docs/collection/query-and-retention.md)을 따른다.

`docker compose down`은 서비스와 네트워크를 정리하고 이벤트 volume은 유지한다. 데이터를 보존할 때에는 `--volumes`를 붙이지 않는다. 애플리케이션의 종료 예산을 늘리면 `stop_grace_period`도 함께 검토한다.

Compose 속성의 의미는 [Docker 서비스 설정](https://docs.docker.com/reference/compose-file/services/)과 [volume 설정](https://docs.docker.com/reference/compose-file/volumes/)을 따른다.
