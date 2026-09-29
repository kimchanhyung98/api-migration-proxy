# 로컬 데모 실행

- API Proxy를 확인할 수 있도록 User·더미 v1·v2 API를 `.demo/`에 미리 구현.
- Docker Engine·Compose·Make 준비 후 프로젝트 루트에서 실행.

## 실행

```sh
make demo-up
make demo-request
```

- `demo-up`: 제품 Proxy와 더미 v1·v2 API 이미지 빌드·실행.
- `demo-request`: Proxy에 요청을 보내 응답 버전·오류·지연 집계.
- 브라우저 확인: `http://127.0.0.1:8080/items/different`.
- 호출 결과: `.demo/results/user.json`.

## 종료

```sh
make demo-down
```

- 데모 구성·선택 설정: [.demo/README.md](.demo/README.md).
- 상세 실행·검증 방법: [.demo/docs/README.md](.demo/docs/README.md).
