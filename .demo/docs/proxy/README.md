# Proxy 전환·shadow·관측

- 실행 대상: 제품 Proxy 패키지. 데모는 합성 endpoint·환경변수·요청 제공.
- 기본 환경변수 route: `default-route`, `GET /items/{id}`. 고급 JSON 예시·시나리오는 `synthetic_item` 사용.
- 기본 동작: v1 serving 100%, shadow OFF.

## serving과 shadow

| 설정 | 기본값 | 역할 |
| --- | --- | --- |
| `DEMO_V2_RATIO` | `0` | `0`: v1 100%, `0.5`: 약 50:50, `1`: v2 100% |
| `DEMO_SHADOW_ENABLED` | `false` | `true`: 안전한 합성 읽기 요청을 반대 backend로 shadow, `false`: OFF |

- serving: 사용자에게 반환할 응답 선택. 선택한 버전의 [본문](../backends/README.md)을 그대로 전달.
- shadow: serving과 독립인 부가 실행. shadow를 켜거나 꺼도 serving 비율은 유지.
- 기본 request cohort: 요청마다 배정. 같은 사용자의 고정 A/B 배정 아님.
- 중간 serving 비율은 유한 표본에서 편차 가능. 양 끝 비율은 해당 버전 100% 요구.
- shadow 활성 시 표본 비율은 100%. 실행 한도·요청 크기·취소 등에 따라 생략될 수 있으므로 관측값 확인 필요.
- 사용자 응답 수와 backend 전체 호출 수는 서로 다른 지표.

## 수동 설정 변경

```sh
make demo-up
make demo-request
DEMO_V2_RATIO=0.5 make demo-reload
DEMO_REQUESTS=1000 make demo-request REPORT=results/mixed.json
DEMO_V2_RATIO=1 make demo-reload
make demo-request REPORT=results/v2.json
DEMO_V2_RATIO=0 make demo-reload
make demo-request REPORT=results/v1-return.json
```

- 반복 실행에는 `.demo/.env`의 값을 수정하는 방식 사용 가능.
- `demo-reload`: 설정 검사 성공 후 Proxy만 재생성. backend·이벤트 volume 유지.
- 설정 검사·재생성 후 실제 응답 분포를 확인. JSON 시나리오는 컨테이너에 mount한 revision과 shadow ON의 저장 이벤트 revision도 대조. health로 활성 revision을 조회하지 않음.
- 무중단 전환·운영 변경 이력의 내구성을 보장하는 배포 절차가 아님.

```sh
DEMO_V2_RATIO=0 DEMO_SHADOW_ENABLED=true make demo-reload
make demo-request
DEMO_V2_RATIO=0 DEMO_SHADOW_ENABLED=false make demo-reload
```

- 위 명령은 v1 serving을 유지하면서 v2 shadow ON → OFF 적용.
- `PROXY_CONFIG` 전체 JSON 지정 시 JSON의 route·비교 정책이 우선. 단순 ratio·shadow 환경변수로 덮어쓰지 않음.
- 고급 JSON: `v2_serve_ratio`, `shadow.sample_ratio`, `shadow.stopped` 및 revision을 수정하고 재적용. [설정 경로](../runtime/README.md#고급-json-설정) 참고.

## 관측

- Proxy health는 컨테이너 내부 `http://127.0.0.1:9090/healthcheck`에서만 조회. 호스트 포트 공개 없음.
- 준비 상태는 HTTP 200과 `{"ready":true}`, 미준비 상태는 HTTP 503과 `{"ready":false}`. backend 정상·저장 성공·활성 revision을 나타내지 않음.
- 아래 명령은 선택 토큰을 컨테이너 환경변수에서 읽어 사용하며 값을 출력하지 않음.

```sh
docker compose -f .demo/compose.yml exec -T proxy python - <<'PYHEALTH'
import os
import urllib.request

token = os.environ.get("API_PROXY_CONTROL_TOKEN", "")
headers = {"Authorization": "Bearer " + token} if token else {}
request = urllib.request.Request("http://127.0.0.1:9090/healthcheck", headers=headers)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request, timeout=2) as response:
    print(response.read().decode())
PYHEALTH
```

| 관측 | 확인할 내용 | 해석 |
| --- | --- | --- |
| User 보고서 | 버전별 serving 응답 수·HTTP 오류·지연 | shadow 실행 여부는 확인하지 않음 |
| 더미 backend `GET /__demo/requests` | 버전과 누적 item 요청 수 | 단계 전후 증가량으로 serving·shadow 호출 대조 |
| Proxy 정상 종료 | 종료 상태·코드·OOM 여부·종료 완료 로그 | 내부 작업 종료 후 저장 결과를 확인하기 위한 전제 |
| SQLite 이벤트 | 단계 revision별 이벤트·역할·비교 결과 | shadow ON은 요청 수만큼 저장, OFF는 0건 요구 |

- 메트릭·상태 HTTP API는 제공하지 않음. 제품 내부 계측과 데모의 backend 호출 관측은 구분.
- shadow OFF: backend별 요청 수 증가량이 User의 해당 버전 응답 수와 일치해야 함.
- shadow ON: v1·v2 각각 단계 요청 수만큼 호출되고 이벤트가 저장되어야 함.
- 시나리오는 User 완료 후 Proxy를 정상 종료하고 최종 SQLite 이벤트를 확인. 다음 단계는 Proxy만 재생성하며 backend·이벤트 volume 유지.
- 이전 단계 이벤트의 보존도 별도 확인. 상세 판정·산출물은 [순차 시나리오](../validation/scenarios.md) 참고.
- 합성 읽기 전용 `review_ref`는 shadow 허용 근거. 데이터·권한 비교 문맥을 신뢰 상태로 바꾸지 않음.
- 기본 비교 문맥은 데이터·권한 모두 미확인. 정상 응답 쌍은 `not_comparable`이며 `matched`를 기대하지 않음.
- 환경변수 기본 계약의 404는 분류 미설정으로 `not_comparable`·`contract_class_unknown`. 고급 JSON 예시의 예상 404는 권한 문맥 미설정으로 `not_comparable`·`authorization_context_unknown`.
- 500은 두 설정 모두 `execution_error`·`unexpected_api_error`.
- 테스트에서만 합성 문맥·필드 매핑을 주입한 비교 결과와 실제 CLI 실행 결과를 구분.

## 자동 검증

```sh
make demo-scenario-serving
make demo-scenario-shadow
```

- serving 전환·복귀와 shadow ON/OFF를 각각 순차 검증. [단계·관측 기준](../validation/scenarios.md) 참고.
- 임의 비율의 단일 표본만 검사하려면 [선택 분배 검증](../validation/distribution.md) 사용.
