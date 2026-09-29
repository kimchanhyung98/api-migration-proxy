# 로컬 데모

- 제품 Proxy를 로컬에서 확인하기 위한 User·더미 API 구현.
- `user/`: Proxy 호출과 응답 버전·오류·지연 집계.
- `v1/`: 평면 데이터와 단순 오류를 반환하는 API.
- `v2/`: `code`·`message`·`result` 구조로 응답하는 API.
- Proxy는 제품 코드 그대로 사용. 데모 코드·설정·테스트·Docker 구성은 이 디렉토리에서 관리.
- 실행 방법: [프로젝트 루트의 데모 안내](../README-DEMO.md).

## 선택 설정·검증

- 기본 설정: v1 응답 100%, v2 shadow 표본 비율 100%.

| 항목 | 설정·확인 가능한 내용 | 상세 안내 |
| --- | --- | --- |
| v1/v2 응답 분배 | 요청별 비율 조정·전체 전환·복귀 | [Proxy 설정](docs/proxy/README.md) |
| 섀도잉 | ON/OFF·표본 비율 조정. 응답 분배와 독립 설정 | [Proxy 설정](docs/proxy/README.md) |
| User 요청 | 요청 수·경로·timeout·보고서 경로 | [User](docs/user/README.md) |
| 실행 환경 | 포트·설정 파일·이벤트 저장 | [Docker](docs/runtime/README.md) |
| 검증 | 응답 전달·shadow·저장 확인, 1,000회 응답 분배 검사 | [검증](docs/validation/README.md) |

- 버전별 응답·상세 절차·GitHub Actions: [문서 목차](docs/README.md).
