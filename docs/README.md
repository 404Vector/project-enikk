# project-enikk 문서

이 프로젝트의 설계 결정, 원칙, 불변식, 구조에 대한 **단일 기준 문서(Single Source of Truth)** 는 이 `docs/` 폴더다.
코드 주석, 루트 `README.md`, `AGENTS.md` 에는 이 내용을 중복해서 적지 않고 이 폴더를 가리키기만 한다.

## 구성

| 경로 | 내용 | 상태 |
|---|---|---|
| [architecture/overview.md](architecture/overview.md) | 목표, 시스템 구성요소, 요청 흐름 | 작성 중 |
| [architecture/principles.md](architecture/principles.md) | 설계 원칙 (P-xx) | 초안 |
| [architecture/invariants.md](architecture/invariants.md) | 깨지면 안 되는 불변식 (I-xx) | 작성 중 |
| [architecture/code-structure.md](architecture/code-structure.md) | `src/` 파일 트리, 패키지 경계, 의존성 규칙 | 승인 |
| [adr/](adr/README.md) | 아키텍처 결정 기록 (ADR) | 진행 중 |
| [plan/milestones.md](plan/milestones.md) | 구현 순서와 각 단계의 완료 조건 | 승인 |

## 문서 작성 규칙

- **결정**은 ADR로 남긴다. 결정을 뒤집을 때는 기존 ADR 본문을 고치지 않고 새 ADR을 만든 뒤, 기존 ADR 상태를 `대체됨(ADR-XXXX)`으로 바꾼다.
- **불변식**은 `architecture/invariants.md`에 I-xx 번호로 적고, 근거 ADR을 연결한다.
- **원칙**은 `architecture/principles.md`에 P-xx 번호로 적는다.
- 외부 사실(가격, 정책, 제품 기능)을 근거로 쓸 때는 **확인 날짜와 출처 URL**을 함께 적는다.
- `제안` 상태의 ADR은 구현 근거로 쓰지 않는다.
