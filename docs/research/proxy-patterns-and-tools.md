# 요청 복제·비교·재생 도구

이 문서는 요청 복제, 응답 비교, 요청 재생 도구가 제공하는 기능과 API Migration Proxy가 담당하는 기능을 구분한다. 아래 도구의 채택 여부는 미정이다. 도입을 결정하려면 사용할 버전의 설정과 지원 범위를 확인하고, 대상 API의 전달·비교·운영 계약을 실제 통합과 대표 부하에서 충족하는지 확인해야 한다.

serving은 사용자 응답을 제공하는 역할이고, shadow는 반대편을 실행하되 그 응답을 사용자에게 반환하지 않는 역할이다. 두 역할은 v1 또는 v2에 배정할 수 있다. 아래의 v1 serving·v2 mirror 설명은 각 도구의 동작 예시이며, 제품에서 역할을 버전에 고정한다는 의미가 아니다. 다른 공통 용어는 [용어 정의](../_rules/glossary.md)를 따른다.

## 구분할 기능

| 기능 | 수행하는 일 | 별도로 확인할 일 |
| --- | --- | --- |
| Serving 배정 | 사용자 응답을 제공할 backend 하나를 선택한다. | 같은 사용자·세션을 일관되게 배정하는지, 선택한 backend의 응답이 실제 사용자에게 전달되는지 확인한다. |
| Request mirroring | 원래 요청을 처리하는 backend 외의 대상에도 요청을 전송한다. | 반대편 응답을 수집하고 비교하며 보존하는 기능이 있는지 각각 확인한다. |
| 응답 비교 | 같은 논리 입력의 결과를 비교 정책에 따라 판정한다. | 데이터 시점, 권한, 비결정적 값, 실행 실패, 관측 누락을 판정에서 어떻게 구분하는지 확인한다. |
| 코드 내부 실험 | 같은 애플리케이션 안에서 기존·신규 코드를 실행하고 결과를 관찰한다. | 후보 실행이 사용자 지연, 예외, 양쪽 코드가 공유하는 가변 상태에 미치는 영향을 확인한다. |
| Capture·replay | 요청을 캡처하여 실시간으로 복제하거나 기록한 뒤 별도 대상에 다시 실행한다. | 원본 응답과 재생 응답을 연결할 수 있는지, 재생 시점의 인증·데이터·외부 효과가 허용되는지 확인한다. |

요청 복제 성공, 응답 비교 성공, 사용자 전환 성공은 각각 다른 결과이므로 별도 증거로 확인해야 한다. 미러 비율을 설정할 수 있다는 사실만으로 사용자 cohort 배정, 전환 단계 승격, 롤백이 구현됐다고 판단해서는 안 된다.

## 도구별 역할

| 도구 | 실행 위치·주요 역할 | 응답·비교 경계 |
| --- | --- | --- |
| Envoy | HTTP router에서 추가 backend로 요청을 보낸다. | shadow 통계와 별도로 양쪽 응답 캡처, 비교, 저장을 연결해야 한다. |
| Istio | 서비스 mesh에서 라우팅과 mirroring을 설정한다. | mirror 응답은 폐기한다. mirroring만으로 양쪽 본문의 비교 기록을 제공하지 않는다. |
| NGINX mirror | 추가 처리를 위한 background subrequest를 생성한다. | mirror 응답을 무시하며, 본문 복제를 켜면 원래 요청의 body를 먼저 읽는다. |
| Diffy | 기존 구현 2개와 신규 구현 1개의 결과를 비교한다. | 기존 구현끼리의 차이로 비결정적 잡음을 추정한다. 원본 프로젝트는 archive 상태다. |
| GitHub Scientist | 애플리케이션 내부에서 기존·후보 코드의 실험을 수행한다. | 기본적으로 control 결과를 반환한다. control과 candidate는 무작위로 정한 순서에 따라 순차 실행한다. |
| GoReplay | 호스트 트래픽을 캡처하여 실시간으로 또는 기록 후 재생한다. | 원래 사용자 응답을 유지한다. 응답 추적과 별도 비교 로직을 연결하면 원본·재생 응답을 쌍으로 분석할 수 있다. |

## Envoy request mirroring

[RequestMirrorPolicy](https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/route/v3/route_components.proto.html)는 요청을 별도 cluster에 보낸다. serving 응답은 shadow 완료를 기다리지 않는다. `runtime_fraction`으로 복제 표본을 제어할 수 있으며, shadow cluster의 통계를 제공한다.

- mirror 요청의 Host/Authority에는 기본적으로 `-shadow`를 추가한다. `disable_shadow_host_suffix_append`로 이 변경을 끌 수 있다.
- `WeightedCluster`는 사용자 응답을 제공할 목적지를 선택한다. 추가 요청을 만드는 request mirroring과는 별도 설정이다.
- HTTP CONNECT와 upgrade는 mirroring 지원 범위에서 제외되며, primary cluster가 없으면 shadow를 실행하지 않는다.

