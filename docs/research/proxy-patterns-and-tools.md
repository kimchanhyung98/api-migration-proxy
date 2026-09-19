# 요청 복제·비교·재생 도구

- 목적: 도구별 기능과 API Migration Proxy의 구현 책임 구분
- 도구 선택: 미정; 대상 API의 전달·비교·운영 계약 충족 여부에 따라 결정
- 도입 조건: 사용할 버전의 설정·지원 범위 확인 및 실제 통합·부하 검증

## 구분할 기능

| 기능 | 수행하는 일 | 별도로 확인할 일 |
| --- | --- | --- |
| Serving 배정 | 사용자 응답을 제공할 backend 선택 | 사용자·세션별 배정 일관성, 실제 사용자 노출 |
| Request mirroring | 요청을 추가 backend에도 전송 | 반대편 응답 수집·비교·보존 여부 |
| 응답 비교 | 같은 논리 입력의 결과를 정책에 따라 판정 | 데이터 시점·권한·비결정성·실패·누락 |
| 코드 내부 실험 | 같은 애플리케이션의 기존·신규 코드 실행 및 관찰 | 사용자 요청 지연·예외·가변 상태 영향 |
| Capture·replay | 요청 기록 또는 실시간 복제 후 별도 대상에 재생 | 원본 응답 연결, 재생 시점의 인증·데이터·외부 효과 |

- 복제 성공·비교 성공·사용자 전환 성공: 별도 검증
- 미러 비율만으로 사용자 cohort·단계 승격·롤백의 구현 완료 판단 금지

## 도구별 역할

| 도구 | 실행 위치·주요 역할 | 응답·비교 경계 |
| --- | --- | --- |
| Envoy | HTTP router의 추가 backend 요청 | shadow 통계와 별도로 응답 캡처·비교·저장 연계 필요 |
| Istio | mesh의 라우팅·mirroring 설정 | mirror 응답 폐기; 해당 기능만으로 양쪽 body 비교 기록 미제공 |
| NGINX mirror | background subrequest 생성 | mirror 응답 무시; 본문 복제 시 선행 body 읽기 발생 |
| Diffy | 기존 구현 2개·신규 구현 1개의 결과 비교 | 기존 구현끼리의 차이로 비결정적 잡음 추정; 원본 프로젝트 archive 상태 |
| GitHub Scientist | 애플리케이션 내부의 코드 실험 | 기본 control 결과 반환, control·candidate의 무작위 순서 순차 실행 |
| GoReplay | 호스트 트래픽 캡처·실시간 또는 기록 후 재생 | 원본 응답 유지, 응답 추적과 별도 비교 로직으로 짝별 분석 가능 |

## Envoy request mirroring

- 복제 설정: [RequestMirrorPolicy](https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/route/v3/route_components.proto.html)로 별도 cluster에 요청 전송; serving 응답의 shadow 완료 대기 없음
- 표본·관측: `runtime_fraction`에 따른 복제 표본 제어와 shadow cluster 통계
- Host/Authority: 기본 `-shadow` 추가; `disable_shadow_host_suffix_append`로 비활성화 가능
- 응답 목적지: `WeightedCluster`로 선택; request mirroring의 추가 실행과 별도 설정
- 제한: HTTP CONNECT·upgrade 미지원, primary cluster 부재 시 shadow 미실행
- 적용 범위: 기존 Envoy 환경의 복제·트래픽 분배; 양쪽 응답 캡처·JSON 비교·이벤트 저장은 별도 연계 검증 필요
- 도입 검증: 고정 목적지·authority·서명 호환성, 역할별 한도, cohort·비율 정책, body 상한 초과·client 취소·응답 후 작업 수명

## Istio mirroring

