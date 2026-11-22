# project-enikk

Slack 으로 사용하는 개인용 AI 에이전트 플랫폼.
작업에 따라 Claude 또는 Gemini 가 일을 맡고, 여러 MCP 서버로 기능을 확장하며, 모든 실행을 Langfuse 로 추적합니다.
로컬 Kubernetes(Docker Desktop)에 Helm 으로 배포합니다.

> **상태: 개발 중.** Slack 에서 Claude 에이전트와 대화하는 기본 경로가 동작합니다. MCP 도구, Gemini, 평가·자기 개선은 계획 단계입니다.

## 주요 기능 (계획)

- **Slack 인터페이스** — Socket Mode 로 연결되어 공개 엔드포인트 없이 동작
- **멀티 모델** — 에이전트 정의마다 Claude(Claude Agent SDK) / Gemini(Google ADK) 선택
- **MCP 확장** — agentgateway 로 여러 MCP 서버를 하나의 엔드포인트로 통합
- **LLMOps** — Langfuse 기반 트레이싱, 프롬프트 관리, 평가, 피드백 수집
- **운영 CLI** — 이미지 빌드, Helm 배포, 에이전트 접속을 하나의 Python CLI 로

## 아키텍처

```
Slack ─► Slack 커넥터 ─► enikk-core (제어) ─┬─► Claude 런타임 ─► Anthropic API
                                           └─► Gemini 런타임 ─► Google API
                              런타임들 ─► agentgateway ─► MCP 서버들
                              전 구간 ─► Langfuse (OpenTelemetry)
배포: GitHub Actions ─► GHCR ─► Argo CD ─► Kubernetes
```

자세한 내용은 [docs/architecture/overview.md](docs/architecture/overview.md) 를 참고하세요.

## 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 언어 / 패키지 관리 | Python, uv (workspace 모노레포) |
| 에이전트 | Claude Agent SDK, Google ADK |
| 인터페이스 | Slack (slack-sdk, Socket Mode) |
| 도구 연동 | MCP, agentgateway |
| 관측 | OpenTelemetry, Langfuse Cloud |
| 배포 | Docker, Kubernetes (Docker Desktop), Helm, GitHub Actions, GHCR, Argo CD |

## 저장소 구조

```
project-enikk/
├─ agents/      에이전트 정의 (YAML, 프롬프트 본문 없음)
├─ config/      인터페이스 연결 규칙 (Slack 소유자, 에이전트 매핑)
├─ docs/        설계 문서 — 아키텍처, 원칙, 불변식, ADR
├─ deploy/      배포 자산 — Dockerfile, Helm 차트, 매니페스트, Argo CD Application
├─ src/         Python 소스 (uv workspace 멤버들)
└─ pyproject.toml
```

`src/` 내부 구조는 [docs/architecture/code-structure.md](docs/architecture/code-structure.md) 를 참고하세요.

## 시작하기

### 요구 사항

- WSL2 (Ubuntu) 또는 Linux
- Docker Desktop (Kubernetes 활성화)
- [uv](https://docs.astral.sh/uv/), [Helm](https://helm.sh/) 3, kubectl

### 설치

```bash
git clone https://github.com/404Vector/project-enikk.git
cd project-enikk
uv sync --all-packages
```

### 클러스터 부트스트랩

```bash
uv run enikk-ops argocd bootstrap   # Argo CD 설치 후 deploy/argocd/ 를 따르도록 등록
uv run enikk-ops argocd status      # Application 동기화·상태 확인
uv run enikk-ops argocd ui          # Argo CD 화면 (http://localhost:8080)
```

이후에는 `main` 에 머지된 변경이 Argo CD 를 통해 자동으로 배포됩니다.

### 비밀값

비밀값(Slack, Claude, Langfuse 토큰)은 이 PC 의 `~/.config/enikk/secrets.yaml` 에 보관하고,
클러스터에는 core 만 읽는 Secret 으로 반영합니다. 다른 서비스는 core 에서 필요한 값만 받습니다.

```bash
uv run enikk-ops secret set slack.bot-token   # 숨김 입력으로 저장
uv run enikk-ops secret list                  # 설정 여부만 표시 (값은 출력하지 않음)
uv run enikk-ops secret unset slack.bot-token
uv run enikk-ops secret sync                  # 파일 내용을 클러스터에 반영
```

파일은 평문입니다. 권한(0600)을 유지하고, 백업 매체도 같은 수준으로 보관하세요.
클러스터를 초기화했다면 `secret sync` 를 다시 실행하면 됩니다.

### Slack 앱

1. [api.slack.com/apps](https://api.slack.com/apps) 에서 **From a manifest** 로 앱을 만들고 [`deploy/slack/app-manifest.yaml`](deploy/slack/app-manifest.yaml) 을 붙여 넣습니다.
2. 워크스페이스에 설치하고 두 토큰을 등록합니다.
   - 봇 토큰(`xoxb-`, OAuth & Permissions): `uv run enikk-ops secret set slack.bot-token`
   - 앱 토큰(`xapp-`, Basic Information → App-Level Tokens, scope `connections:write`): `uv run enikk-ops secret set slack.app-token`
3. `uv run enikk-ops secret sync` 후 커넥터가 시작되면, 봇에게 DM 을 보내거나 채널에서 멘션합니다.

응답하는 사용자와 에이전트는 [`config/slack.yaml`](config/slack.yaml) 에서 정합니다. 그 외 사용자의 메시지는 무시합니다.
진행 상태는 원본 메시지의 반응으로 표시합니다: 👀 접수 → ✅ 답변 / ❌ 실패.

### 배포 확인

```bash
uv run enikk-ops smoke core         # 실제 ServiceAccount 토큰으로 core 의 전체 흐름과 인증을 점검
uv run enikk-ops smoke claude       # Claude 런타임과 두 메시지 대화 (실제 모델 호출, 구독 사용량 소모)
```

## 문서

- [문서 안내](docs/README.md)
- [시스템 개요](docs/architecture/overview.md)
- [설계 원칙](docs/architecture/principles.md)
- [불변식](docs/architecture/invariants.md)
- [아키텍처 결정 기록 (ADR)](docs/adr/README.md)

## 기여

코딩 에이전트용 작업 지침은 [AGENTS.md](AGENTS.md) 에 있습니다.
