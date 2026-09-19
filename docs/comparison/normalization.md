# 응답 정규화와 의미 비교

- 필드·타입·배열·허용 오차를 명시한 정책 적용
- 의도적인 차이와 회귀 구분

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 비교 가능한 완전 응답, route별 비교 정책 revision, 명시된 제외·매핑 규칙 |
| 결과·출력 | matched 또는 different, 차이 경로와 정책 적용 결과 |
| 실패·제한 | 정규화에 의한 중요 회귀 은폐 금지, 캡처 불완전·파싱 한계의 비교 불가 처리 |

## 기본 비교 규칙

| 대상 | 기본 정책 제안 |
| --- | --- |
| status | 정확히 같은 코드. 의도적 호환 차이는 route별 명시 |
| 응답 class | 성공·예상 거절의 의미 비교. 예상 밖 오류는 별도 실패 |
| 헤더 | 콘텐츠 유형·캐시·업무상 의미 있는 헤더를 명시적으로 선택 |
| JSON 객체 | 키 순서는 무시, 필드 존재·타입·값은 비교 |
| JSON 배열 | 순서와 중복을 보존. 명시된 경로만 집합 또는 ID 기준 비교 |
| 숫자 | 의미를 보존하는 표현으로 비교. float 변환으로 큰 정수·금액의 정밀도를 잃지 않음 |
| 문자열 | 기본 정확 비교. 시간대·대소문자·Unicode 정규화는 필요한 경로만 |
| null·누락 | 서로 다르게 처리 |
| 빈 body | HEAD·204·304 등 계약상 허용인지 확인한 뒤 비교 |
| 파싱 오류 | 유효한 전체 JSON을 기대한 경우 비교 불가로 기록 |
| 비결정적 필드 | 지정된 필드만 제외. 제외 목록과 정책 revision 기록 |

- JSON parser의 깊이·크기·숫자 처리 한도 설정
- 중복 키 등 모호한 입력: 임의 값 선택 후 일치 처리 금지, `not_comparable` 분류
- 필드 경로·JSON 객체 키에 포함된 민감 정보도 저장 전 검토
- 제외 필드·허용 오차·매핑 적용 내역과 정책 revision 기록; 원문 값 저장과 분리

## 예시: 상품 상세 API

- 가상 상품 데이터 예시

```json
{
  "v1": {"id": "item-101", "price": 12000, "available": true, "request_id": "a"},
  "v2": {"id": "item-101", "price": 12000, "available": true, "request_id": "b"}
}
```

- 정책에 `request_id`만 제외하도록 명시한 경우: `matched`
- v2의 `price`가 12500인 경우: `different`, 차이 경로 `/price` 기록
- 가격의 무조건적 오차 허용·timestamp 또는 ID 이름의 일괄 제외 금지

| 변형 | 판정 예시 |
| --- | --- |
| 키의 순서만 변경 | `matched` |
| `available: null`과 필드 누락 | `different` |
| 배열 순서 변경 | 기본 `different`; 집합 의미가 명시된 필드만 별도 규칙 |
| v2 timeout | `execution_error`, v2 timeout |
| 같은 예상 밖 500 | `execution_error`, both unexpected error |
| 동일한 예상 거절 403 | 비교 조건이 맞으면 `matched`, class=`expected_rejection` |
| v2가 다른 테넌트 데이터 반환 | 권한 불변식 위반. 일반 불일치율과 별도로 중지 판단 |
| 응답 크기 상한 초과 | `not_comparable`, capture oversized |

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-18 | P0 | status·필수 헤더·JSON 의미 비교 | 객체 키 순서만 기본 무시, 배열 순서·null·누락·타입 차이 유지 |
| FR-19 | P0 | 제외 필드·허용 오차·좁은 매핑을 버전 관리 | 제외 전후 차이와 정책 revision 추적, 업무 핵심 필드의 무단 제외 방지 |
| FR-26 | P1 | 업무별 비교 확장 | 필요한 API에서만 집합·순위·비결정적 결과 규칙을 계약 테스트로 추가 |

## 검증 계획

- 구현 후 검증: [T-10, T-18, T-19, T-20, T-40](../validation/test-catalog.md)

## 관련 기능

- [비교 결과 분류](context-and-outcomes.md)
- [허용 상세 데이터](../collection/detail-sampling.md)
