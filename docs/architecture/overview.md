# 시스템 개요

- 상태: 작성 중
- 최종 갱신: 2026-11-23

## 목표

소유자 1인이 Slack 으로 사용하는 개인용 AI 에이전트 에닉과 그 플랫폼.
에이전트는 에닉 하나이고, `agents/*.yaml` 하나하나는 에닉의 역할(예: assistant, summarizer)이다(ADR-0031).

- Slack(Socket Mode)으로 대화하고 작업을 맡긴다.
- 역할 정의 파일에 적힌 엔진(Claude 또는 Gemini)이 작업을 수행한다 (ADR-0003, ADR-0005).
- 여러 MCP 서버를 연결해 기능을 확장한다 (ADR-0004).
- 모든 실행을 Langfuse 로 추적·평가하고, 프롬프트는 Langfuse 에서 버전 관리한다 (ADR-0001, ADR-0006).
- Slack 대화로 에닉 자신의 프롬프트·정의·코드 개선을 요청하고, 승인하면 자동 반영된다 (ADR-0007).
- 로컬 Kubernetes(Docker Desktop)에 GitOps(Argo CD)로 배포한다 (ADR-0008).

## 비목표

- 다중 사용자 / 팀 서비스 (ADR-0002 의 전제가 깨짐).
- 외부 공개 엔드포인트 (Socket Mode 와 GitOps 풀 방식이므로 인바운드 접근 불필요).

## 구성요소

### 자체 개발

| 구성요소 | 역할 | 근거 |
|---|---|---|
| enikk-core | 제어 경로 중앙 서비스: 작업 상태, 스레드-세션 연결, 카탈로그, 승인 흐름, 스레드 세션의 PR 제출(ADR-0037), 프롬프트 후보 생성과 승격(Langfuse 에 쓰는 유일한 곳) | ADR-0010, ADR-0032 |
| Slack 커넥터 | Socket Mode 수신, 소유자 확인, core 에 이벤트 전달, 결과 게시 | ADR-0010, I-01 |
| Claude 런타임 | Claude Agent SDK 로 작업 실행 (구독 토큰). 역할의 기본 도구(명령·파일·웹)는 Landlock 샌드박스 안에서 비밀값 없이 실행하고, 명령·웹 사용을 로그에 남긴다 | ADR-0002, ADR-0003, ADR-0033 |
| Gemini 런타임 | Google ADK 로 작업 실행 (AI Studio API 키) | ADR-0003 |
| 자체 MCP 서버들 | 프로젝트 전용 도구 | ADR-0004 |
| 운영 CLI | 부트스트랩(Argo CD 설치 등), 로컬 개발용 빌드·접속 | ADR-0008 |

### 기성 제품·서비스 (설정만 관리)

| 구성요소 | 역할 | 근거 |
|---|---|---|
| agentgateway | MCP 통합 엔드포인트, 도구 접근 제어 | ADR-0004 |
| 외부 MCP 서버들 | 기성 도구 | ADR-0004 |
| Langfuse Cloud | 트레이싱, 프롬프트 관리, 평가, 피드백 | ADR-0001, ADR-0006 |
| GitHub / Actions / GHCR | 저장소(공개), 검사·빌드, 이미지 레지스트리 | ADR-0008 |
| Argo CD (+ Image Updater) | git → 클러스터 배포, 새 이미지 태그 반영 | ADR-0008 |
| 상태 저장소 / 작업 큐 | core 의 상태와 작업 배정 | ADR-0010 (기술 미정) |

## 요청 흐름

```
 Slack ──(Socket Mode)──► Slack 커넥터 ──이벤트──► enikk-core
                              ▲                      │  역할 정의 → 엔진 결정
                              │                      │  작업 배정 (큐)
                              │            ┌─────────┴──────────┐
                              │            ▼                    ▼
                              │      Claude 런타임          Gemini 런타임
                              │            │ 모델 호출(직접)     │ 모델 호출(직접)
                              │            ├──► Anthropic API   ├──► Google API
                              │            │                    │
                              │            └── 도구 호출 (MCP) ──┘
                              │                      ▼
                              │                agentgateway ──► MCP 서버들
                              │
                              └──── 결과 게시 ◄── enikk-core ◄── 결과 보고 (런타임)

 모든 단계 ──(OpenTelemetry)──► Langfuse Cloud
 런타임 ──(실행 시 조회)──► Langfuse 프롬프트
```

## 변경·배포 흐름

