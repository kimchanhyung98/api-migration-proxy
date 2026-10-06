# HTTP 요청 전달과 사용자 응답

프록시는 선택한 serving의 공개 HTTP 계약을 보존하여 요청과 응답을 전달한다. serving은 사용자 응답을 제공하도록 이미 선택된 backend 역할이다. 비교용 캡처·정규화·shadow 결과가 호출자에게 전달할 status·body·계약 헤더를 바꾸어서는 안 된다.

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 프록시는 원본 HTTP 요청, 요청에 고정된 serving 목적지와 route별 헤더·body·오류 계약을 사용한다. |
| 결과·출력 | 호출자는 선택한 serving의 status·body·필수 헤더를 받거나, 전송 단계에 따라 정의된 프록시 실패를 받는다. |
| 실패·제한 | 응답 headers를 전송한 뒤에는 다른 status의 응답으로 바꾸어서는 안 된다. 비교용 정규화 결과를 사용자 body로 전달해서도 안 된다. |

## HTTP 전달 계약

| 항목 | 요구 동작 |
| --- | --- |
| URL | 운영자가 허용한 고정 backend를 사용한다. 명시적인 path 매핑이 없으면 원래 raw path를 전달하며, raw 인코딩과 query의 중복 키 의미를 보존한다. |
| 요청 body | 호출자가 보낸 body 바이트를 보존하여 serving에 전달한다. 동일 논리 요청을 양쪽에 실행하는 경우에는 각 실행이 독립 reader로 같은 바이트를 읽어야 한다. 전달 body를 임의로 파싱·재직렬화해서는 안 된다. |
| 헤더 | 인증·콘텐츠·조건부 요청 헤더를 보존하되, hop-by-hop 헤더와 `Connection`이 지정한 필드는 각 backend에 전달하기 전에 제거한다. |
| 요청별 상태 | 해당 요청의 허용된 cookie·인증·tenant 문맥만 전달한다. 공유 HTTP client에 남은 다른 요청의 상태나 이전 backend 응답 상태를 자동으로 추가해서는 안 된다. |
| Host·TLS | 목적지에 맞는 authority를 사용하고 HTTPS 인증서를 검증한다. Host 변경이 서명·가상 호스트·인증 계약에 영향을 주는 API는 적용 전에 해당 계약을 확인해야 한다. |
| 응답 | serving의 status·body·계약 헤더를 전달한다. 복수 `Set-Cookie` 필드를 하나로 임의 결합해서는 안 된다. |
| 압축 | 전달 body와 `Content-Encoding`·길이의 일관성을 유지한다. 비교용으로 압축을 해제하는 경우에는 해제된 데이터에 별도 캡처 상한을 적용한다. |
| redirect | backend HTTP 클라이언트는 자동으로 redirect를 따라가지 않는다. serving이 반환한 redirect status와 계약 헤더를 호출자에게 전달한다. |
| 조건부·본문 없는 응답 | ETag와 304의 의미를 보존하고, HEAD·204·304의 body 제한을 지킨다. 비교 정책의 `allow_empty_body`가 `true`이고 양쪽의 디코딩된 body가 모두 빈 경우에는 JSON 파싱을 생략한다. |
| 전송 중 실패 | 응답 headers를 보낸 뒤 실패하면 전송 불완전 상태를 기록한다. 성공 종료나 대체 status를 보내어 완전한 응답처럼 처리해서는 안 된다. |

