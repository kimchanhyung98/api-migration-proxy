# 제품

제품 목적·적용 범위·사용자 흐름·요구사항과 위험을 정의한다.

부작용 없는 조회 API부터 양쪽 실행 결과를 확인하고, 사용자 응답을 점진적으로 v2로 전환한다. 프록시는 검증과 전환을 위한 임시 구성으로, 종료 조건을 충족하면 제거한다.

```mermaid
flowchart LR
    A["v1 기준선 확보"] --> B["사용자 응답은 v1<br/>v2 shadow 검증"]
    B --> C["증거 확인·수동 승인"]
    C --> D["v2 응답 비율 확대"]
    D --> E["안정성·잔존 의존 확인"]
    E --> F["v2 직접 연결<br/>프록시 제거"]
```

## 기능별 문서

| 기능 | 상세 범위 |
| --- | --- |
| [제품 개요와 경계](overview.md) | 목적, 책임, 성공 지표와 전체 흐름 |
| [적용 범위와 사용자 흐름](scope-and-workflows.md) | 사용 시나리오, MVP, 제외 범위와 불변식 |
| [제품 요구와 설계 기준](decisions.md) | 현행 설계·재검토 조건·위험·대응 기준 |

## 공통 기준과 참고자료

| 문서 | 상세 범위 |
| --- | --- |
| [공통 용어](../_rules/glossary.md) | serving·shadow·cohort·event·stage의 의미 |
| [Elasticsearch 방식의 API 적용](../research/elasticsearch-reference.md) | 전환 원리·데이터 책임·외부 시스템의 적용 경계 |
| [리팩토링·마이그레이션 패턴](../research/refactoring-and-migration.md) | 점진적 교체·비노출 실행·임시 구성의 근거 |

[전체 도메인](../README.md)
