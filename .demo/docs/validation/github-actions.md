# GitHub Actions 데모 검증

- Workflow: `Demo validation` (`.github/workflows/demo.yml`).
- 제품 `Product CI`와 독립. 개별 기능 테스트 후 serving·shadow 순차 시나리오 실행.
- 실행 전제: 필요한 코드·설정·workflow가 원격 실행 대상 revision에 포함. 로컬 검증과 원격 실행은 별개.

## 실행 조건

| 이벤트 | 조건 |
| --- | --- |
| push | `main`에 대상 경로 변경 |
| pull request | 대상 branch가 `main`이며 대상 경로 변경 |
| 수동 실행 | Actions의 `Demo validation`에서 표본 수·허용 오차 지정 |

- 대상 경로: `.demo/**`, `src/**`, 루트 `Makefile`·`Dockerfile`·`.dockerignore`·`.env.example`·`pyproject.toml`·`requirements-dev.txt`, 해당 workflow.
- 같은 workflow·ref의 새 실행은 이전 실행 취소.
- 실행 환경: `ubuntu-24.04`, job 제한 30분, 저장소 읽기 권한.

## 수동 입력

| 입력 | 기본값 | 의미 |
| --- | --- | --- |
| `requests` | `1000` | serving 시나리오의 단계별 요청 수 |
| `shadow_requests` | `100` | shadow 시나리오의 단계별 요청 수 |
| `tolerance` | `0.08` | 50:50 단계의 절대 비율 허용 오차, ±8%p |

- push·PR은 기본값 사용. 0·1 비율은 항상 정확한 전체 전환 요구.
- serving 비율·shadow 단계 순서는 고정. `v2_ratio` 수동 입력은 사용하지 않음.
- 임의의 비율 한 번을 검사하려면 로컬 [demo-distribution](distribution.md) 사용.

## 실행 단계

1. 소스 checkout.
2. `make demo-docker-test`: lint·format·타입·의존성 검사와 개별 기능 테스트.
3. `make demo-scenario-serving PYTHON=python3`: 같은 환경에서 v1 → 50:50 → v2 → v1.
4. `make demo-scenario-shadow PYTHON=python3`: 같은 환경에서 v1 serving의 shadow ON/OFF → v2 serving의 shadow ON/OFF.
5. 시나리오 요약 게시, 테스트용 Compose 자원 정리, 산출물 artifact 업로드.

- 각 시나리오는 자신의 고유 Compose project를 만들고 단계 사이에 backend·volume 유지 후 정리.
- 기능 테스트 실패 시 시나리오 미실행. serving 시나리오 실패 시 shadow 시나리오 미실행.
- 요약·정리·artifact 단계는 앞선 실패 시에도 실행하도록 구성.
- 취소·강제 종료·runner 장애까지 정리·업로드 완료를 보장하지 않음.

## 결과와 실패 확인

- Actions job 결과: 개별 테스트·각 시나리오·실행 오류 확인.
- 실행 요약: 단계별 결과·요청 수·기대 v2 비율·shadow ON/OFF, User 버전·HTTP·전송 오류, 저장 이벤트 수 확인.
- 상세 revision·backend 요청 수·Proxy 종료 판정은 `validation.json`, `observation.json`, `events.json`에서 확인.
- artifact 이름: `demo-scenarios-<run_id>-<run_attempt>`.
- 업로드: `.demo/results/scenarios/`의 단계별 config·report·observation·events·stored-events·proxy 로그·validation, 전체 summary·validation·로그.
- 보관 기간: 7일. 토큰·원문 요청/응답 payload 저장 제외.
- 시나리오 시작 전 실패·입력 오류는 산출물이 없을 수 있으므로 step 로그 확인.
- 단계 실패는 해당 `validation.json`과 observation/events/stored-events·로그를 대조.
- 시간 제한 초과는 성공으로 처리하지 않음.

## 로컬 재현

```sh
make demo-docker-test
DEMO_SCENARIO_REQUESTS=1000 DEMO_SCENARIO_TOLERANCE=0.08 \
  make demo-scenario-serving PYTHON=python3
DEMO_SCENARIO_SHADOW_REQUESTS=100 \
  make demo-scenario-shadow PYTHON=python3
```

- [단계·통과 기준·산출물](scenarios.md), [검증 범위](README.md) 참고.
