# GitHub Actions 데모 검증

- Workflow: `Demo validation` (`.github/workflows/demo.yml`).
- 제품 `Product CI` (`.github/workflows/ci.yml`)와 독립 실행.
- 역할: 데모 Docker 검사와 실제 serving 분배 검증. 배포·운영 데이터 변경 없음.
- 실행 전제: workflow와 필요한 데모·제품 파일이 원격 저장소의 실행 대상 revision에 포함된 상태. 로컬 파일 작성만으로 원격 실행되지 않음.

## 실행 조건

| 이벤트 | 조건 |
| --- | --- |
| push | `main`에 아래 대상 경로 변경 |
| pull request | 대상 branch가 `main`이며 아래 대상 경로 변경 |
| 수동 실행 | Actions의 `Demo validation`에서 입력값 지정 |

- 변경 감지 경로: `.demo/**`, `src/**`, 루트 `Makefile`·`Dockerfile`·`.dockerignore`·`pyproject.toml`·`requirements-dev.txt`, 해당 workflow 파일.
- 같은 workflow·ref의 새 실행 시작 시 이전 실행 취소.
- 실행 환경: `ubuntu-24.04`, job 제한 20분, 저장소 읽기 권한.

## 수동 입력

| 입력 | 기본값 | 의미 |
| --- | --- | --- |
| `requests` | `1000` | 순차 요청 수 |
| `v2_ratio` | `0.5` | 기대 v2 serving 비율 |
| `tolerance` | `0.08` | 절대 비율 허용 오차. 기본 ±8%p |

- push·PR: 기본값 사용.
- 수동 입력의 범위·통과 조건·전체 전환 예외: [분배 검증](distribution.md).

## 실행 단계

1. 소스 checkout.
2. `make demo-docker-test`: 데모 lint·format·타입·의존성 검사와 HTTP 통합 테스트.
3. `make demo-distribution PYTHON=python3`: 독립 Docker 환경에서 분배 검증.
4. 생성된 `summary.md`를 Actions 실행 요약에 반영.
5. 데모 테스트용 Compose 자원 정리.
6. 분배 산출물 artifact 업로드.

- 단계 2 실패 시 단계 3 미실행.
- 분배 실행기는 별도 고유 Compose project를 만들고 직접 정리.
- workflow의 정리 단계는 데모 테스트용 project만 대상으로 실행.
- 요약·정리·artifact 단계는 앞선 단계 실패 시에도 실행하도록 구성.
- 취소·강제 종료·runner 장애까지 정리·업로드 완료를 보장하지 않음.

## 결과와 실패 확인

- Actions job 결과: 테스트·분배·실행 오류 확인.
- 실행 요약: 기대 비율, 실제 backend 응답 수, HTTP status, 오류, 허용 구간 확인.
- artifact 이름: `demo-distribution-<run_id>-<run_attempt>`.
- 보관 기간: 7일.
- 업로드 파일: 각 실행의 `config.json`, `report.json`, `validation.json`, `summary.md`, `execution.log`, `containers.log`만 명시적으로 지정.
- 분배 단계 시작 전 실패·입력 오류: 요약이나 artifact가 없을 수 있음. 해당 step 로그 확인.
- 분배 실패: `validation.json`의 이유와 실행·컨테이너 로그 대조.
- 요청 수 증가로 시간 제한을 넘긴 경우 성공으로 처리하지 않음.

## 로컬 재현

- 프로젝트 루트에서 동일한 데모 명령 사용.

```sh
make demo-docker-test
DEMO_DISTRIBUTION_REQUESTS=1000 DEMO_DISTRIBUTION_V2_RATIO=0.5 \
  DEMO_DISTRIBUTION_TOLERANCE=0.08 make demo-distribution PYTHON=python3
```

- 로컬 통과와 GitHub runner 통과를 구분하여 확인.
- 검증 한계·smoke 범위: [데모 검증](README.md).
