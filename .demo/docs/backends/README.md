# 합성 API v1·v2

- 목적: 의도적으로 다른 v1·v2 응답으로 Proxy의 응답 전달·버전 전환·오류 처리·비교 동작 검증.
- 공통 구현: [backend.py](../../backend.py)의 `create_app(version)`.
- 버전별 진입점: [v1 앱](../../v1/app.py), [v2 앱](../../v2/app.py). 공통 실행 코드에서 버전에 따라 서로 다른 응답 본문 생성.
- 실행: 각 버전을 독립 컨테이너로 시작. 실제 업무 시스템·인증·DB 연동 없음.
- 공통 범위: HTTP 경로·상태 코드·버전 헤더 형식. v1은 성공 데이터를 평면으로 반환하고, v2는 정의된 성공 응답과 404·405·합성 500 오류 응답을 `code`·`message`·`result`로 구성.

## 응답 구조

- v1 성공: 데이터 직접 반환. item은 `id`·`name`, health는 `status` 사용.
- v1 실패: HTTP 상태 코드로 실패 구분, 본문은 `{"result":[]}`만 반환.
- v2 성공: 문자열 `code="0000"`, `message="Success"`, `result`에 실제 데이터 배치.
- v2 실패: 오류 `code`·`message`, 빈 배열 `result=[]` 반환.
- HTTP 상태 코드와 본문의 `code`는 별도 정보. 오류 본문을 제공해도 HTTP 404·500을 200으로 변경하지 않음.
- 단건 item은 `result` 바로 아래 `id`·`name` 배치. 추가 `item` 래퍼·`label` 필드 사용 없음.

```json
{
  "code": "0000",
  "message": "Success",
  "result": {
    "id": "sample",
    "name": "Synthetic item"
  }
}
```

## 경로와 응답

| GET 경로 | 상태 | v1 응답 | v2 응답 |
| --- | --- | --- | --- |
| `/health` | 200 | `{"status":"alive"}` | `{"code":"0000","message":"Success","result":{"status":"alive"}}` |
| `/items/same` | 200 | `{"id":"same","name":"Synthetic item"}` | `{"code":"0000","message":"Success","result":{"id":"same","name":"Synthetic item"}}` |
| `/items/different` | 200 | `{"id":"different","name":"Synthetic item"}` | `{"code":"0000","message":"Success","result":{"id":"different","name":"Changed item"}}` |
| `/items/missing` | 404 | `{"result":[]}` | `{"code":"NOT_FOUND","message":"Not found.","result":[]}` |
| `/items/error` | 500 | `{"result":[]}` | `{"code":"SYNTHETIC_FAILURE","message":"synthetic failure","result":[]}` |
| `/items/slow` | 200 | `{"id":"slow","name":"Synthetic item"}` | 0.5초 대기 후 `{"code":"0000","message":"Success","result":{"id":"slow","name":"Synthetic item"}}` |
| `/items/{그 외 값}` | 200 | `{"id":"<전달한 값>","name":"Synthetic item"}` | `{"code":"0000","message":"Success","result":{"id":"<전달한 값>","name":"Synthetic item"}}` |

- `/items/{item_id}` 응답: `Content-Type: application/json`, `x-backend-version: v1` 또는 `v2` 포함.
- `/health`: 생존 확인 전용, 버전 헤더 없음.
- `/health`를 Proxy로 요청할 경우 등록된 전환 route가 아니므로 기본 v1에 전달. 분배 검증은 `/items/different` 사용.
- 등록되지 않은 경로의 404·지원하지 않는 메서드의 405에도 버전별 오류 구조 적용. 405의 `Allow` 헤더 유지.
- v2의 공통 404: `code="NOT_FOUND"`, `message="Not found."`; 공통 405: `code="METHOD_NOT_ALLOWED"`, `message="Method not allowed."`. 두 응답 모두 `result=[]`.
- `GET /__demo/requests`: `{"version":"v1","items":0}` 또는 v2 버전의 누적 item 요청 수 반환. 데모 관측 전용이며 위 응답 비교·분배 대상에서 제외.
- item handler에 진입한 호출을 집계. health·counter 조회는 집계하지 않으며, backend 재시작 시 0으로 초기화.
- Swagger UI·ReDoc·OpenAPI 경로 비활성화.

## 검증 역할

- `same`·일반 item: 실제 데이터는 같고 응답 구조만 다른 대조군. v1 전체 본문과 v2의 `result` 내용은 같지만 전체 JSON은 다름.
- `different`: 응답 구조에 더해 `name` 값도 의도적으로 변경. 구조 차이와 실제 데이터 차이를 구분해 확인.
- Proxy: 선택한 serving 본문 그대로 전달. v2의 공통 응답 구조를 제거하거나 v1과 같은 형식으로 변환하지 않음.
- `missing`, `error`: 동일 상태 코드에서도 버전별 오류 본문을 그대로 전달하는지 확인. 분배 검증의 성공 경로로 사용하지 않음.
- `slow`: v2의 의도적 지연 제공. 실제 응답 시간은 0.5초 대기에 처리·통신 시간이 추가된 값.
- 신뢰하는 합성 문맥을 명시한 기본 비교 테스트: `same`·일반 item·`different`·`missing` 모두 응답 구조가 달라 `different`, 예상 밖 500인 `error`는 `execution_error` 확인.
- 별도 필드 매핑 비교 테스트: v2의 `/result/id`·`/result/name`을 `/id`·`/name`으로 매핑하고 공통 응답 메타데이터·매핑 후 빈 `result` 래퍼를 비교에서 제외. `same`은 `matched`, `different`는 `different` 확인.
- 필드 매핑은 해당 테스트의 비교 정책에만 적용. 데모 실행 설정과 사용자에게 전달하는 본문은 변경하지 않음.
- 일반 Proxy CLI: 데이터·권한 문맥 미설정으로 정상 응답 쌍은 `not_comparable`. 합성 데이터라는 이유만으로 비교 가능하다고 판단하지 않음.
- 환경변수 기본 계약의 404는 `not_comparable`·`contract_class_unknown`. 고급 JSON 예시에서 예상 거부로 등록한 404는 `not_comparable`·`authorization_context_unknown`.
- 500은 두 설정 모두 `execution_error`·`unexpected_api_error`.

관련 문서: [Docker 주소·실행](../runtime/README.md), [Proxy 전환](../proxy/README.md), [로컬 검증](../validation/README.md).
