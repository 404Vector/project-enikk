# 코드 구조

- 상태: 승인
- 최종 갱신: 2026-11-21
- 근거: ADR-0003, ADR-0005, ADR-0006, ADR-0007, ADR-0010, ADR-0011, P-01, P-02, P-13, P-14

## 1. 전제

- Python, uv workspace 모노레포. `src/` 아래 각 패키지는 독립된 workspace 멤버(자체 `pyproject.toml`, 자체 `tests/`).
- **앱 = 배포 단위(컨테이너 이미지 1개)**. 라이브러리 = 앱이 조립해 쓰는 부품.
- 기성 제품(agentgateway, Argo CD, CloudNativePG, Langfuse, 외부 MCP)은 `src/` 가 아닌 `deploy/` 에서 설정으로만 다룬다.
- 패키지 이름: 디렉토리 `src/<종류>/<이름>/`, 배포 이름 `enikk-<이름>`, import 이름 `enikk_<이름>`.

## 2. 저장소 최상위

```
project-enikk/
├─ agents/                 에이전트 정의 YAML (ADR-0005). 프롬프트 본문 없음 (I-13)
├─ config/                 인터페이스 연결 규칙 등 구성 (예: Slack → 에이전트 매핑, I-15)
├─ deploy/
│  ├─ docker/              앱별 Dockerfile
│  ├─ helm/                앱별 Helm 차트 (또는 공용 차트 + 앱별 values)
│  └─ argocd/              Argo CD Application 정의 (app-of-apps)
├─ docs/
├─ src/
├─ AGENTS.md, README.md, pyproject.toml (workspace 루트)
```

## 3. `src/` 트리

```
src/
├─ apps/                               ── 배포 단위. 얇게 유지 (입력 → 유스케이스 → 출력)
│  ├─ core/            enikk_core           enikk-core: API 서버(FastAPI), 유스케이스, 조립 지점
│  ├─ connector-slack/ enikk_connector_slack Slack Socket Mode 커넥터
│  ├─ runtime-claude/  enikk_runtime_claude 런타임 프레임워크 + Claude 엔진 조립
│  ├─ runtime-gemini/  enikk_runtime_gemini 런타임 프레임워크 + Gemini 엔진 조립
│  ├─ ops-cli/         enikk_ops            부트스트랩, 로컬 개발 도구
│  └─ mcp-<name>/      enikk_mcp_<name>     자체 MCP 서버 (필요해질 때 추가)
│
└─ libs/
   ├─ kernel/          enikk_kernel         기반: 에러 계층, 로깅, 설정 베이스(pydantic-settings),
   │                                          시계·ID 생성 포트, 프로세스 실행
   ├─ contracts/       enikk_contracts      공용 계약(pydantic): API 요청·응답, 작업·이벤트 메시지,
   │                                          에이전트 정의 스키마. 로직 없음
   ├─ core-client/     enikk_core_client    타입 있는 core API 클라이언트 + SA 토큰 첨부 (I-27, I-28)
   ├─ core-domain/     enikk_core_domain    core 도메인: Task·Session·Approval 상태 기계,
   │                                          포트: 레포지토리, UnitOfWork, JobQueue, AgentCatalog
   ├─ runtime/         enikk_runtime        런타임 프레임워크: 작업 가져오기 루프, 임대 연장,
   │                                          ModelEngine 포트, PromptStore 포트, 결과 보고
   └─ adapters/                          ── 포트 구현 (외부와 닿는 곳). 서로 import 하지 않음
      ├─ persistence-sql/ enikk_persistence_sql  SQLAlchemy 레포지토리·UoW·Postgres 큐, Alembic
      ├─ k8s-auth/        enikk_k8s_auth         TokenReview 검증기 (core 용)
      ├─ engine-claude/   enikk_engine_claude    ModelEngine 구현: Claude Agent SDK
      ├─ engine-gemini/   enikk_engine_gemini    ModelEngine 구현: Google ADK
      ├─ langfuse/        enikk_langfuse         PromptStore·FeedbackSink 구현, OTel 내보내기 설정
      └─ platform/        enikk_platform         Docker·Helm·kubectl (ops-cli 용)
```

