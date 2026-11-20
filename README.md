# project-enikk

Slack 으로 사용하는 개인용 AI 에이전트 플랫폼.
작업에 따라 Claude 또는 Gemini 가 일을 맡고, 여러 MCP 서버로 기능을 확장하며, 모든 실행을 Langfuse 로 추적합니다.
로컬 Kubernetes(Docker Desktop)에 Helm 으로 배포합니다.

> **상태: 설계 단계.** 아키텍처를 문서로 확정하는 중이며, 실행 가능한 기능은 아직 없습니다.

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
| 인터페이스 | Slack Bolt (Socket Mode) |
| 도구 연동 | MCP, agentgateway |
| 관측 | OpenTelemetry, Langfuse Cloud |
| 배포 | Docker, Kubernetes (Docker Desktop), Helm, GitHub Actions, GHCR, Argo CD |

## 저장소 구조

```
project-enikk/
├─ docs/        설계 문서 — 아키텍처, 원칙, 불변식, ADR
├─ deploy/      배포 자산 — Dockerfile, Helm 차트
├─ src/         Python 소스 (uv workspace 멤버들)
├─ enikk.toml   런타임 설정
└─ pyproject.toml
```

`src/` 내부 구조는 [docs/architecture/code-structure.md](docs/architecture/code-structure.md) 에서 확정 중입니다.

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

실행 방법은 구현이 진행되면 추가됩니다.

## 문서

- [문서 안내](docs/README.md)
- [시스템 개요](docs/architecture/overview.md)
- [설계 원칙](docs/architecture/principles.md)
- [불변식](docs/architecture/invariants.md)
- [아키텍처 결정 기록 (ADR)](docs/adr/README.md)

## 기여

코딩 에이전트용 작업 지침은 [AGENTS.md](AGENTS.md) 에 있습니다.
