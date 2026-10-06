# 로컬 데모

- 제품 Proxy를 로컬에서 확인하기 위한 User·더미 API 구현.
- `user/`: Proxy 호출과 응답 버전·오류·지연 집계.
- `v1/`: 평면 데이터와 단순 오류를 반환하는 API.
- `v2/`: `code`·`message`·`result` 구조로 응답하는 API.
- `tests/`: 개별 기능 테스트. `scenarios/`: Docker 순차 시나리오·분배·smoke 실행 도구.
- Proxy는 제품 코드 그대로 사용. 데모 코드·설정·테스트·Docker 구성은 이 디렉토리에서 관리.
- 실행 방법: [프로젝트 루트의 데모 안내](../README-DEMO.md).

## 기본값·선택 설정

- `.env` 없이 실행 가능. 기본 동작은 v1 응답 100%, shadow OFF.
- 선택 변경: `.env.example`을 `.env`로 복사. Compose는 `.demo/.env`를 자동 사용.
- 제품용 `.env`와 데모용 `.demo/.env`는 별개. 데모 변수는 Compose에서 제품의 `API_PROXY_` 환경변수로 전달.
- health는 Proxy 컨테이너 내부에서만 조회. 호스트에 health 포트를 공개하지 않음.

| 항목 | 설정·확인 가능한 내용 | 상세 안내 |
| --- | --- | --- |
| serving·shadow | `DEMO_V2_RATIO`, `DEMO_SHADOW_ENABLED`, Proxy 재생성 | [Proxy 설정](docs/proxy/README.md) |
| 관측 | 컨테이너 내부 health·backend 요청 수·이벤트 저장 | [Proxy 관측](docs/proxy/README.md#관측) |
| User 요청 | 요청 수·경로·timeout·보고서 경로 | [User](docs/user/README.md) |
| 실행 환경 | `.env` 기본값·포트·고급 JSON·이벤트 volume | [Docker](docs/runtime/README.md) |
| 개별 기능 테스트 | API·User·Proxy 연결·판정 기능 | [검증](docs/validation/README.md) |
| 순차 시나리오 | serving 전환·복귀, shadow ON/OFF, 단계별 관측 | [시나리오](docs/validation/scenarios.md) |

- 버전별 응답·선택 분배 검사·GitHub Actions: [문서 목차](docs/README.md).