## 4. 의존성 규칙

```
                apps/*
                  │
        ┌─────────┼──────────────┐
        ▼         ▼              ▼
   adapters/*   core-client    (apps 는 필요한 것만 조립)
        │         │
        ▼         ▼
   core-domain  runtime
        │         │
        └────┬────┘
             ▼
         contracts
             │
             ▼
          kernel
```

| 패키지 | import 할 수 있는 것 | import 하면 안 되는 것 |
|---|---|---|
| kernel | (외부 라이브러리만) | 프로젝트 내 모든 패키지 |
| contracts | kernel, pydantic | 그 외 전부 |
| core-domain, runtime | contracts, kernel | adapters, apps, 외부 SDK(SQLAlchemy, Anthropic, Google, Langfuse, Slack) |
| core-client | contracts, kernel, httpx | core-domain, adapters, apps |
| adapters/* | 자신이 구현하는 포트의 패키지, contracts, kernel, 해당 외부 SDK | 다른 adapters, apps |
| apps/* | 위 전부 (조립 목적) | 다른 apps |

- 이 규칙은 import-linter 계약으로 기계적으로 강제한다 (P-01).
- 외부 SDK 의 import 위치도 제한한다: SQLAlchemy 는 `persistence-sql`, Anthropic·Claude SDK 는 `engine-claude`, Google ADK 는 `engine-gemini`, Langfuse 는 `langfuse` 에서만 (I-06, I-08, I-26).

## 5. 설계 판단 메모

- **core 도메인을 라이브러리로 둔 이유**: 영속성 어댑터(`persistence-sql`)가 레포지토리 포트를 구현하려면 포트를 import 해야 한다. 라이브러리는 앱을 import 할 수 없으므로 포트와 도메인은 앱 밖(`core-domain`)에 있어야 한다. 유스케이스와 API 는 core 앱 안에 둔다.
- **contracts 는 공유 커널**: core 와 커넥터·런타임이 함께 쓰는 유일한 타입 집합이다. 바뀌면 양쪽이 영향을 받으므로 변경은 하위 호환을 원칙으로 한다.
- **엔진별 런타임 앱이 얇은 이유**: 작업 처리 흐름은 `runtime` 프레임워크가 공통으로 갖고, 앱은 엔진 어댑터를 끼워 넣기만 한다. 새 엔진 = `adapters/engine-<x>` + `apps/runtime-<x>`.
- **개발 에이전트**: 별도 앱을 만들지 않고 `runtime-claude` 를 다른 배포 프로필(전용 이미지에 git·uv 등 도구 포함, 전용 ServiceAccount, 격리 Pod)로 실행한다. 차이는 에이전트 정의(ADR-0005)의 허용 도구와 배포 설정뿐이다.
- **Slack 전용 라이브러리를 두지 않은 이유**: Slack 을 쓰는 곳이 커넥터 하나뿐이다. 두 번째 사용처가 생기면 그때 분리한다.

## 6. 기존 작성분 처리

| 기존 | 처리 |
|---|---|
| `src/libs/core` (enikk_core) | `libs/kernel` 로 이동·이름 변경. dataclass 값 객체를 pydantic 으로 전환 |
| `src/libs/platform` (enikk_platform) | `libs/adapters/platform` 으로 이동. ops-cli 전용 |
| `deploy/helm/enikk-agent`, `deploy/docker/claude-agent` | 초기 실험용(Claude Code 상주 Pod). **삭제 완료 (2026-11-21)** |
| 루트 `enikk.toml` | ops-cli 설정으로 축소하거나 앱별 pydantic-settings 로 대체 |
| 클러스터의 `enikk` 릴리스, PVC, 네임스페이스, 로컬 이미지 | **삭제 완료 (2026-11-21)** |