- 복제 설정: [Istio Mirroring](https://istio.io/latest/docs/tasks/traffic-management/mirroring/)의 `mirrorPercentage`로 비율 지정; 공식 예제의 해당 필드 생략 시 전체 복제
- 응답 처리: 사용자 v1 응답을 유지하며 v2에 요청 복제; mirror 응답 폐기
- Host/Authority: mirror 요청에 `-shadow` 추가
- 비율 의미: `mirrorPercentage=10`은 사용자 10%의 v2 serving 배정과 다름
- 적용 범위: 기존 Istio 환경의 요청 복제; 비교용 양쪽 응답 수집 경로 별도 필요
- 도입 검증: 사용할 Istio·Gateway API 버전의 기능 차이, stable cohort, 상세 비교 연계

## NGINX mirror 모듈

- 복제 방식: [mirror 모듈](https://nginx.org/en/docs/http/ngx_http_mirror_module.html)의 background subrequest 생성; 해당 응답 무시
- 본문 처리: `mirror_request_body` 기본값 `on`; subrequest 생성 전 client body 읽기로 buffering 지연 가능
- body 복제 활성 시: `proxy_request_buffering` 등으로 지정한 unbuffered body 전달 비활성화
- body 복제 비활성 시: mirror 대상의 body·Content-Length 함께 조정; body를 생략한 요청의 동일 입력 비교 표본 처리 금지
- 적용 범위: 기존 NGINX 환경과 작은 입력 중심의 대상; 반대편 응답 수집·비교 경로 별도 필요
- 도입 검증: body 전달 계약, client 취소·대용량 body·자원 한도 동작

## Diffy

- 비교 방식: candidate·primary·secondary 세 인스턴스에 같은 요청 전달; 같은 기존 코드를 실행하는 primary·secondary의 자연 발생 차이와 candidate 차이를 대조
- 적용 범위: 비결정적 필드·데이터 변동에 따른 잡음을 구분하는 비교 설계
- 메서드 제한: POST·PUT·DELETE 기본 제외 및 명시적 허용 옵션 제공; 실제 부작용 검토의 대체 수단으로 사용 금지
- 유지보수: [Diffy 저장소](https://github.com/twitter-archive/diffy)는 보관 상태이며 유지보수 중단 명시
- 도입 검증: 사용할 배포판·fork의 유지보수 상태, 세 실행의 추가 부하, 잡음 추정의 의미, 사용자 응답 선택·점진 전환 계약

## GitHub Scientist

- 실험 방식: [Scientist](https://github.com/github/scientist/blob/main/README.md)로 Ruby 코드의 control·candidate를 감싸 결과·실행 시간·예외 비교
- 실행·응답: 무작위 순서의 순차 실행 후 기본 control 결과 반환; 병렬 네트워크 shadow와 구분
- 격리 한계: candidate timeout 격리 미제공, 내부 callback 오류의 기본 재전파; 사용자 응답과 후보 실행·결과 발행의 독립성 검증 필요
- 비교·발행: 사용자 정의 비교와 결과 발행 지원; 발행용 `clean`과 비교 규칙 별도 처리
- 적용 범위: 데이터 변경 없는 API 내부 로직의 리팩토링
- 도입 검증: 언어·라이브러리 선택, 애플리케이션 코드 변경 가능 여부, 후보 실행·발행의 지연·예외·비용 예산

## GoReplay

- 실행 방식: [GoReplay](https://goreplay.org/shadow-testing/)로 호스트 HTTP 트래픽 캡처 후 실시간 또는 파일 기록 후 재생; 후보 응답의 사용자 반환 없음
- 응답 비교: 원본·재생 응답 추적을 활성화하고 request ID로 연결한 뒤 middleware·분석 도구에서 처리
- TLS 제한: raw packet capture의 TLS 해독 불가; TLS 종료 뒤 평문 HTTP 캡처 지점 필요
- 격리 조건: 격리된 DB·자격증명, 외부 효과 차단, 필요 시 token·생성 ID 변환
- 비교 문맥: 기록 후 재생에 따른 데이터·시간·권한 변화 고려; 같은 요청 바이트만으로 동일 문맥 보장 불가
- 적용 범위: 이전 요청군 재현·사전 검증; 사용자 serving의 점진 전환과 구분
- 도입 검증: 설치 버전·기능, 캡처 허용 범위·보존 정책·손실률, 원본 응답 연결, 순서·세션 재현, 재생 부하 예산

## 도구 선택 기준

- 도구 채택 전: 최소 대상 API 한 개로 계약 충족 여부 검증

| 확인 대상 | 통과에 필요한 증거 | 관련 계약 |
| --- | --- | --- |
| 복제 실행 주체 | 기존 gateway와 자체 proxy가 동일 요청을 중복 shadow하지 않는 실행량 | 실행 계약 |
| 응답 수집 경로 | 하나의 request 식별자로 양쪽 결과·실패·미실행 연결 | 이벤트 모델 |
| 비율과 cohort | serving 비율·shadow 비율의 독립성, 같은 그룹의 키·revision 규칙 | 배정 계약 |
| 입력·authority | body·query·헤더·서명 의미 보존, mirror용 변경의 공개 계약 영향 | HTTP 전달 |
| 사용자 영향 | 본문 선행 읽기·복제·캡처·비교 비용, 느린 shadow의 응답 지연 영향 | 품질 기준 |
| 양방향 전환 | v2 serving 시 v1 shadow 실행·수집·종료까지 동일한 관측 계약 | 단계별 gate |
| 부작용과 재생 | 실제 실행 효과·인증·데이터 시점·외부 의존 검증 | 적격성 |

- 기존 도구의 mirror·통계 기능만 채택하는 안과 비교까지 자체 구현하는 안의 비용 비교 필요
- 미정 운영 수준·확장 기능: 실제 대상 API의 필요와 추가 부하·복구 계약을 검증한 뒤 별도 결정
