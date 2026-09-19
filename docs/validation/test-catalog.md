# 요구사항별 검증 시나리오

- 기능 계약·FR 수용 조건: 각 도메인의 담당 기능 문서에서 정의
- 구현 후 실행할 검증 시나리오와 요구사항 연결

## 테스트 시나리오

- 검증 대상: 공개 동작, 역할별 실행·관측과 실패 시 사용자 영향
- 내부 함수 호출 횟수만으로 기능 성공을 판정하지 않음

| ID | 시나리오 | 기대 결과 | 요구사항 |
| --- | --- | --- | --- |
| T-01 | 중복·충돌 route와 잘못된 backend 등록 | 활성화 전에 검증 실패, 기존 유효 설정 유지 | [FR-01](../routing/route-registration.md), [FR-06](../routing/configuration.md), [FR-29](../routing/configuration.md) |
| T-02 | v1 통과에서 정상·예상 오류 호출 | v1만 한 번 실행, 상태·필수 헤더·body 보존 | [FR-02](../routing/serving-and-cohorts.md), [FR-07](../routing/http-forwarding.md), [FR-17](../comparison/context-and-outcomes.md) |
| T-03 | 미등록 일반 HTTP와 미지원 프로토콜 | 문서화한 v1 통과·기존 진입점 정책 유지 | [FR-08](../routing/route-registration.md) |
| T-04 | shadow가 serving보다 빠르게 완료 | 빠른 결과로 바꾸지 않고 선택 응답만 전달 | [FR-03](../routing/serving-and-cohorts.md), [FR-12](../shadow/parallel-execution.md) |
| T-05 | v2 비율 0·1과 중간 비율 | 경계값 정확, 중간 배정은 정한 bucket 정책 만족 | [FR-04](../routing/serving-and-cohorts.md) |
| T-06 | 같은 키·그룹·salt, 인스턴스·재시작 변경 | 같은 bucket 재현, 증가 비율에서 기존 v2 cohort 유지 | [FR-05](../routing/serving-and-cohorts.md), NFR-10 |
| T-07 | 연관 route 그룹·정책 불일치·missing key | 같은 revision 내 배정 일치, 그룹별 전환 활성 상태·키 출처·salt·해시·비율 불일치 거부, 필요한 키 누락은 v1·사유 기록 | [FR-05](../routing/serving-and-cohorts.md), [FR-29](../routing/configuration.md) |
| T-08 | 요청 도중 설정 revision 교체 | 한 요청의 목적지·정책이 바뀌지 않음 | [FR-06](../routing/configuration.md) |
| T-09 | body 복제, 인코딩·중복 query, 헤더 | 두 backend에 동일 의미 전달, reader 소모·변조 없음 | [FR-07](../routing/http-forwarding.md), [FR-11](../shadow/parallel-execution.md) |
| T-10 | Set-Cookie·압축·redirect·HEAD·204·304·mirror authority 변경 | body·압축 헤더·가상 호스트·서명 계약 보존, 불필요한 follow·파싱 없음 | [FR-07](../routing/http-forwarding.md), [FR-18](../comparison/normalization.md) |
| T-11 | 부작용 미확인·비허용 route | shadow 미실행, eligibility 사유 계측 | [FR-09](../shadow/eligibility-and-sampling.md) |
| T-12 | shadow 비율 0·1, serving 역할 전환, 기존 gateway와 프록시의 복제 설정 | 0이면 serving만 실행; 1이고 자원·입력 제한·취소가 없는 적격 요청은 각 backend 한 번 실행, 진입 경로의 중복 mirror 없음 | [FR-10](../shadow/eligibility-and-sampling.md), [FR-15](../shadow/parallel-execution.md) |
| T-13 | serving 정상 종료 뒤 느린 shadow | 사용자 응답은 먼저 종료, shadow는 자신의 deadline 내 수집 | [FR-12](../shadow/parallel-execution.md), [FR-14](../shadow/lifecycle-and-limits.md) |
| T-14 | client 연결 종료·shadow 선행 timeout·정상 응답 뒤 ASGI disconnect | 실제 전송 중 취소와 정상 완료 뒤 disconnect 구분; 정상 완료 뒤 shadow 유지, shadow timeout 후 serving 지속; serving 종료 전 T·비교 이벤트 조기 확정 금지 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-14](../shadow/lifecycle-and-limits.md), [FR-28](../observability/coverage-and-analysis.md) |
| T-15 | serving 연결 실패·timeout·본문 중단 | 정한 오류·불완전 전송 처리, 자동 fallback 없음 | [FR-07](../routing/http-forwarding.md), [FR-15](../shadow/parallel-execution.md) |
| T-16 | shadow 동시 실행 상한 도달 | serving 지속, 선택 후 생략과 분모 보존 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-20](../comparison/context-and-outcomes.md) |
| T-17 | 길이 미상 입력·압축 입력·요청 복제 및 응답 캡처 상한 초과 | prefix·잔여 body의 손실·중복 없는 serving 전달; wire·해제 크기 한도 적용, shadow 생략·비교 불가 사유 보존 | [FR-11](../shadow/parallel-execution.md), [FR-13](../shadow/lifecycle-and-limits.md), [FR-20](../comparison/context-and-outcomes.md) |
| T-18 | 깊은 JSON·큰 숫자·중복 키·잘못된 JSON | 정밀도 보존과 명시적 제한·비교 불가 처리 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-18](../comparison/normalization.md), NFR-05 |
| T-19 | 객체 키·배열 순서·중복·null·누락·타입 변경 | 문서의 기본 비교 의미와 일치 | [FR-18](../comparison/normalization.md) |
| T-20 | 제외 필드·허용 오차·좁은 필드 매핑 | 명시된 경로만 적용, 공개 응답 원문은 바뀌지 않음 | [FR-19](../comparison/normalization.md) |
| T-21 | 성공·예상 거절·예상 밖 오류·unknown의 조합 | 같은 500·unknown의 matched 금지, 성공/거절 혼합 쌍은 different·mixed, backend class와 comparison_class 분리 | [FR-17](../comparison/context-and-outcomes.md), [FR-20](../comparison/context-and-outcomes.md) |
| T-22 | 데이터 시차·다른 테넌트·문맥 누락 | 정책에 맞는 비교 제한 또는 권한 불변식 실패 | [FR-17](../comparison/context-and-outcomes.md), [FR-20](../comparison/context-and-outcomes.md), NFR-07 |
| T-23 | 비교 큐 포화·작업 만료·CPU 적체 | 무한 메모리 증가 없음, 드롭과 비교 지연 계측 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-22](../collection/async-storage.md), NFR-05 |
| T-24 | 저장소 중단·복구·배치 일부 성공·ACK 유실·중복 재시도 | serving 지속, 같은 event ID의 중복 행·W 증가 방지, ACK 미확인과 저장 실패 확정 구분 | [FR-21](../collection/event-model.md), [FR-22](../collection/async-storage.md), [FR-28](../observability/coverage-and-analysis.md), NFR-04 |
| T-25 | 상세 비율 0·일부·상세 cap | 비교와 요약 유지, 제한된 상세만 저장 | [FR-23](../collection/detail-sampling.md) |
| T-26 | 토큰·cookie·민감 본문·예외·diff, 상세 비활성 상태의 자동 HTTP 계측 | 저장·로그·trace·조회와 실제 exporter 출력의 URL/query·헤더·예외에 금지 값 노출 없음 | [FR-24](../collection/detail-sampling.md), NFR-07 |
| T-27 | 보존 기한 경과와 삭제 실패 | 목적별 만료 정리, 실패·적체 관측 | [FR-24](../collection/detail-sampling.md), NFR-07, NFR-09 |
| T-28 | route·기간·result·revision별 이벤트 조회 | 권한·범위 제한 안에서 양쪽 결과 연계 가능 | [FR-25](../collection/query-and-retention.md) |
| T-29 | 실행·실패·미실행·비교·저장의 고정 구간, 느린 client·구간 경계 지연·계측 재시작 | backend 종료 시각과 사용자 전송 종료 시각 구분, 사용자 종료 결과 확보 후 최종 요약 제출; 요청 시작 집단·처리시각 counter와 E/S/D/T/C/M/X/W 일관성, 유실된 S를 저장 행만으로 복원 금지 | [FR-21](../collection/event-model.md), [FR-27](../observability/metrics.md), [FR-28](../observability/coverage-and-analysis.md) |
| T-30 | 비교 실행 구간의 분모 0·표본 부족·모니터링 누락, S5/S6의 계획된 비교 종료 | 실행 중 증거 부족은 unknown; S5에서 신규 S=0이어도 적격 요청 E 집계 유지; 계획된 종료는 적용 제외 사유·과거 비교 증거·현재 serving 검증 필요, 신규 비교 pass로 표시 금지 | [FR-28](../observability/coverage-and-analysis.md), [FR-30](../rollout/stages-and-gates.md) |
| T-31 | 인스턴스별 설정 적용 지연·동시 변경, 독립 설정 로딩 worker 사이의 부분 적용 | 신규 요청용 revision 편차·오래된 변경 충돌 감지, 확대 중지; 한 worker의 응답으로 인스턴스 전체 적용 완료 판정 금지 | [FR-29](../routing/configuration.md), NFR-06 |
| T-32 | v2 serving 중 shadow만 중지, v1 serving 복귀만 적용, 둘 다 중지 | 각 조작의 신규 요청 영향 구분; serving만 v1 복귀 시 v2 shadow 잔존 확인, 진행 중 요청은 별도 수명 정책 적용 | [FR-16](../rollout/rollback-and-bypass.md) |
| T-33 | v1 복귀 가능한 상태에서 serving 롤백 | 목표 시간 내 실제 분포 복귀, 진행 요청 중복 재실행 없음 | [FR-16](../rollout/rollback-and-bypass.md), [FR-30](../rollout/stages-and-gates.md), NFR-02 |
| T-34 | v1 데이터·스키마·capacity가 복귀 불가 | ready로 표시하지 않고 별도 복구 경로 선택 | [FR-30](../rollout/stages-and-gates.md) |
| T-35 | 프록시 장애와 직접 v1 경로 복귀 | 실제 client 요청 경로·복귀 시간 확인 | [FR-31](../rollout/rollback-and-bypass.md), NFR-02 |
| T-36 | serving 완료 뒤 느린 shadow·큐 잔존, LB 등록 해제·종료 신호·유예 만료·재시작 | HTTP 연결 배출과 내부 작업 배출 구분, 진행 응답의 조기 종료·유실 구간 보고, 메트릭 완전성 과장 없음 | [FR-14](../shadow/lifecycle-and-limits.md), [FR-22](../collection/async-storage.md), NFR-04 |
| T-37 | 대표·피크·큰 응답·cold start·전체 shadow 설정에서 실행 상한 발동 | 사용자 오류·지연·자원·비용 검증, 부하 생성기의 미발행 요청과 실제 도착률 보고; 설정 비율 1이어도 실제 실행량·완료율 부족이면 full-load 통과 금지 | [FR-30](../rollout/stages-and-gates.md), NFR-01, NFR-03, NFR-05, NFR-09 |
| T-38 | worker·replica 증가, shadow on/off와 공유 DB·cache·quota 부하 | 프로세스별 한도를 합산한 총 shadow 예산 확인; cache hit·양쪽 지연·공유 부하의 상호 간섭 기록, v1을 독립 대조군으로 단정 금지 | [FR-13](../shadow/lifecycle-and-limits.md), NFR-03, NFR-09 |
| T-39 | 미등록 v1 의존, v2 직접 연결, 종료 시 보존 기한 미도래 기록 | 잔존 의존과 직접 호출 계약 검증; 종료만을 이유로 보존 대상 삭제 금지, 기한별 정리 확인 | [FR-32](../rollout/retirement.md), [FR-24](../collection/detail-sampling.md), NFR-08 |
| T-40 | 두 번째 API에 다른 최소 비교 규칙 적용 | 필요가 있을 때만 확장, 기존 경로 계약 회귀 없음 | [FR-26](../comparison/normalization.md), NFR-08, NFR-10 |
| T-41 | 동일 config 아래 backend rolling 배포·비교 정책·필수 데이터 문맥 변경 | 실제 backend revision·확인 불가 상태 기록, 혼재 구간 분리와 기존 gate 증거 유효성 재평가 | [FR-21](../collection/event-model.md), [FR-28](../observability/coverage-and-analysis.md), [FR-30](../rollout/stages-and-gates.md), NFR-10 |
| T-42 | read timeout 이내의 느린 chunk 반복·pool 대기·취소 후 연결 재사용 | 총 deadline에 따른 종료, pool 대기 포함 시간 예산 준수, streaming 연결·task 누적 없음, 역할별 종료 이유 보존 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-14](../shadow/lifecycle-and-limits.md), NFR-05 |
| T-43 | 공유 HTTP client에서 사용자 A·B·익명 요청을 순차·동시 실행, 양쪽 Set-Cookie 반환과 호출자의 후속 Cookie 전송 | cookie·인증·tenant 문맥의 요청 간 혼입 없음, shadow 쿠키의 후속 serving 유입 없음; serving의 Set-Cookie와 호출자가 후속 요청에 보낸 허용 Cookie 보존 | [FR-07](../routing/http-forwarding.md), [FR-11](../shadow/parallel-execution.md), NFR-07 |
| T-44 | 허용 최대 입력의 동시 파싱·비교·마스킹, timeout·취소 뒤 CPU 작업 잔존 | serving·event loop 지연과 실제 작업·메모리 상한 검증; await 종료만으로 실제 작업 슬롯·캡처 해제 처리 금지 | [FR-13](../shadow/lifecycle-and-limits.md), [FR-22](../collection/async-storage.md), NFR-01, NFR-05 |
| T-45 | 다중 worker 요청 분산·계측 수집, worker 종료·재시작 | 수집 가능한 worker counter의 집계 누락·중복 방지, 종료 worker의 stale gauge 제거, 깊이·inflight·최고 대기 시간의 집계 의미 보존; 종료·재시작 경계의 미수집 counter·유실 가능성 별도 표시 | [FR-27](../observability/metrics.md), [FR-28](../observability/coverage-and-analysis.md), NFR-04 |
