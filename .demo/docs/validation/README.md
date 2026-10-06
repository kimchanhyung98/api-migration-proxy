# 데모 검증

- 목적: 합성 API·User·제품 Proxy의 개별 기능과 실제 전환 흐름 검증.
- `tests/`: 개별 기능 테스트. `scenarios/`: 실행 중인 서비스 관측·Docker 순차 시나리오.
- 전체 시나리오는 pytest 수집·실행 대상에 넣지 않음.
- 모든 명령은 프로젝트 루트에서 실행.

## 개별 기능 검사

```sh
make init
make demo-check
make demo-docker-test
```

- 호스트 전제: Python 3.12 이상과 프로젝트 개발 의존성 설치.
- `demo-check`: 데모 코드·실행 도구·테스트의 lint·format·타입 검사와 개별 pytest 실행.
- `demo-docker-test`: 별도 데모 이미지에서 같은 검사, 의존성 검사, 개별 pytest 실행.
- HTTP 기능 테스트는 필요한 프로세스를 직접 시작·종료. `demo-up` 선행 불필요.
- 제품 검증: 루트 `make check`, `make docker-test`로 별도 실행.

| 테스트 영역 | 검증 내용 |
| --- | --- |
| 합성 API | 버전별 성공·오류 본문, 동일 데이터 대조군, 지연·404·405 응답 |
| User | HTTP 응답 집계, redirect·네트워크 실패, 입력·출력 처리 |
| Proxy 연결 | 실제 HTTP 응답 전달·버전 헤더·저장 등 각 기능 |
| 비교 결과 | 합성 문맥·기본 정책의 구조 차이, 별도 필드 매핑 정책의 `matched`·`different` |
| 관측·판정 | 내부 health·backend 요청 수·정상 종료·이벤트 판정, 분배 허용 경계·오류·누락 표본 |

## 순차 시나리오

```sh
make demo-scenario-serving
make demo-scenario-shadow
```

- serving: v1 100% → 50:50 → v2 100% → v1 100%, 모든 단계 shadow OFF.
- shadow: v1 serving·v2 shadow ON → OFF → v2 serving·v1 shadow ON → OFF.
- 각 시나리오 안에서 backend·이벤트 volume 유지. 단계마다 설정 검사 후 Proxy만 재생성.
- User 보고서·backend 요청 수 증가량·Proxy 정상 종료 후 revision별 SQLite 이벤트를 함께 판정.
- 단계별 통과 기준·산출물·표본 수: [순차 시나리오](scenarios.md).
- 임의 비율 한 번의 표본만 확인: [선택 분배 검사](distribution.md).

## 선택 smoke

- 기본 수동 실행은 shadow OFF이므로 shadow를 요구하는 smoke 조건을 충족하지 않음.
- 아래 고급 JSON 예시는 v1 serving 100%·v2 shadow 100%·합성 200/404/500 계약을 포함.

```sh
PROXY_CONFIG=./config/docker.json make demo-up
PROXY_CONFIG=./config/docker.json make demo-smoke
PROXY_CONFIG=./config/docker.json make demo-down
```

- 실행 도구: [scenarios/smoke.py](../../scenarios/smoke.py).
- 전제: serving 비율 0 또는 1, `shadow.sample_ratio=1`, `shadow.stopped=false`, 합성 API 계약.
- `SERVING_BACKEND=v2`: smoke의 기대 응답 버전. Proxy 설정 자체를 변경하지 않음.
- 확인: HTTP 200·404·500의 상태·본문·버전 헤더, serving·shadow 역할, 완료·SQLite 저장.
- 읽기 전용 SQLite 조회로 smoke 시작 이후 route 이벤트 확인. 같은 환경에 다른 요청을 섞지 않는 전용 실행 권장.
- 일반 CLI의 정상·예상 404 응답 쌍은 `not_comparable`, 예상 밖 500은 `execution_error`.
- 중간 serving 비율·shadow OFF는 순차 시나리오로 확인.

## 검증 경계

- User는 외부 응답만 집계. shadow는 backend 요청 수와 정상 종료 후 저장 이벤트를 함께 검증. 제품 내부 메트릭·queue를 HTTP로 조회하지 않음.
- readiness는 runtime 요청 수락·유효 설정 확인. backend 성공률·비교 품질·저장소 준비의 보장 아님.
- Docker 검증은 실제 HTTP 흐름이며 브라우저 UI E2E와 구분.
- 제외: 실제 HTTPS·업무 인증·업무 데이터·대표 부하·클라우드·다중 Proxy 운영 검증.
- 수동 `demo-down`은 저장 완료 이벤트 volume 유지. 독립 시나리오는 자신이 만든 자원만 정리.
- [GitHub Actions](github-actions.md)는 개별 기능 테스트 후 serving·shadow 시나리오를 순서대로 실행.
