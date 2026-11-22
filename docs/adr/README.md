# 아키텍처 결정 기록 (ADR)

## 목록

| 번호 | 제목 | 상태 | 날짜 |
|---|---|---|---|
| [0001](0001-observability-langfuse-cloud.md) | LLM 관측 도구로 Langfuse Cloud(Hobby) 사용 | 승인 | 2026-11-21 |
| [0002](0002-claude-auth-subscription-token.md) | Claude 인증에 본인 구독 토큰 사용 (개인 전용) | 승인 | 2026-11-21 |
| [0003](0003-multi-model-engine.md) | 에이전트 정의 단위로 Claude / Gemini 엔진을 선택하는 멀티 모델 구조 | 승인 | 2026-11-21 |
| [0004](0004-agentgateway-mcp-only.md) | agentgateway 를 MCP 통로에만 배치 | 승인 | 2026-11-21 |
| [0005](0005-agent-definition-format.md) | 에이전트 정의 공통 형식 | 승인 | 2026-11-21 |
| [0006](0006-prompts-managed-in-langfuse.md) | 프롬프트의 원본은 Langfuse 이며 git 에 두지 않는다 | 승인 | 2026-11-21 |
| [0007](0007-self-improvement-workflow.md) | 에이전트 자기 개선 구조 (제안 → 승인 → 자동 반영) | 승인 | 2026-11-21 |
| [0008](0008-cicd-github-ghcr-gitops.md) | CI/CD — GitHub Actions 빌드, GHCR, GitOps 배포 | 승인 | 2026-11-21 |
| [0009](0009-split-slack-bot-and-agent-runtime.md) | Slack 봇과 에이전트 런타임을 별도 프로세스로 분리 | 대체됨(ADR-0010) | 2026-11-21 |
| [0010](0010-enikk-core-control-plane.md) | enikk-core 를 제어 경로 전용 중앙 서비스로 두고 엔진별 런타임을 분리 | 승인 | 2026-11-21 |
| [0011](0011-core-persistence-and-api.md) | enikk-core 의 저장소·작업 큐·API·데이터 모델링 | 승인 | 2026-11-21 |
| [0012](0012-core-skeleton-implementation.md) | enikk-core 골격 구현 세부 결정 | 승인 | 2026-11-21 |
| [0013](0013-credentials-local-file-core-broker.md) | 비밀값은 소유자 PC 의 파일이 원본이고, core 가 서비스에 나눠 준다 | 승인 | 2026-11-22 |
| [0014](0014-engine-transcripts-in-core.md) | 엔진 대화 이력(트랜스크립트)은 core 가 저장한다 | 승인 | 2026-11-22 |
| [0015](0015-runtime-worker.md) | 런타임 공통 작업 처리 흐름 | 승인 | 2026-11-22 |
| [0016](0016-langfuse-adapter.md) | Langfuse 어댑터 — 프롬프트 조회와 트레이스 전송 설정 | 승인 | 2026-11-22 |
| [0017](0017-claude-engine-runtime.md) | Claude 엔진 런타임 — 도구 차단, 대화 이어 가기, 트레이스 | 승인 | 2026-11-22 |
| [0018](0018-slack-connector.md) | Slack 커넥터 — 연결 규칙, 중복 처리, 진행 표시 | 승인 | 2026-11-22 |
| [0019](0019-github-app-scoped-tokens.md) | GitHub App 신원과 용도별로 좁힌 설치 토큰 | 승인 | 2026-11-22 |
| [0020](0020-slack-approval-and-merge.md) | Slack 승인, 상태 검사 기반 머지, 배포 실패 대응 | 승인 (알림 위치는 ADR-0026 으로 부분 대체) | 2026-11-22 |
| [0021](0021-dev-agent-job-per-task.md) | 개발 에이전트는 작업마다 새 Pod(Job)로 실행한다 | 승인 | 2026-11-22 |
| [0022](0022-delegation-to-developer.md) | assistant 가 개발 에이전트에 작업을 위임한다 | 승인 | 2026-11-22 |
| [0023](0023-deployment-notices-and-revert.md) | 배포 결과 알림과 되돌리기 | 승인 (알림 위치는 ADR-0026 으로 부분 대체) | 2026-11-22 |
| [0024](0024-sensitive-paths-instead-of-protected.md) | 보호 경로를 막지 않고, 민감 경로로 표시해 강화된 승인을 받는다 | 승인 | 2026-11-22 |
| [0025](0025-thread-follow-up-on-agent-prs.md) | 위임한 스레드에서 PR 을 끝까지 따라가고, 같은 PR 을 이어서 고친다 | 승인 | 2026-11-22 |
| [0026](0026-slack-notice-destinations.md) | Slack 알림을 어디에 올리는가 — 배포 알림은 #release, 개발 요청은 상태와 상세를 나눈다 | 승인 | 2026-11-22 |
| [0027](0027-thread-context-for-the-thread-agent.md) | 스레드의 에이전트는 그 스레드에 올라온 다른 글도 본다 | 승인 | 2026-11-22 |
| [0028](0028-shared-memory.md) | 스레드를 넘어 쌓이고 공유되는 기억(메모리) | 제안 | 2026-11-22 |

## 상태 정의

- **제안**: 논의 중. 구현 근거로 쓰지 않는다.
- **승인**: 확정. 구현과 다른 문서는 이 결정을 따른다.
- **대체됨(ADR-XXXX)**: 더 새로운 결정으로 바뀌었다. 기록 보존을 위해 삭제하지 않는다.
- **폐기**: 검토했으나 채택하지 않았다.

## 새 ADR 작성

[template.md](template.md)를 복사해 다음 번호로 만든다. 파일명은 `NNNN-짧은-영문-제목.md`.