전달 규칙의 기준은 [RFC 9110 메시지 전달 규칙](https://www.rfc-editor.org/rfc/rfc9110.html#section-7.6)이다. 실제 적용 환경에서는 선택한 HTTP 구현이 위 헤더·필드·body 계약을 지키는지 확인해야 한다.

## 요청 전달 절차

아래 절차는 route와 serving을 선택한 일반 HTTP 요청에 적용한다. shadow도 실행하는 경우에는 같은 논리 입력에 대해 이 전달 규칙을 적용하며, 두 backend의 완료 순서를 고정하지 않는다.

1. 프록시는 고정한 설정에서 목적지 origin을 가져온다. origin은 scheme·host·port로 이루어진 기준 주소이다. 호출자 입력을 목적지 호스트로 사용하지 않는다.
2. 명시적인 path 매핑이 있으면 등록된 parameter의 raw 표현을 매핑에 대입한다. 매핑이 없으면 raw path를 그대로 사용한다.
3. 프록시는 원래 query를 목적지 path에 연결한다. 파싱·재직렬화로 query의 중복 키나 인코딩을 바꾸지 않는다.
4. 프록시는 hop-by-hop 헤더와 `Connection` 지정 필드를 제거한다. hop-by-hop은 현재 연결에만 적용하는 필드이다. 그 밖의 허용된 인증·콘텐츠·조건부 헤더와 cookie는 현재 요청에서 가져온다.
5. 프록시는 목적지에 맞는 Host를 구성한다.
6. 프록시는 body 바이트를 선택한 backend에 전달한다. shadow를 위한 body 확보와 상한 초과 처리는 [입력 복제 절차](../shadow/parallel-execution.md)를 따른다.

목적지 path 매핑의 정적 segment에는 raw 비ASCII 문자와 DEL을 허용하지 않는다. 설정 검증에서 이를 거부하며, 이미 percent-encoded된 표현은 보존한다. 경로 등록 시 사용하는 ingress decoding과 backend에 전달하는 raw 표현을 구분해야 한다. 이 제한을 변경하지 않고 Unicode path 지원이나 ingress 정규화를 임의로 추가해서는 안 된다.

## 응답 전달과 실패 분기

프록시는 serving 응답 headers를 받으면 전달할 status와 헤더를 준비한다. body는 도착하는 순서대로 전달하며, 비교를 위해 전체 body가 모이거나 shadow가 끝날 때까지 기다리지 않는다. 비교용 캡처는 별도 상한을 적용한다.

| 실패 또는 종료 시점 | 호출자에게 보이는 처리 | 실행·관측 처리 |
| --- | --- | --- |
| serving 응답 headers를 아직 보내지 않았고 backend 실행이 timeout으로 끝남 | 프록시는 504 응답을 생성한다. | backend timeout과 프록시 생성 응답을 구분하여 기록한다. 반대편 응답으로 대체하지 않는다. |
| serving 응답 headers를 아직 보내지 않았고 연결 실패 등 전송 오류가 발생함 | 프록시는 502 응답을 생성한다. | 전송 오류를 기록하고 자동 재시도·fallback을 수행하지 않는다. |
| serving의 응답 headers를 이미 보낸 뒤 body 수신·전달이 실패함 | 시작한 응답을 불완전하게 종료한다. 다른 status나 정상 종료 body로 대체하지 않는다. | backend 실행 결과와 사용자 전송 실패를 각각 기록한다. |
| serving이 HTTP 오류 status를 포함한 완전한 응답을 반환함 | 선택한 serving의 status·body·계약 헤더를 그대로 전달한다. | route 계약에 따른 오류 분류는 별도로 수행한다. 비교 분류를 이유로 사용자 응답을 다시 작성하지 않는다. |
| 응답 전송이 끝나기 전에 호출자가 연결을 종료함 | 계속 전송할 수 없는 응답을 성공 완료로 처리하지 않는다. | 진행 중 실행의 취소를 시도하고 [수명·취소 규칙](../shadow/lifecycle-and-limits.md)에 따라 종료 사유를 기록한다. |

응답 headers를 보냈는지가 status 변경 가능 여부의 경계이다. 첫 body 바이트를 아직 보내지 않았다는 이유만으로 이미 보낸 status를 바꿀 수 있다고 판단해서는 안 된다. 느린 호출자에 대한 전송 timeout도 별도 예산으로 제한하며, 전송 완료의 관측 범위는 [수명 문서](../shadow/lifecycle-and-limits.md)를 따른다.

복제·캡처 상한은 공개 API가 허용하는 body 크기 제한과 목적이 다르다. 비교 상한을 넘었다는 이유만으로 원래 허용된 요청이나 응답을 오류로 바꾸어서는 안 된다. 지원 범위 안의 serving 전달은 유지하고, shadow 생략 또는 비교 불가 사유를 기록해야 한다.

### 업로드 중 조기 응답 전달

요청 body 송신과 backend 응답 수신은 함께 진행한다. Backend가 업로드 완료 전에 413 등의 최종 응답을 반환하면 남은 업로드 완료를 기다리지 않고 해당 status·body·전달 대상 헤더를 호출자에게 전달해야 한다. 이미 받은 최종 응답을 업로드 대기로 인한 502·504로 바꾸어서는 안 된다.

응답 headers를 받았다는 이유만으로 진행 중인 업로드를 중단하지 않는다. Backend 응답을 끝까지 받았거나 응답 연결을 닫을 때 남은 업로드 작업을 정리한다. 전체 요청 body를 확보하지 못한 경우에는 shadow를 실행하지 않으며, 일부 입력을 완전한 비교 입력으로 사용해서는 안 된다.

## 연결 재사용과 사용자 상태 격리

연결 풀 재사용이 허용된다는 사실은 cookie·인증 상태 공유를 허용한다는 뜻이 아니다. [HTTPX Client의 쿠키 보존·설정 병합 동작](https://www.python-httpx.org/advanced/clients/)이 현재 호출자의 입력에 이전 상태를 추가하지 않는지 별도로 확인해야 한다.

공유 cookie jar나 공유 인증 헤더를 매 요청마다 변경·초기화하는 방법을 상태 격리 수단으로 사용해서는 안 된다. 동시 요청 사이에 경쟁이 생길 수 있기 때문이다. serving의 `Set-Cookie`는 호출자에게 전달하지만 shadow의 `Set-Cookie`를 호출자나 후속 backend 요청에 자동 적용해서는 안 된다.

사용자 전달 경로는 압축된 원문 표현과 그 헤더를 함께 보존한다. 비교용 해제 데이터는 사용자 전달 경로와 분리해야 한다. 자동 content decoding을 사용한다면 해제된 body에 원래 압축 헤더나 길이가 남지 않는지 확인해야 한다.

## 적용 전 확인할 계약

API 소유자는 [RFC 9110 메서드 속성](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2)과 실제 업무 효과를 별도로 확인해야 한다. GET도 조회수 증가, 외부 호출 비용 또는 공유 quota 소비를 일으킬 수 있다. method만으로 양쪽 실행의 안전성을 판정해서는 안 된다.

기존 gateway·mirror가 있는 환경에서는 운영자가 각 계층의 Host/Authority 변경 여부를 확인하고 가상 호스트·인증·서명 계약과 대조해야 한다. 도구별 차이는 [프록시 도구 비교](../research/proxy-patterns-and-tools.md)를 참고한다.

관측 접근과 사용자 API 전달은 서로 다른 경계로 유지해야 한다. 관측 기능을 제공하기 위해 기존 사용자 API 경로를 예약하거나 가로채서는 안 된다.

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-07 | P0 | 공개 HTTP 계약을 보존해야 한다. | route별로 query, body, 필수 헤더·쿠키, status와 오류 전달 결과가 위 계약과 일치한다. |

## 검증 기준

[T-02, T-09, T-10, T-15, T-17, T-43, T-47, T-48](../validation/test-catalog.md)에 따라 raw path·중복 query, 청크 경계, 압축, cookie 격리, 조건부 응답과 headers 전후 실패를 검증한다. 비교 상한을 넘는 사례에서도 serving body 바이트와 헤더 의미가 보존되는지 확인해야 한다.

## 관련 기능

- [양쪽 실행](../shadow/parallel-execution.md)
- [인증과 데이터 책임](../_rules/identity-and-data-boundaries.md)
