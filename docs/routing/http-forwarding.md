# HTTP 요청 전달과 사용자 응답

- 목적: 선택된 serving의 공개 HTTP 계약 보존 및 요청·응답 전달

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 원본 요청, 고정된 serving 목적지, route별 헤더·body·오류 계약 |
| 결과·출력 | 선택한 serving의 status·body·필수 헤더 또는 정의된 전달 실패 |
| 실패·제한 | 본문 전송 시작 후 오류의 정상 응답 대체 금지; 비교 정규화에 따른 사용자 응답 수정 금지 |

## HTTP 전달 계약

| 항목 | 요구 동작 |
| --- | --- |
| URL | 고정된 허용 backend·명시적 path 매핑 사용; raw path 인코딩·query 중복 키 의미 보존 |
| 요청 body | 각 실행의 독립 reader로 동일 논리 바이트 소비; 파싱·재직렬화에 따른 임의 의미 변경 금지 |
| 헤더 | 인증·콘텐츠·조건부 요청 헤더 보존; hop-by-hop 헤더와 `Connection` 지정 필드의 전달 전 제거 |
| 요청별 상태 | 현재 요청의 허용된 cookie·인증·tenant 문맥 보존; 공유 HTTP client에 축적된 다른 요청·backend 응답 상태의 자동 혼입 금지 |
| Host·TLS | 목적지에 맞는 authority·인증서 검증; 서명 대상 값 변경 시 별도 계약 확인 |
| 응답 | serving status·body·계약 헤더 보존; 복수 `Set-Cookie` 필드의 임의 결합 금지 |
| 압축 | 전달 표현의 `Content-Encoding`·길이 일관성 유지; 비교용 해제 데이터에 별도 캡처 상한 적용 |
| redirect | backend HTTP 클라이언트의 자동 redirect 기본 비활성; serving의 redirect 계약을 호출자에게 전달 |
| 조건부 응답 | ETag·304·HEAD·204의 body 유무·의미 확인; 정상적인 빈 body와 JSON 파싱 실패 구분 |
| 본문 전송 중 실패 | headers 전송 후 다른 status의 정상 응답 대체 금지; 전송 불완전·사용자 영향 기록 |

- 전달 기준: [RFC 9110 메시지 전달 규칙](https://www.rfc-editor.org/rfc/rfc9110.html#section-7.6)
- 라이브러리 확인: 실제 선택한 HTTP 구현의 헤더·필드 전달 동작 검증
- 연결 풀 재사용과 사용자 상태 격리: 별도 계약으로 검증; HTTPX Client의 쿠키 보존·설정 병합이 호출자 입력에 이전 상태를 추가하지 않도록 요청 구성 방식 확인. [HTTPX Clients](https://www.python-httpx.org/advanced/clients/)
- 공유 cookie jar·인증 헤더의 매 요청 변경·초기화: 동시 요청과 경쟁하므로 상태 격리 수단으로 사용 금지; serving의 `Set-Cookie`는 호출자에게 전달하되 shadow 쿠키를 후속 요청에 적용하지 않음
- 자동 content decoding 사용 시: 해제된 body와 원래 압축 헤더의 불일치 방지; 원문 전달과 비교용 해제 경로를 구분
- 기존 mirror의 Host/Authority 변경 여부: 가상 호스트·인증·서명 계약과 대조; [도구별 동작](../research/proxy-patterns-and-tools.md) 확인
- 메서드 검토: [RFC 9110 메서드 속성](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2)과 실제 업무 효과 별도 확인
- GET의 부수 효과: 조회수 증가·외부 호출 비용·공유 quota 소비 가능성 확인
- 복제·캡처 상한과 공개 API 제한: 별도 구분; 비교 한도 초과만으로 기존에 허용된 요청의 오류 전환 금지

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-07 | P0 | 공개 HTTP 계약 보존 | query, body, 필수 헤더·쿠키, status, 오류 전달을 route별 검증 |

## 검증 계획

- 구현 후 검증: [T-02, T-09, T-10, T-15, T-17, T-43](../validation/test-catalog.md)

## 관련 기능

- [양쪽 실행](../shadow/parallel-execution.md)
- [인증과 데이터 책임](../_rules/identity-and-data-boundaries.md)
