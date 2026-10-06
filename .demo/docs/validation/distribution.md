# 선택 Serving 응답 분배 검증

- 목적: 임의의 v2 serving 비율 한 번에 대해 User 응답 분배 확인.
- 기본 전환 흐름과 CI는 [순차 시나리오](scenarios.md) 사용. 이 명령은 선택적인 단일 표본 검사.
- 대상: 실제 Docker v1·v2·제품 Proxy와 일회성 User의 순차 HTTP 요청.
- 기본값: 1,000회, v2 50%, 허용 오차 ±8%p.
- 범위: 요청별 배정 확인. 동시 부하·목표 QPS·운영 성능 측정 제외.

## 실행

- 전제: Docker Engine·Compose 실행, Python 3.13 이상 사용 가능.
- 명령 실행 위치: 프로젝트 루트.
- 기존 데모의 `demo-up` 불필요. 실행마다 독립 환경 구성.

```sh
make demo-distribution
```

- 기본 Python: 프로젝트 `.venv/bin/python`.
- 별도 Python 사용 시 `make demo-distribution PYTHON=python3` 지정.

```sh
DEMO_DISTRIBUTION_REQUESTS=1000 DEMO_DISTRIBUTION_V2_RATIO=0.5 \
  DEMO_DISTRIBUTION_TOLERANCE=0.08 make demo-distribution

DEMO_DISTRIBUTION_V2_RATIO=0 make demo-distribution
DEMO_DISTRIBUTION_V2_RATIO=1 make demo-distribution
```

| 입력 | 기본값 | 허용 범위 |
| --- | --- | --- |
| `DEMO_DISTRIBUTION_REQUESTS` | `1000` | 양의 정수 |
| `DEMO_DISTRIBUTION_V2_RATIO` | `0.5` | 유한한 수, `0` 이상 `1` 이하 |
| `DEMO_DISTRIBUTION_TOLERANCE` | `0.08` | 유한한 수, `0` 이상 `0.5` 미만 |

- 허용 오차의 단위: 절대 비율. `0.08`은 ±8%p.
- 잘못된 입력: Docker 실행 전 거부, CLI 종료 코드 `2`.
- 요청 경로: `/items/different`, 요청별 timeout: 5초.
- User 실행 제한: 600초. 표본 수를 크게 늘리면 실행 제한 도달 가능.

## 통과 기준

| 항목 | 기준 |
| --- | --- |
| 요청 수 | `attempts`·`responses` 모두 설정한 요청 수와 일치 |
| 네트워크 오류 | `transport_errors=0` |
| HTTP 응답 | 전체 HTTP 200 |
| 응답 버전 | `unknown=0`, v1·v2 응답 수 합계가 요청 수와 일치 |
| 분배 비율 | v2 응답 수가 허용 범위 안에 존재. 경계 포함 |

- 기본 조건: v2 응답 **420~580회**, v1 응답 **420~580회**.
- 일반 범위: 요청 수를 `N`, v2 비율을 `p`, 허용 오차를 `t`로 두고 아래 정수 구간 적용.
  - 최솟값: `ceil(N × max(0, p - t))`.
  - 최댓값: `floor(N × min(1, p + t))`.
- `p=0`·`p=1`: 허용 오차를 `0`으로 적용하여 각각 v1·v2 응답 100% 요구.
- 관측 비율: 보고서의 비율 값 대신 실제 버전별 응답 수에서 재계산.
- 잘못된 카운터·분모 구조: 판정 입력 거부.
- 요청 실패·HTTP 오류·버전 미확인: 분배 비율이 맞더라도 실패.
- 판정 통과 후 로그 수집·자원 정리 실패: 전체 실행 실패.
- 정상 종료 코드: 통과 `0`, 실행·판정·정리 실패 `1`.

## 배정과 관측 범위

- request cohort 사용: 각 요청의 키를 기준으로 serving 배정.
- 동일 사용자의 고정 배정·1,000명의 서로 다른 사용자 비율은 검증하지 않음.
- 유한 표본의 편차 허용. 정확한 500:500 요구 없음.
- 허용 구간 판정이며 실패 표본 제외·통과할 때까지 자동 재실행 없음.
- shadow 중지 상태로 검증. User는 `x-backend-version`으로 응답 버전을 집계.
- backend별 실제 호출 증가량을 User의 버전별 응답 수와 대조. Proxy 정상 종료 후 해당 revision의 SQLite 이벤트 0건도 확인.
- 양쪽 backend의 총 호출 수는 shadow 등의 영향을 받으므로 serving 응답 수와 구분.
- User 집계 자체는 품질 판정을 하지 않으며 별도 검증기가 통과·실패 결정.

## 실행 흐름과 격리

1. 원본 [`config/docker.json`](../../config/docker.json)을 결과 디렉토리에 복사.
2. 복사본에 새 revision, 요청한 v2 비율, request cohort, shadow 중지 적용.
3. 고유 Compose project·빈 이벤트 volume·임의의 loopback 포트 할당.
4. v1·v2·Proxy·User·저장소 조회용 test 이미지 빌드 후 Proxy 설정 검사.
5. 내부 health와 mount한 설정 revision 확인, backend 시작 counter 기록 후 User 요청 실행.
6. Proxy 정상 종료 후 backend 최종 counter·SQLite 이벤트 판정, 로그 수집, 해당 실행의 컨테이너·네트워크·volume 제거.
7. 판정 JSON·요약 저장 및 종료 코드 반환.

- 원본 설정과 기존 로컬 데모의 컨테이너·이벤트 volume 유지.
- 실행 도중 실패해도 가능한 범위에서 로그 수집·자원 정리 수행.
- 강제 종료·Docker Engine 오류로 정리 실패 가능. `execution.log`의 Compose project 이름을 확인해 해당 자원만 정리.
- Docker 이미지는 실행 후 유지.

## 산출물

- 저장 경로: `.demo/results/distribution/run-*`.
- 실행마다 별도 디렉토리 생성. 생성 결과는 Git 추적 제외.

| 파일 | 내용 |
| --- | --- |
| `config.json` | 해당 실행에 적용한 설정 복사본 |
| `report.json` | User의 HTTP·버전·지연 집계 |
| `observation.json` | 내부 health·mount한 설정 revision·backend 전후 counter·Proxy 종료 상태 |
| `events.json` | 해당 revision의 이벤트. shadow OFF이므로 빈 배열 요구 |
| `stored-events.json` | 전체 저장 이벤트의 ID·요약. 독립 shadow OFF 환경이므로 빈 배열 요구 |
| `proxy.log` | 해당 실행의 Proxy 시작·종료 로그 |
| `validation.json` | User·backend·종료·이벤트 기대값과 관측값·통과 여부·실패 이유 |
| `summary.md` | 사람이 확인할 실행 요약 |
| `execution.log` | Compose project 이름, 빌드·실행·정리 로그 |
| `containers.log` | v1·v2·Proxy 등 해당 project의 컨테이너 로그 |

- 초기 실패 시 `report.json` 부재 또는 불완전 가능. 판정·실행 로그를 함께 확인.
- 입력 검증처럼 결과 디렉토리 생성 전 실패한 경우 산출물 없음.
- 기본 CI의 자동 실행·보관 대상은 [순차 시나리오](scenarios.md). 이 단일 분배 명령은 로컬 선택 검사.
- 구현: [시나리오 실행기](../../scenarios/run_distribution.py), [분배 판정기](../../scenarios/distribution.py).
