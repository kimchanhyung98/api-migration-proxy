# 순차 데모 시나리오

- 목적: 같은 backend·이벤트 volume을 유지하며 설정 전환과 실제 관측 결과 대조.
- 실행: `.demo/scenarios/`의 별도 도구. pytest에서 전체 흐름을 실행하지 않음.
- 대상: Docker v1·v2·제품 Proxy·User, 컨테이너 내부 health·backend 호출 수·읽기 전용 SQLite 조회.
- 전제: Docker Engine·Compose, Python 3.13 이상. 프로젝트 루트에서 실행.
- `demo-up` 선행 불필요. 각 명령은 고유 Compose project·새 이벤트 volume·임의 loopback 포트 사용.

## 실행

```sh
make demo-scenario-serving
make demo-scenario-shadow
```

- 기본 Python: `.venv/bin/python`. 별도 Python은 `PYTHON=python3` 지정.

```sh
DEMO_SCENARIO_REQUESTS=1000 DEMO_SCENARIO_TOLERANCE=0.08 \
  make demo-scenario-serving PYTHON=python3
DEMO_SCENARIO_SHADOW_REQUESTS=100 \
  make demo-scenario-shadow PYTHON=python3
```

| 입력 | 기본값 | 역할 |
| --- | --- | --- |
| `DEMO_SCENARIO_REQUESTS` | `1000` | serving 시나리오의 단계별 요청 수 |
| `DEMO_SCENARIO_SHADOW_REQUESTS` | `100` | shadow 시나리오의 단계별 요청 수 |
| `DEMO_SCENARIO_TOLERANCE` | `0.08` | 중간 serving 비율의 절대 허용 오차, ±8%p |

- 요청 수는 양의 정수. 허용 오차는 유한한 0 이상 0.5 미만 값.
- 요청 경로 `/items/different`, 요청별 timeout 5초, 순차 호출.
- serving 기본 총 4,000회, shadow 기본 총 400회. 동시 부하·운영 성능 검증 아님.

## serving 전환·복귀

| 단계 | v2 비율 | serving 기대값 | shadow | 기본 표본 |
| --- | --- | --- | --- | --- |
| 1 | `0` | v1 100% | OFF | 1,000회 |
| 2 | `0.5` | v1·v2 약 50:50 | OFF | 1,000회 |
| 3 | `1` | v2 100% | OFF | 1,000회 |
| 4 | `0` | v1 100% 복귀 | OFF | 1,000회 |

