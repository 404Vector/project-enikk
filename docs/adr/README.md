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

## 상태 정의

- **제안**: 논의 중. 구현 근거로 쓰지 않는다.
- **승인**: 확정. 구현과 다른 문서는 이 결정을 따른다.
- **대체됨(ADR-XXXX)**: 더 새로운 결정으로 바뀌었다. 기록 보존을 위해 삭제하지 않는다.
- **폐기**: 검토했으나 채택하지 않았다.

## 새 ADR 작성

[template.md](template.md)를 복사해 다음 번호로 만든다. 파일명은 `NNNN-짧은-영문-제목.md`.
