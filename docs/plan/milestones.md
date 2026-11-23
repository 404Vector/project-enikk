# 구현 마일스톤

- 상태: 승인
- 최종 갱신: 2026-11-22
- 근거: [architecture/code-structure.md](../architecture/code-structure.md), ADR-0001 ~ ADR-0011

## 진행 원칙

- 각 마일스톤은 **실제로 동작을 확인할 수 있는 상태**로 끝난다. 완료 조건을 만족해야 다음으로 넘어간다.
- 마일스톤 시작 전에 세부 작업 목록을 소유자에게 리뷰받고, 끝나면 결과와 문서 변경을 보고한다.
- 구현 중 새 결정이 필요하면 코드보다 ADR 을 먼저 제안한다.
- 비밀값(토큰, 키)은 소유자가 직접 만들어 Secret 으로 넣는다 (I-21). 각 마일스톤의 "소유자 준비물"에 적는다.

## 순서와 이유

```
M0 기반 정비 ─► M1 배포 플랫폼 ─► M2 core 골격 ─► M3 첫 대화 (E2E)
                                                    │
               M7 자기 개선 ◄─ M6 LLMOps ◄─ M5 Gemini ◄─ M4 MCP 도구
```

- M0~M2 에서 배포 경로와 core 를 먼저 세우는 이유: 이후 모든 기능이 "git → Argo CD → 클러스터" 경로와 core API 위에서 동작하므로, 나중에 바꾸면 비용이 가장 큰 부분을 먼저 검증한다.
- M3 에서 처음으로 Slack 대화가 끝까지 동작하는 가장 얇은 경로(walking skeleton)를 만든다. 이후 마일스톤은 이 경로에 기능을 붙인다.

---

## M0. 기반 정비 — 완료 (2026-11-21, PR #1)

**목표**: 저장소·도구·CI 를 새 구조에 맞춘다. 기능 코드는 없다.

- git 초기화, GitHub 공개 저장소 연결, secret scanning·push protection 활성화
- `main` 브랜치 보호 규칙 (PR 필수, 검사 통과 필수, 강제 push 금지)
- workspace 재구성: `libs/kernel`(기존 core 이전, pydantic 전환), `libs/contracts`(빈 골격), `libs/adapters/platform`(기존 platform 이전)
- 정적 검사 구성: ruff(금지 API 규칙 포함), mypy strict + pydantic 플러그인, import-linter 계약(code-structure.md §4), pytest
- GitHub Actions: PR 마다 위 검사 실행 (GitHub 호스팅 runner)
- 기존 실험용 차트·Dockerfile·클러스터 릴리스 정리

**완료 조건**: PR 에서 검사가 돌고, 계층 위반 코드를 넣으면 CI 가 실패한다.
**소유자 준비물**: GitHub 저장소 생성(또는 생성 권한), 원격 URL.

## M1. 배포 플랫폼 — 완료 (2026-11-21, PR #2, #3)

**목표**: "git 에 머지하면 클러스터에 반영되는" 경로를 만든다.

- ops-cli 부트스트랩 명령: Argo CD 설치, app-of-apps 등록
- `deploy/argocd/` Application 정의, CloudNativePG 오퍼레이터 + Postgres 클러스터
- GitHub Actions: 앱 이미지 빌드 → GHCR 푸시 (커밋 SHA 태그)
- Argo CD Image Updater: 새 태그 반영 (ADR-0008: write-back method `argocd`)
- 확인용 최소 앱(health 만 있는 core 골격)으로 전체 경로 검증

**완료 조건**: core 골격 코드를 고쳐 머지하면 사람 개입 없이 새 이미지가 클러스터에 배포된다.
**소유자 준비물**: 없음 (공개 저장소·이미지이므로 pull 자격증명 불필요).

## M2. core 골격 — 완료 (2026-11-21, PR #4 ~ #9)

**목표**: core 의 상태 관리·작업 큐·API·인증을 완성한다. 아직 실제 역할 실행은 없다.
**세부 결정**: ADR-0012

