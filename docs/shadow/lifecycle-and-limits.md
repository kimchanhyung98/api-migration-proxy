# 실행 수명·취소·자원 제한

- 목적: shadow의 최대 수명·자원 제한 및 정상 응답·client 취소·프로세스 종료 구분

## 기능 계약

| 항목 | 정의 |
| --- | --- |
| 시작 조건·입력 | 역할별 timeout·동시 실행 한도, body cap, client·프로세스 종료 신호 |
| 결과·출력 | 완료·timeout·취소·미실행 결과와 자원 회수 상태 |
| 실패·제한 | 무한 task·대기열 금지; 취소 시도와 backend 효과 취소·정확한 유실 개수 보장 구분 |

## timeout·취소·실패

| 상황 | serving | shadow·수집 |
| --- | --- | --- |
| serving 정상 완료 | 선택 응답 전달 | 이미 시작한 shadow는 자신의 deadline까지 유지 |
| shadow timeout·전송 오류 | 기존 serving 계속 | 실패 결과 기록, 재시도 없음; serving 종료 전 양쪽 종료 확정 금지 |
| serving timeout | headers 전이라면 정의한 gateway timeout 응답 | 이미 시작한 shadow는 제한 시간 안에서 수집 가능; 사용자 응답에 사용하지 않음 |
| serving 연결 실패 | headers 전이라면 정의한 gateway 오류 응답 | 반대편 fallback 없음 |
| client 연결 종료 | upstream 취소 시도 | 실행 중 shadow도 취소 시도, 취소 이유 기록 |
| shadow 슬롯 부족 | serving 계속 | 대기열에 무한 적재하지 않고 실행 생략 |
| 캡처 상한 초과 | 응답 전송 계속 | 부분 바이트의 완전 JSON 취급 금지 |
| 비교 큐·저장소 장애 | serving 계속 | 감지된 드롭 증가, 단계 승격 중지 |
| 프로세스 정상 종료 | 신규 요청 수락 중단, 제한 시간 내 완료 | 잔여 작업 배출 후 timeout 또는 유실 기록 |

- 시간 구분: 역할별 connection·응답·전체 deadline
- HTTP 클라이언트의 connect·read·write·pool timeout과 총 실행 deadline 구분; chunk마다 갱신되는 read timeout만으로 총 수명 제한 금지
- 대기·정리 경계: pool 대기도 시간 예산에 포함, 완료·취소·예외 시 streaming 응답과 연결 자원 해제
- 사용자 응답 전송: backend 수신과 별도의 완료·실패 관리, 느린 client에 대한 전송 시간 예산 적용; 최종 이벤트 대기로 캡처·상태를 무기한 보유하지 않음
- HTTP 클라이언트 설정: [HTTPX·실행 환경 제약](../research/service-options-and-constraints.md) 확인
- 초기 운영안: 역할별 총 시간 예산과 검증된 HTTP 클라이언트 기본 설정 사용
- 최대 수명: shadow·비교 작업의 큐 대기 시간 포함
- 정상 serving 완료: client 연결 종료와 구분; 응답 완료 처리에 따른 shadow의 일괄 취소 금지
- ASGI `http.disconnect`: 정상 응답 완료 뒤에도 발생 가능하므로 해당 신호만으로 client 취소 판정 금지; 응답 완료 상태·전송 중 오류와 함께 해석. [ASGI HTTP 규격](https://asgi.readthedocs.io/en/latest/specs/www.html#disconnect-receive-event)
- 사용자 전송 완료의 관측 경계: 서버가 확인한 전송 종료; ASGI `send()` 완료를 최종 client의 전체 수신·처리 성공으로 해석하지 않음. 서버·프레임워크 조합의 실제 연결 종료 동작 검증 필요
- client 취소: backend 실행 중단의 보장 없음; 취소된 요청도 종료 사유·분모 유지
- 업무 효과: 취소에 따른 원상 복구 가정 금지
- 초기 shadow 범위: 부작용 없는 API로 제한

## 자원 예산

| 예산 | 제한할 대상 | 초과 시 제안 |
| --- | --- | --- |
| serving 동시 실행 | 사용자 요청과 연결 | 기존 서비스 SLO에 맞는 admission·오류 처리 |
| shadow 동시 실행 | 반대편 요청·연결 | 신규 shadow 생략과 사유 계측 |
| 요청 복제 bytes | 입력 body·복사본, 전달 바이트 및 해제 수행 시 해제 바이트 | shadow 생략, 검증된 serving 경로 유지 |
| 응답 캡처 bytes | 비교용 body, 전달 바이트 및 해제 수행 시 해제 바이트 | 캡처 중지, `not_comparable` |
| JSON 복잡도·CPU | 입력량·파싱 깊이·노드 수·비교 작업 시간 | 작업 진입 제한·지원되는 중단 방식 적용과 원인 기록 |
| 큐 개수·bytes·age | 비교 대기·저장 대기 | 유한한 드롭·만료와 감지 메트릭 |
| batch 크기·쓰기 시간 | 저장소 작업 | 제한된 재시도 후 드롭 계측 |
| 상세 보존량 | 표본 데이터·만료 작업 | 표본 축소 또는 상세 수집 중지 |

- 메모리 추정: 진행 중 캡처·queue entry·parser·HTTP 클라이언트 overhead 합산
- 본문 cap의 한계: 전체 메모리 상한과 구분
- 양쪽 응답 보관 큐: entry 수·총 bytes 동시 제한
- 압축 해제: 압축된 body 크기만으로 메모리 상한 판단 금지
- CPU 작업: 파싱·비교 전 입력량과 지원되는 복잡도 한도 적용; 비동기 timeout만으로 동기 연산의 선점 중단 보장 금지
- 취소 후 자원: 실행 중 thread의 강제 중단은 보장되지 않으므로 실제 작업 종료 전 슬롯 반환·캡처 메모리 해제 완료로 집계 금지; 남은 작업도 실행·메모리 예산에 포함. [AnyIO thread 취소 경계](https://anyio.readthedocs.io/en/stable/threads.html#reacting-to-cancellation-in-worker-threads)
- 역할별 자원: shadow가 serving의 연결·실행 슬롯을 모두 점유하지 않도록 한도 분리 또는 serving 여유 보장
- 자원 격리 구현: 별도 connection pool 필수화 없이 선택한 공유 방식의 최대 영향 검증
- 전체 실행 한도: 인스턴스별 L·인스턴스 수 N일 때 최대 N×L까지 증가 가능
- 자동 확장: 전체 shadow 예산 증가 효과를 운영 모델에 반영
- 분산 limiter 도입 전: 인스턴스별 예산·최대 replica 수만으로 관리 가능한지 검토

## 요구사항과 수용 조건

| ID | 우선순위 | 요구사항 | 수용 조건 |
| --- | --- | --- | --- |
| FR-13 | P0 | timeout·동시 실행·메모리·큐 상한 적용 | 한도 초과가 무한 대기·무한 task 생성으로 이어지지 않음 |
| FR-14 | P0 | serving 완료·호출자 취소·프로세스 종료를 구분 | 정상 응답 뒤 shadow 유지, client 취소 시 취소 시도, 제한된 종료 배출 |

## 검증 계획

- 구현 후 검증: [T-14, T-16, T-17, T-18, T-23, T-36, T-38, T-42, T-44](../validation/test-catalog.md)

## 관련 기능

- [비동기 수집 큐](../collection/async-storage.md)
- [용량과 비용](../infrastructure/capacity-and-cost.md)