- 0·1 비율은 허용 오차 없이 해당 버전 100% 요구.
- 50:50 단계는 기본 v1·v2 각 420~580회 허용. 요청 수·허용 오차 변경 시 [분배 정수 구간](distribution.md#통과-기준) 사용.
- 모든 단계에서 shadow 실행·해당 revision의 이벤트 0건 요구.

## shadow ON/OFF

| 단계 | serving | shadow | 기본 표본 | 해당 revision 이벤트 |
| --- | --- | --- | --- | --- |
| 1 | v1 100% | v2 ON, 표본 100% | 100회 | 100건 |
| 2 | v1 100% | OFF | 100회 | 0건 |
| 3 | v2 100% | v1 ON, 표본 100% | 100회 | 100건 |
| 4 | v2 100% | OFF | 100회 | 0건 |

- ON → OFF에서 serving 버전 유지. 반대 backend의 shadow 실행만 변화.
- ON 단계는 모든 shadow 요청 완료·이벤트 저장 요구. 선택됐지만 생략되거나 drop된 요청이 있으면 실패.
- OFF 단계는 이전 이벤트가 volume에 남더라도 현재 revision의 이벤트는 0건이어야 함.
- 기본 데이터·권한 문맥은 미확인. ON 이벤트의 비교 결과는 `not_comparable`이며 `matched` 통과 조건을 사용하지 않음.

## 단계마다 적용·관측

1. 같은 시나리오의 새 revision·이전 revision·변경 사유를 가진 JSON 생성.
2. `check-config` 성공 후 Proxy만 재생성. backend·Compose project·이벤트 volume 유지.
3. Proxy 컨테이너 안에서 `/healthcheck`와 mount한 설정 파일 확인, backend별 시작 counter 기록.
4. User 요청 완료 후 Proxy 정상 종료 요청. 종료 상태·코드·OOM 여부·종료 완료 로그 확인.
5. backend 최종 counter와 읽기 전용 SQLite 이벤트를 수집하고 User 보고서·해당 revision·이전 저장 결과와 대조.
6. 단계 판정과 마지막 전체 요약 저장. 다음 단계는 Proxy를 재생성하며, 실행 종료 시 해당 시나리오의 자원만 정리.

- 원본 JSON·`.demo/.env`·일반 수동 데모의 컨테이너·volume은 유지.
- 설정 파일 revision 확인은 mount한 파일의 증거. health 응답이나 실행 중 runtime에서 활성 revision을 읽는 방식이 아님.
- 실제 동작은 응답 분포와 backend 호출 수로 확인. shadow ON은 저장 이벤트의 revision도 대조.
- 이전 단계 Proxy의 정상 종료와 최종 저장 결과를 확인한 뒤 다음 단계로 진행. 설정 hot reload가 아님.

## 공통 통과 기준

| 관측 | 통과 기준 |
| --- | --- |
| User 요청 | attempts·responses가 단계 요청 수와 같음 |
| HTTP·전송 | 전체 200, transport errors 0 |
| 응답 버전 | unknown 0, v1·v2 합계가 요청 수와 같고 단계 기대 비율 충족 |
| 시작 상태·설정 | 내부 health의 `ready=true`, mount한 JSON revision이 단계 설정과 같음 |
| backend 호출 | OFF는 backend별 증가량이 User의 해당 버전 응답 수와 같음. ON은 양쪽 각각 N건 |
| Proxy 종료 | `exited`, 종료 코드 `0` 또는 `143`, OOM 없음, 애플리케이션 종료 완료 로그 모두 확인 |
| 저장 | 해당 revision 이벤트가 ON=N, OFF=0이고 역할·비교 결과가 기대와 같음 |
| 단계 간 보존 | 이전 이벤트의 ID·내용 보존, 예상하지 않은 추가 이벤트 없음 |

- backend `GET /__demo/requests`의 `items`는 누적 item 호출 수. 단계 전후 차이를 사용하며 감소·누락·잘못된 버전은 실패.
- Proxy 내부 queue·메트릭은 HTTP로 조회하지 않음. 정상 종료와 최종 요청·이벤트 대조가 내부 counter 전체 검증을 보장하지는 않음.
- 저장 이벤트의 `summary.configuration_revision`으로 해당 단계를 구분. 이벤트가 없는 OFF도 User·backend 호출 수로 요청 실행 확인.
- User 보고서 생성 성공만으로 통과하지 않음. 관측 불일치·비정상 종료·정리 실패도 전체 실패로 처리.

## 산출물·정리

- 위치: `.demo/results/scenarios/<run>/`. 실행마다 별도 디렉토리, Git 제외.

| 범위 | 산출물 |
| --- | --- |
| 단계별 | `config.json`, `report.json`, `observation.json`, `events.json`, `stored-events.json`, `proxy.log`, `validation.json` |
| 실행 전체 | `summary.md`, `execution.log`, `containers.log`, 전체 `validation.json` |

- config: 적용 설정. report: User 응답·버전 집계. observation: 내부 health·mount한 설정 revision·backend 전후 counter·Proxy 종료 상태.
- events: 해당 revision의 이벤트. stored-events: 전체 저장 이벤트의 ID·요약으로 이전 단계 보존과 추가 이벤트 유무 확인. 원문 요청·응답 본문·인증 토큰 저장 제외.
- proxy.log: 해당 단계 Proxy의 시작·종료 로그. validation: 기대값·관측값·통과 여부·실패 이유. summary: 전체 단계 결과.
- 토큰·원문 payload·비밀값을 산출물에 기록하지 않음.
- 초기 실패 시 일부 단계 파일이 없을 수 있음. 실행·컨테이너 로그와 전체 validation 확인.
- 실패 시에도 가능한 로그 수집·자원 정리 수행. 강제 종료·Docker 오류로 정리 실패하면 로그의 해당 project만 정리.
- 실행 완료 후 컨테이너·네트워크·시나리오 volume 제거, Docker 이미지·산출물 유지.
- 자동 실행과 보관: [GitHub Actions](github-actions.md).
