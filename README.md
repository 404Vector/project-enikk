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
uv run enikk-ops secret set github.app-private-key --from-file ~/.config/enikk/github-app.pem  # 여러 줄 값은 파일에서
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
Slack 에서 enikk 자체(코드, 설정, 에이전트 정의, 문서)를 바꿔 달라고 요청하면 개발 에이전트가 격리된 환경에서 작업해 pull request 를 만들고, 소유자가 Slack 에서 승인한 뒤에야 반영됩니다.
승인 요청 DM 에는 PR 제목, 한두 줄 요약, 링크, [승인하고 머지] [반려] 버튼만 담깁니다. 저장소·브랜치 정보, PR 본문 전체, 바뀐 파일, 검사 결과는 그 메시지의 스레드 댓글로 나눠 올리고, diff 파일도 같은 스레드에 첨부합니다.

### 프롬프트 수정 방법

에이전트의 시스템 프롬프트(대화 방식, 말투)는 git 이 아니라 Langfuse 에만 있습니다([ADR-0006](docs/adr/0006-prompts-managed-in-langfuse.md)).
코드 배포 없이 Langfuse 화면에서 바로 고칩니다.

**어디를 고치나**

- Langfuse 호스트: `https://jp.cloud.langfuse.com` (Langfuse Cloud 일본 리전, 직접 호스팅 아님, [ADR-0001](docs/adr/0001-observability-langfuse-cloud.md), [ADR-0016](docs/adr/0016-langfuse-adapter.md))
  - 기본값: `ENIKK_RUNTIME_LANGFUSE_URL`, `ENIKK_DEV_LANGFUSE_URL` 설정의 기본값
  - 배포값: Helm values 의 `langfuseUrl` (`deploy/helm/runtime-claude/values.yaml`, `deploy/helm/dev-agent/values.yaml`). Argo CD Application 은 이 값을 덮어쓰지 않습니다.
- 프롬프트 (에이전트 정의 `agents/*.yaml` 의 `prompt`):

  | 에이전트 | 하는 일 | 프롬프트 이름 | label |
  |---|---|---|---|
  | `assistant` | Slack DM·멘션에 답하는 메인 대화 에이전트 (`config/slack.yaml`) | `assistant` | `production` |
  | `developer` | 코드 변경 요청을 받아 PR 을 만드는 개발 에이전트 | `developer` | `production` |

- 프로젝트 ID: **저장소에 없습니다.** 런타임은 API 키로 프로젝트를 정하므로 코드와 설정에 프로젝트 ID 를 적지 않습니다.
  Langfuse 에 로그인해 프로젝트를 열면 주소창의 `/project/<프로젝트 ID>/...` 부분, 또는 **Settings → General** 화면에서 확인합니다.
- 바로 가는 주소 (`<프로젝트 ID>` 를 위 값으로 바꿉니다):
  - `https://jp.cloud.langfuse.com/project/<프로젝트 ID>/prompts/assistant`
  - `https://jp.cloud.langfuse.com/project/<프로젝트 ID>/prompts/developer`
- API 키 이름: `langfuse.public-key`, `langfuse.secret-key` (core 가 런타임에 전달합니다. [비밀값](#비밀값) 참고).

**절차**

1. 위 주소로 들어가거나, 왼쪽 메뉴 **Prompts** 에서 고칠 프롬프트(`assistant` 등)를 엽니다.
2. 현재 `production` label 이 붙은 버전을 확인하고 **New version** 으로 새 버전을 만듭니다.
3. 형식을 지킵니다. 어기면 그 에이전트는 실행을 거부합니다([ADR-0016](docs/adr/0016-langfuse-adapter.md)).
   - 프롬프트 형식은 **text** 입니다 (chat 아님).
   - **Config** 에 `"model"` 키가 있어야 합니다. 이전 버전의 config 를 그대로 가져가세요. `model` 외의 키는 생성 파라미터로 엔진에 전달됩니다.
4. 새 버전에 **`production` label** 을 지정합니다. 저장할 때 바로 지정하거나, 나중에 버전 목록에서 label 을 옮깁니다.
   label 을 옮기지 않으면 새 버전을 만들어도 에이전트는 이전 버전을 계속 씁니다.
5. 되돌릴 때는 `production` label 을 이전 버전으로 옮깁니다.

**반영 시점**

- 재시작은 필요 없습니다. 런타임이 작업마다 Langfuse 에서 프롬프트를 가져옵니다.
- `assistant`: 런타임 프로세스가 프롬프트를 **60초** 캐시하므로, label 을 옮기고 최대 60초 뒤의 대화부터 새 버전이 쓰입니다.
- `developer`: 작업마다 새 Job 으로 실행되므로 다음 작업부터 새 버전이 쓰입니다.
- Langfuse 에 연결할 수 없을 때만 최대 1시간 된 캐시를 씁니다. 어떤 버전이 쓰였는지는 런타임 로그(`prompt <이름> v<버전>`)에서 확인할 수 있습니다.

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