- `contracts` v0: 이벤트, 작업, 결과, outbox 메시지, 역할 정의 스키마
- `core-domain`: Task·Session 상태 기계, 레포지토리·UoW·JobQueue 포트
- `persistence-sql`: SQLAlchemy 구현, Postgres 큐(SKIP LOCKED, 임대), Alembic 마이그레이션
- `k8s-auth`: TokenReview 검증기, 역할 매핑
- core 앱: 이벤트 접수, 작업 배정·임대·재배정, 결과·진행 보고, outbox, 역할 정의 카탈로그(`agents/` 로딩·검증)
- `core-client`: 타입 있는 클라이언트 + projected token 첨부
- 테스트: 도메인 단위 테스트(가짜 레포지토리), 영속성 통합 테스트(실제 Postgres)

**완료 조건**: 가짜 커넥터·가짜 런타임(테스트용 클라이언트)으로 "이벤트 → 작업 배정 → 결과 → outbox" 흐름이 클러스터에서 동작하고, 허용되지 않은 ServiceAccount 의 호출은 거부된다.
**작업 단위(PR)**:
1. 문서 정리, kernel 보강(시계·ID 포트), contracts v0, 외부 SDK import 위치 제한
2. core-domain: 상태 기계, 포트, 메모리 기반 가짜 구현
3. persistence-sql: SQLAlchemy 레포지토리·UoW·Postgres 큐, Alembic, 통합 테스트(testcontainers)
4. k8s-auth(TokenReview), core-client
5. core 앱 조립, 배포 설정(마이그레이션 Job, ServiceAccount·권한), `enikk-ops smoke core` 로 클러스터 검증

## M3. 첫 대화 (End-to-End) — 완료 (2026-11-22, PR #12 ~ #17)

**목표**: Slack 에 말을 걸면 Claude 엔진 역할이 답한다.

- 비밀값 관리 ([ADR-0013](../adr/0013-credentials-local-file-core-broker.md)): `enikk-ops secret`, core 의 비밀값 API
- `connector-slack`: Socket Mode, 소유자 확인(I-01), 연결 규칙(`config/`), 진행 상태 표시
- `runtime` 프레임워크: 작업 가져오기 루프, 임대 연장, 프롬프트 조회(캐시·실패 시 거부, I-13)
- `engine-claude`: Claude Agent SDK, 내장 도구 전부 차단(I-14), 세션 유지(I-11)
- `langfuse` 어댑터: PromptStore, OTel 트레이스 내보내기
- 역할 정의 1개 (도구 없음), Langfuse 프롬프트 1개

**완료 조건**: Slack 스레드에서 대화가 이어지고, Langfuse 에 프롬프트 버전과 연결된 트레이스가 남는다. 다른 사용자의 메시지는 무시된다.
**소유자 준비물**: Slack 앱(Socket Mode, 봇·앱 토큰), `claude setup-token` 토큰, Langfuse 프로젝트 키와 프롬프트. 토큰과 키는 `enikk-ops secret set` 으로 등록한다.
**세부 결정**: ADR-0013 ~ ADR-0018
**확인 결과 (2026-11-22)**:
- 소유자가 DM 과 채널 멘션으로 대화했다. 같은 스레드의 후속 메시지가 앞의 내용을 기억했고, 채널 스레드에서는 멘션 없는 답글로도 이어졌다.
- Langfuse 에 generation 4건이 `assistant` v1 과 연결되어 남았고, 스레드별로 같은 세션에 묶였다.
- 다른 사용자의 메시지를 무시하는 동작은 connector-slack 테스트로 확인했다(다른 계정으로는 확인하지 않음).
- 런타임은 작업마다 빈 설정 디렉터리에서 core 의 대화 이력으로 이어 간다(ADR-0014, `enikk-ops smoke claude`). Pod 재시작 뒤의 Slack 대화는 따로 확인하지 않았다.

## M4. MCP 도구

**목표**: 에닉이 agentgateway 를 통해 MCP 도구를 쓴다.

- agentgateway 배포·구성, 첫 MCP 서버(외부 1개 또는 자체 1개)
- 역할 정의의 도구 허용 목록을 엔진 설정과 게이트웨이 양쪽에서 적용
- 도구 호출 트레이스

**완료 조건**: 허용된 도구만 호출되고, 허용되지 않은 도구 호출은 차단·기록된다.
**소유자 준비물**: MCP 서버가 쓰는 외부 자격증명 (있는 경우).

## M5. Gemini 엔진과 엔진 간 위임

