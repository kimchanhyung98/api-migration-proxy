# 로컬 데모 실행

- 제품 Proxy를 확인할 수 있도록 User·더미 v1·v2 API를 `.demo/`에 구현.
- Docker Engine·Compose·Make 준비 후 프로젝트 루트에서 실행.
- `.env` 작성 없이 기본값으로 실행 가능. 선택 설정은 `.demo/.env.example`을 `.demo/.env`로 복사해 변경.

## 기본 실행

```sh
make demo-up
make demo-request
```

- 기본 동작: v1 응답 100%, shadow OFF.
- `demo-up`: 제품 Proxy와 더미 v1·v2 API 이미지 빌드·실행.
- `demo-request`: 응답 버전·오류·지연 집계. 결과는 `.demo/results/user.json`.
- 브라우저 확인: `http://127.0.0.1:8080/items/different`.
- 상태 확인: Proxy 컨테이너 내부의 `/healthcheck`. [조회 방법·검증 범위](.demo/docs/proxy/README.md#관측).
- 설정 변경 후 적용: `make demo-reload`. 설정 검사 성공 후 Proxy만 재생성.

## 순차 시나리오

```sh
make demo-scenario-serving
make demo-scenario-shadow
```

- serving: v1 100% → 50:50 → v2 100% → v1 100%, 단계마다 1,000회 요청, shadow OFF.
- shadow: v1 serving에서 ON → OFF, v2 serving에서 ON → OFF, 단계마다 100회 요청.
- 각 명령은 독립 환경을 만들고 단계 사이에 backend·이벤트 volume을 유지. User 응답·backend 요청 수·Proxy 정상 종료 후 SQLite 이벤트를 함께 검증.
- 결과: `.demo/results/scenarios/`. [단계·통과 기준·산출물](.demo/docs/validation/scenarios.md).

## 종료

```sh
make demo-down
```

- 수동 데모의 이벤트 volume은 유지.
- 데모 구성·선택 설정: [.demo/README.md](.demo/README.md).
- 상세 실행·검증 방법: [.demo/docs/README.md](.demo/docs/README.md).
