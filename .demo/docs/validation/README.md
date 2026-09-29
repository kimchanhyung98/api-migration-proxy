# 데모 검증

- 목적: 합성 API·User·제품 Proxy의 연결, 응답 전달, 전환, 이벤트 저장 검증.
- 구분: 호스트 검사, 독립 Docker 테스트, 실행 중인 서비스 smoke, [분배 검증](distribution.md).
- 명령 실행 위치: 프로젝트 루트.

## 호스트·Docker 검사

```sh
make init
make demo-check
make demo-docker-test
```

- 호스트 전제: Python 3.12 이상과 프로젝트 개발 의존성 설치.
- `demo-check`: 데모 코드·테스트의 lint·format·타입 검사와 pytest 실행.
- `demo-docker-test`: 별도 데모 이미지에서 같은 검사, 의존성 검사, pytest 실행.
- Docker 테스트의 HTTP 프로세스: 테스트가 필요한 v1·v2·Proxy를 직접 시작·종료하므로 `demo-up` 선행 불필요.
- 제품 검증: 루트 `make check`, `make docker-test`로 별도 실행.

| 테스트 영역 | 검증 내용 |
| --- | --- |
| 합성 API | v1 평면 성공·빈 결과 오류, v2 공통 응답 구조, 동일 데이터 대조군, 지연·404·405 응답 |
| User | 실제 로컬 HTTP 응답 집계, redirect·네트워크 실패, 입력·출력 처리 |
| Proxy 연결 | 실제 v1·v2와 Proxy CLI 연결, 양쪽 serving, 이벤트 저장 및 재시작 후 보존 |
| 비교 결과 | 합성 문맥·기본 정책에서 구조 차이는 `different`, 500은 `execution_error`. 별도 필드 매핑 정책에서 `same`은 `matched`, `different`는 `different` |
| 분배 판정 | 허용 경계, 전체 전환, 오류·누락 표본, 잘못된 입력·집계 거부 |

## 실행 중인 서비스 smoke

```sh
make demo-up
make demo-smoke
make demo-down
```

- 기본 설정: serving v1 100%, shadow v2 100%.
- 전제: `v2_serve_ratio`가 `0` 또는 `1`, `shadow.sample_ratio=1`, `shadow.stopped=false`.
- 전체 v2 전환 후 확인: `make demo-smoke SERVING_BACKEND=v2`.
- `SERVING_BACKEND`: 응답 기대값 지정. Proxy 설정 변경 기능 없음.
- 별도 설정·포트를 사용한 경우 관련 Compose 명령에 동일한 환경변수 적용.
- 확인 대상: HTTP 200·404·500의 status·버전별 본문·버전 헤더, serving·shadow 역할, 요청 완료, SQLite 이벤트 저장.
- 본문 전달: [버전별 응답](../backends/README.md)의 v1 평면 성공·`result=[]` 오류, v2 `code`·`message`·`result` 구조 유지 확인. 버전 전환 시 본문을 통일하는 변환 없음.
- SQLite 접근: 테스트 컨테이너에서 이벤트 volume 읽기 전용 mount 및 읽기 전용 연결.
- 이벤트 대기: smoke 시작 이후 해당 route의 이벤트를 최대 10초간 확인.
- 일반 Proxy CLI의 정상·예상 404 응답 쌍: 데이터·권한 문맥 미설정으로 `not_comparable`. 예상 밖 500은 `execution_error`.
- 중간 serving 비율·shadow 중지 상태: 이 smoke의 대상이 아니며 [User 집계](../user/README.md)와 [분배 검증](distribution.md) 사용.

## 검증 경계

- User: 외부 HTTP 응답 관측. DB·Proxy 내부 조회 없음.
- smoke: HTTP 응답과 내부 이벤트 저장을 함께 확인. 동일 환경에 다른 요청을 섞지 않는 전용 실행 권장.
- Compose의 Proxy healthcheck: TCP 수락 확인. 비교 품질·저장소 준비·운영 readiness 보장 없음.
- `demo-docker-test`·분배 검증: 실제 HTTP 흐름 확인이며 브라우저 UI E2E와 구분.
- 제외: 실제 HTTPS·인증·업무 데이터·대표 부하·클라우드·다중 Proxy 운영 검증.
- `demo-down`: 저장 완료된 이벤트 volume 유지. 독립 분배 검증 자원은 실행 종료 시 제거.

## 관련 문서

- [분배 검증 기준·실행·산출물](distribution.md)
- [GitHub Actions 실행 조건·결과 확인](github-actions.md)
- [데모 문서 목차](../README.md)