본 제품에서 검토할 범위는 기존 Envoy 환경의 요청 복제와 트래픽 분배다. Envoy의 shadow 통계가 양쪽 응답 캡처, JSON 비교, 이벤트 저장을 대신한다고 해석해서는 안 된다. 도입 담당자는 고정 목적지, Host/Authority 및 서명 호환성, 역할별 실행 한도, cohort와 비율 정책을 확인해야 한다. body 상한 초과, client 취소, serving 응답 이후 작업이 남는 조건에서도 전달과 자원 정리가 계약대로 동작하는지 확인해야 한다.

## Istio mirroring

[Istio Mirroring](https://istio.io/latest/docs/tasks/traffic-management/mirroring/)은 `mirrorPercentage`로 요청 복제 비율을 지정한다. 인용한 공식 예제에서는 이 필드를 생략하면 전체 요청을 복제한다. 예제의 v1 응답은 사용자에게 그대로 반환하고, v2의 mirror 응답은 폐기한다. mirror 요청의 Host/Authority에는 `-shadow`를 추가한다.

`mirrorPercentage=10`은 요청의 10%를 추가 실행 대상으로 선택한다는 의미이며, 사용자 10%에게 v2 응답을 제공한다는 의미가 아니다. 기존 Istio 환경의 복제 기능을 채택하더라도 비교를 위한 양쪽 응답 수집 경로는 별도로 연결해야 한다. 도입 담당자는 사용할 Istio와 Gateway API 버전의 기능 차이, 동일 사용자·세션의 안정 배정 여부, 상세 비교 연계 여부를 확인해야 한다.

## NGINX mirror 모듈

[mirror 모듈](https://nginx.org/en/docs/http/ngx_http_mirror_module.html)은 background subrequest를 만들고 그 응답을 무시한다. `mirror_request_body`의 기본값은 `on`이다. 이때 subrequest를 만들기 전에 client body를 읽으므로, 본문을 먼저 모으는 buffering으로 지연이 생길 수 있다.

- body 복제를 켜면 `proxy_request_buffering` 등으로 지정한 unbuffered body 전달이 비활성화된다. 기존 전달 설정이 그대로 유지된다고 가정해서는 안 된다.
- body 복제를 끄면 mirror 대상에 전달할 body와 `Content-Length`를 일관되게 조정해야 한다. 원래 body를 생략한 mirror 요청을 동일 입력의 비교 표본으로 분류해서는 안 된다.

본 제품에서는 기존 NGINX 환경과 작은 입력 중심의 API를 적용 후보로 검토한다. 반대편 응답의 수집과 비교는 별도 경로가 필요하다. 도입 담당자는 body 전달 계약과 함께 client 취소, 대용량 body, 자원 한도 도달 시 동작을 확인해야 한다.

## Diffy

Diffy는 candidate, primary, secondary라는 세 인스턴스에 같은 요청을 전달한다. primary와 secondary는 같은 기존 코드를 실행한다. 두 기존 인스턴스 사이의 자연 발생 차이를 신규 candidate와의 차이에 대조하여 비결정적 필드나 데이터 변동에 의한 잡음을 추정한다.

POST, PUT, DELETE는 기본적으로 제외하며 명시적 허용 옵션을 제공한다. 이 메서드 제한을 실제 부작용 검토의 대체 수단으로 사용해서는 안 된다. [Diffy 저장소](https://github.com/twitter-archive/diffy)는 보관 상태이며 유지보수 중단을 명시한다.

본 제품에서는 비결정적 차이를 분리하는 비교 설계의 참고자료로 사용한다. 도입을 검토한다면 선택한 배포판 또는 fork의 유지보수 상태와 요청당 세 실행의 추가 부하를 확인해야 한다. 잡음 추정이 업무 차이를 숨기지 않는지도 확인해야 한다. 사용자 응답 선택과 점진 전환을 연결하는 방식은 별도로 확인해야 한다.

## GitHub Scientist

[Scientist](https://github.com/github/scientist/blob/main/README.md)는 Ruby 코드의 기존 동작(control)과 후보 동작(candidate)을 감싸 결과, 실행 시간, 예외를 비교한다. 두 동작을 무작위로 정한 순서에 따라 순차 실행하고, 기본적으로 control 결과를 반환한다. 따라서 네트워크 요청을 병렬 실행하는 shadow와 같은 실행 모델이 아니다.

candidate의 timeout을 격리하는 기능은 제공하지 않으며, 내부 callback의 오류는 기본적으로 다시 전파한다. 사용자 정의 비교와 결과 발행을 지원하지만, 발행용 데이터 정리 함수 `clean`과 비교 규칙은 별도로 처리한다. 발행 전에 데이터를 정리한다고 비교 입력도 같은 방식으로 정리된다고 가정해서는 안 된다.

본 제품에서 참고할 적용 범위는 데이터를 변경하지 않는 API 내부 로직의 리팩토링이다. 도입 담당자는 언어와 라이브러리의 적합성, 애플리케이션 코드 수정 가능 여부, 후보 실행과 결과 발행의 지연·예외·비용 예산을 확인해야 한다. control 결과를 반환한다는 사실만으로 후보 실행이나 결과 발행이 사용자 응답에 영향을 주지 않는다고 판단해서는 안 된다.

## GoReplay

[GoReplay](https://goreplay.org/shadow-testing/)는 호스트의 HTTP 트래픽을 캡처하여 실시간으로 또는 파일에 기록한 뒤 재생한다. 후보의 응답을 원래 사용자에게 반환하지 않는다. 원본·재생 응답의 추적을 활성화하고 request ID로 연결하면 middleware나 분석 도구에서 두 응답을 비교할 수 있다.

- raw packet capture는 TLS를 해독하지 못한다. 이 방식을 사용할 경우 TLS 종료 뒤의 평문 HTTP 캡처 지점이 필요하다.
- 재생 대상에는 격리된 DB와 자격증명을 사용하고 외부 업무 효과를 차단해야 한다. 기록한 token이나 생성 ID를 재생 대상이 그대로 해석할 수 없다면, 변환 계약을 정해야 한다.
- 기록 시점과 재생 시점의 데이터, 시간, 권한이 달라질 수 있다. 요청 바이트가 같다는 사실만으로 비교 문맥이 같다고 판단해서는 안 된다.

본 제품에서 검토할 범위는 과거 요청군의 재현과 사전 검증이며, 사용자 serving의 점진 전환과는 별도 기능이다. 도입 담당자는 설치 버전과 지원 기능, 캡처 허용 범위, 보존 정책, 손실률, 원본 응답 연결, 순서·세션 재현, 재생 부하 예산을 확인해야 한다.

## 도구 선택 기준

아래 항목은 도구의 도입 검증 담당자에게 적용한다. 먼저 대상 API를 최소 한 개 선정하십시오. 선정한 API에서 아래 증거를 확인하십시오. 표의 행 순서는 실행 순서를 뜻하지 않는다.

확인하지 못한 기능은 미확인으로 기록하십시오. 도구 이름이나 설정의 존재만으로 통과 처리하지 마십시오.

| 확인 대상 | 통과에 필요한 증거 | 관련 계약 |
| --- | --- | --- |
| 복제 실행 주체 | 기존 gateway와 자체 Proxy가 동일 요청을 중복 shadow하지 않는다는 실행량 증거를 확보하십시오. | [실행 계약](../shadow/parallel-execution.md) |
| 응답 수집 경로 | 하나의 논리 요청 식별자로 양쪽 결과, 실행 실패, 미실행을 연결한 기록을 확보하십시오. | [이벤트 모델](../collection/event-model.md) |
| 비율과 cohort | serving 비율과 shadow 비율의 독립성을 확인하십시오. 배정 결과가 같은 cohort 그룹의 키와 revision 규칙을 충족하는지 확인하십시오. | [배정 계약](../routing/serving-and-cohorts.md) |
| 입력·authority | body, query, 헤더, 서명의 의미가 보존되는지 확인하십시오. mirror를 위한 Host/Authority 변경이 공개 계약에 미치는 영향을 기록하십시오. | [HTTP 전달](../routing/http-forwarding.md) |
| 사용자 영향 | 본문 선행 읽기, 복제, 캡처, 비교의 비용을 측정하십시오. 느린 shadow가 사용자 지연에 미치는 영향을 측정하십시오. | [품질 기준](../validation/quality-and-acceptance.md) |
| 양방향 전환 | v2 serving에서도 v1 shadow의 실행, 수집, 종료가 같은 관측 계약을 충족하는지 확인하십시오. | [단계별 gate](../rollout/stages-and-gates.md) |
| 부작용과 재생 | 실제 실행 효과, 인증 의미, 데이터 시점, 외부 의존성이 요청의 복제·재생 적격 조건을 충족하는지 확인하십시오. | [적격성](../shadow/eligibility-and-sampling.md) |

선정 담당자는 기존 도구의 mirror·통계 기능을 이용하는 구성과 비교까지 자체 담당하는 구성의 구현·운영 비용을 비교해야 한다. 운영 수준과 확장 기능의 채택은 미정이며, 실제 대상 API의 필요, 추가 부하, 복구 계약을 확인한 뒤 별도로 결정한다.
