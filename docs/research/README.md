# 기술 자료

이 문서 모음은 마이그레이션 사례와 설계 패턴을 설명하고, 도구·서비스가 제공하는 기능을 Proxy의 요구사항과 비교한다. 각 상세 문서는 외부 자료가 설명하는 기능, 이 프로젝트에 적용한 설계 판단, 서비스별로 결정해야 할 채택 조건을 구분한다.

외부 도구가 기능을 제공한다는 사실만으로 이 프로젝트가 그 도구를 채택했거나 해당 연동을 구현했다고 판단해서는 안 된다. 현재 코드에 적용한 구조는 [FastAPI·도메인 구조](fastapi-and-domain-structure.md)에서 설명한다. Python/FastAPI는 확정된 구현 스택이며, 운영 인프라·운영 저장소·관측 플랫폼의 선택은 미정이다.

serving, shadow, route, cohort의 의미는 [공통 용어](../_rules/glossary.md)를 따른다. 자료를 기술 선택에 사용할 때에는 [기술 평가 기준](../_rules/technology-evaluation.md)에 따라 선택한 제품과 버전의 지원 범위 및 실제 통합 결과를 구분해야 한다.

```mermaid
flowchart LR
    A["마이그레이션 사례·패턴"] --> C["요구사항과 적용 조건 비교"]
    B["도구·서비스의 기능·제약"] --> C
    C --> D["검증할 가정·별도 구현 책임 식별"]
    D --> E["기술 선택·설계 판단의 근거"]
```

| 문서 | 요약 |
| --- | --- |
| [Elasticsearch 방식의 API 적용](elasticsearch-reference.md) | 데이터 준비, 양쪽 실행, 관측, 전환, 종료에서 가져온 원리와 일반 API에 적용할 수 없는 전용 계약을 구분한다. |
| [리팩토링·마이그레이션 패턴](refactoring-and-migration.md) | 점진적 교체와 비노출 실행의 목적을 설명하고, 임시 프록시의 책임 및 제거 조건을 정리한다. |
| [프록시·검증 도구](proxy-patterns-and-tools.md) | 요청 복제·응답 비교·재생 도구가 제공하는 기능과 제품이 별도로 연결해야 할 책임을 구분한다. |
| [서비스·런타임 후보](service-options-and-constraints.md) | AWS 서비스, Python HTTP 구성 요소, PostgreSQL의 기능과 프록시 적용 시 확인할 제약을 정리한다. |
| [내부 관측과 저장소·외부 서비스](observability-and-storage.md) | 내부 수집·집계·저장을 기본으로 설명하고, SQLite·PostgreSQL의 선택 조건과 외부 관측 서비스의 역할·데이터 전송 경계를 비교한다. |
| [FastAPI·도메인 구조](fastapi-and-domain-structure.md) | 패키지 배치와 도메인 중심 설계의 개념을 설명하고, 현재 코드의 책임 분리 및 유지한 결합을 명시한다. |

[전체 문서](../README.md)