**목표**: Gemini 엔진 역할을 쓰고, Claude 엔진 역할이 Gemini 엔진 역할에 일을 맡길 수 있다.

- `engine-gemini`(Google ADK), `runtime-gemini` 앱
- core 의 부모-자식 작업 위임 (명시적 입력·출력만, I-12)
- 엔진·모델 호환성 검증 (I-16)
- ADK → Langfuse 트레이스 연동 확인 (ADR-0003 후속 확인 사항)

**완료 조건**: 이미지 생성 같은 Gemini 작업이 Slack 에서 동작하고, 위임 흐름이 하나의 트레이스로 이어진다.
**소유자 준비물**: Google AI Studio API 키. Gemini API 약관 확인.

## M6. LLMOps

**목표**: 품질을 측정하고 프롬프트를 안전하게 바꾼다.

- Slack 반응(👍/👎 등) → Langfuse 점수 (FeedbackSink)
- `eval-runner`: Langfuse 데이터셋 기반 평가
- 프롬프트 후보(`candidate`) → 평가 → Slack 승인 → `production` 승격 흐름 (core 승인 기능)

**완료 조건**: 프롬프트 후보를 평가 결과와 함께 Slack 에서 승인·롤백할 수 있다.

## M7. 자기 개선 — 완료 (2026-11-22, PR #10 ~ #40)

**목표**: Slack 대화로 정의·코드 변경을 요청하면 PR 이 만들어지고, 승인하면 배포된다.

- 개발 역할 배포 프로필(전용 이미지·ServiceAccount·격리 Pod, 실행 빈도 제한)
- GitHub App 신원, 민감 경로의 강화된 승인(I-20), PR 생성·Slack 승인 연동
- 배포 실패 시 자동 롤백 확인

**완료 조건**: Slack 요청 → PR → 승인 → 자동 배포가 사람의 수동 작업 없이 동작하고, 배포 결과와 롤백 결과가 DM 으로 온다. 민감 경로를 건드린 PR 은 강화된 승인을 거친다(ADR-0024).
**소유자 준비물**: GitHub App 생성·설치, 그 자격증명.
**세부 결정**: ADR-0019 ~ ADR-0024. 프롬프트 개선 경로는 M6 에서 다룬다.
**작업 순서**:
1. CI 보강: PR 빌드에서 이미지 push 권한 제거(ADR-0008 위험), 보호 경로 검사, `main` ruleset 과 auto-merge 설정. (보호 경로 검사는 7 에서 민감 경로로 대체)
2. GitHub 어댑터와 core 의 토큰 발급(ADR-0019), 키 등록(`secret set` 의 파일 입력).
3. 개발 역할(developer): `enikk-dev` 네임스페이스, Job 템플릿 차트, 이미지, `agents/developer.yaml`, core dispatcher 와 실행 제한(ADR-0021).
4. 위임: `delegate` 도구, 하위 작업과 세션, 스레드 게시(ADR-0022).
5. 승인: PR 감시, 소유자 DM 승인 요청, Slack 버튼, 승인 상태 기록과 auto-merge(ADR-0020).
6. 배포 안전장치(ADR-0023): RollingUpdate 전환과 PostSync 스모크 검사, 배포 감시와 결과 DM(성공·실패, 빌드 실패), [되돌리기]와 롤백 결과 DM, 끝에서 끝까지 확인.
7. 민감 경로(ADR-0024): core 판정과 강화된 승인, 개발 토큰 `workflows` 권한, `protected-paths` 워크플로와 필수 검사 제거.

**확인한 것** (2026-11-22)
- Slack 요청 → 개발 역할 PR(#34) → DM 승인 → 머지 → 자동 배포 → "배포 성공" DM.
- 일부러 실패시킨 배포(#37) → "배포 실패" DM → [되돌리기] → core 가 연 되돌리기 PR(#38) → 승인 → "롤백 성공" DM. 실패하는 동안 이전 Pod 가 계속 동작했다.
- 민감 경로를 바꾼 PR(#40) 의 승인 요청에 민감 경로 목록과 diff 가 담겼고, 확인 창을 거쳐 머지됐다.

**이어지는 일**: 위임한 스레드로 결과 알림, 같은 PR 이어서 고치기(ADR-0025).