```
 Slack 프롬프트 변경 요청 ─► assistant 역할 ─► enikk-core ─► Langfuse 후보 버전(candidate)
                                                   └─ 소유자 DM [승격] ─► core 가 production 라벨 이동 (ADR-0032)

 Slack 개선 요청 ─► enikk-core ─► 개발 역할 (격리 Pod)
                                     └─ 정의·코드: 브랜치 → PR → 소유자 승인 → 머지
                                                                         │
                     GitHub Actions 빌드 → GHCR ─► Argo CD Image Updater ─┤
                                                                         ▼
                                                              Argo CD 가 클러스터에 배포
```

## Slack 알림 위치 (ADR-0026, ADR-0036)

| 알림 | 위치 | 설정 (`config/slack.yaml`) | 게시 실패 시 |
|---|---|---|---|
| PR 승인 요청·검사 실패·승인 결과(ADR-0020), 프롬프트 승격 요청(ADR-0032) | 승인 채널 | `approval_channel`, 기본 `C0CL7HZ5UQ1` (환경변수 `ENIKK_CONNECTOR_APPROVAL_CHANNEL`) | 로그를 남기고 "<채널> 에 올리지 못해 DM 으로 보냅니다"를 붙여 소유자 DM |
| 배포 성공·실패, 이미지 빌드 실패, 롤백 결과(ADR-0023) | #release | `deploy_notify_channel`, 기본 `C0CKT1ZNZ43` (환경변수 `ENIKK_CONNECTOR_DEPLOY_NOTIFY_CHANNEL`) | 같은 방식으로 소유자 DM |
| 개발 역할 PR 의 진행 | 원래 요청 스레드의 PR 상태 카드 한 줄(링크와 머지·반려 결과만) | — | 로그를 남기고 다음 단계에서 다시 올림 |
| 날짜별 대화 요약 실패(ADR-0035) | 소유자 DM | — | — |

- 봇이 두 채널의 구성원이어야 한다. 승인 채널은 비공개이므로 소유자가 `/invite @enikk` 로 초대한다. DM 마저 일시적 오류로 실패하면 알림을 확인 처리하지 않아 다음 조회에서 채널부터 다시 시도한다.
- 채널의 버튼([승인하고 머지] [반려] [승격] [거절] [되돌리기])은 누구나 볼 수 있지만 소유자만 실행된다. 커넥터가 누른 사람을 확인하고, 다른 사람에게는 그 사람에게만 보이는 거절 안내를 보낸다(I-01).
- **원래 요청 스레드**(채널 또는 DM): **PR 상태 카드** 하나. 카드는 버튼 없는 한 줄로 승인 채널 스레드 링크와 결과(머지됨 / 반려됨 / 닫힘)만 담는다(GitHub 링크 없음). 결과가 나면 같은 메시지를 `chat.update` 로 고치고, 실패해도 새 글은 올리지 않는다(ADR-0036 §3, 2026-11-24 개정).
  - core 가 카드 알림을 notices 큐에 넣고, 커넥터가 처음 올린 카드의 위치를 notices 확인 요청에 담아 돌려준다. core 는 DB `status_cards` 에 두고 다음 알림에 채워 준다.
  - 개발 역할의 답에 PR 링크가 있으면 core 가 답 대신 상태 한 줄을 올린다. PR 링크가 없는 답(PR 을 만들지 않은 이유 등)은 그대로 올린다.
- **승인 메시지 스레드**(승인 채널): 원래 요청 스레드 링크, PR 상세(본문, 바뀐 파일, 검사 결과), 개발 역할의 보고 전문, diff 파일. 같은 PR 의 새 커밋은 같은 메시지를 고친다(ADR-0020 §4). 처리하면 버튼이 결과 문구로 바뀐다.
- **양방향 링크**: 요청 스레드 → 승인 메시지는 카드의 링크, 승인 메시지 → 요청 스레드는 승인 메시지 스레드의 첫 댓글. permalink 를 얻지 못하거나 core 호출이 실패하면 링크 없이 나머지를 계속 올린다.
- 배포 상세(앱별 결과, 실패 이유, [되돌리기])는 #release 에만 있다.
- 프롬프트 승격 요청의 스레드 댓글에는 원래 요청 스레드 링크와 본문·Config diff 가 있다. 원래 요청 스레드에는 상태와 승격 요청 링크만 올린다. 승격 결과 메시지에는 [되돌리기] 가 붙는다.

## 미결정 사항

- core 상태 저장소, 작업 큐 기술, core API 형식 (ADR-0010).
- Argo CD Image Updater 의 태그 기록 대상과 자격증명 범위 (ADR-0008).
- 역할 정의를 Slack 메시지와 연결하는 규칙 파일의 형식.
- `src/` 코드 구조 ([code-structure.md](code-structure.md)).
- ADR-0003, ADR-0006 의 후속 확인 사항.
