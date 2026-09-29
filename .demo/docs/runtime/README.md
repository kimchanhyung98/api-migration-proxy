# Docker 실행 환경

- 구성: [compose.yml](../../compose.yml), [데모 Dockerfile](../../Dockerfile), [루트 명령 Makefile](../../../Makefile), [내부 데모 Makefile](../../Makefile).
- 조건: Docker Engine·Compose 사용 가능. 아래 명령은 프로젝트 루트에서 실행.
- Proxy 이미지: 제품 루트 Dockerfile의 `runtime` 사용. 데모 구현은 제품 패키지·Proxy 이미지에서 제외.
- 데모 이미지: 실행 결과(`results/`)와 로컬 검사 캐시를 Docker build context에서 제외.

## 서비스와 실행

| 서비스 | 이미지 target | 역할 | 실행 기간 |
| --- | --- | --- | --- |
| `v1`, `v2` | 데모 `backend` | 버전별 합성 API | 상시 |
| `proxy` | 제품 `runtime` | 실제 Proxy | 상시 |
| `user` | 데모 `user` | 외부 HTTP 호출·집계 | 일회성, `user` profile |
| `test` | 데모 `test` | 검사·테스트·smoke | 일회성, `test` profile |

```sh
make demo-up
make demo-request
make demo-smoke
make demo-down
```

- `demo-up`: 이미지 빌드 후 v1·v2·Proxy 시작, healthcheck 완료 대기.
- `demo-request`: 응답 비율·오류 집계. [User 문서](../user/README.md) 참고.
- `demo-smoke`: 기본 설정의 v1 serving·shadow·이벤트 저장 확인. [검증 조건](../validation/README.md) 참고.
- `demo-down`: 컨테이너·네트워크 정리, 이벤트 볼륨 유지.
- backend healthcheck: `/health` HTTP 응답 확인.
- Proxy healthcheck: TCP 수락 여부 확인. 비교 품질·저장소 준비·운영 readiness의 증거는 아님.

## 주소와 포트

| 서비스 | 기본 호스트 주소 | Docker 내부 주소 |
| --- | --- | --- |
| v1 | `http://127.0.0.1:8101` | `http://v1:8000` |
| v2 | `http://127.0.0.1:8102` | `http://v2:8000` |
| Proxy | `http://127.0.0.1:8080` | `http://proxy:8080` |

- 호스트 포트는 loopback에만 공개.
- 브라우저 확인: Proxy의 `/items/different` 접속. v1의 최상위 `id`·`name`과 v2의 `code`·`message`·`result` 구조 및 `result.name`의 변경값 구분. 정확한 본문은 [버전별 응답](../backends/README.md) 참고.
- 포트 충돌 시 `PROXY_PORT`, `V1_PORT`, `V2_PORT` 변경. 이후 관련 Compose·Make 명령에도 동일 환경변수 전달.

```sh
PROXY_PORT=18080 V1_PORT=18101 V2_PORT=18102 make demo-up
```

## 설정 파일

| 파일 | 대상 | 초기 동작 |
| --- | --- | --- |
| [config/docker.json](../../config/docker.json) | Docker 내부 v1·v2 | serving v1, request cohort, shadow 100% |
| [config/local.json](../../config/local.json) | 호스트 8101·8102 | 전환 비활성, v1 통과, shadow 비활성 |

- Proxy: 시작 시 `/config/proxy.json`으로 mount한 설정 읽기.
- 다른 파일 사용: `PROXY_CONFIG` 지정. 상대 경로는 `.demo/compose.yml`이 있는 `.demo` 기준.
- 파일 변경 적용: `make demo-reload`. revision 갱신 등 [전환 절차](../proxy/README.md) 함께 준수.
- 호스트용 설정과 Docker 내부 주소 혼용 금지.

## 데이터와 정리

- 기본 Compose project: `api-migration-proxy`.
- 이벤트 저장: `events` named volume의 `/data/events.sqlite`. 기본 실제 볼륨 이름 `api-migration-proxy_events`.
- Proxy: 이벤트 저장소 쓰기. smoke용 테스트 컨테이너: 같은 볼륨을 읽기 전용 mount.
- Proxy 재생성·`demo-down`: 저장 완료된 이벤트 보존. 데이터가 필요하면 Compose의 `down -v`·볼륨 삭제 금지.
- 보존 범위: 정상적으로 저장된 로컬 이벤트. 종료 중 메모리 작업의 무손실·호스트 장애 복구·공용 운영 DB 내구성 보장 아님.
- [자동 분배 검증](../validation/distribution.md): 별도 project·새 볼륨 생성 후 해당 자원만 삭제. 일반 수동 실행의 데이터 보존 방식과 구분.
