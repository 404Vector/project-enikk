# ADR-0037: 에닉 하나가 스킬을 불러 직접 일한다 — 공유 볼륨, 스레드 세션, 권한 동작은 core 경유

- 상태: 승인 (1단계 구현: 이 ADR 과 같은 PR. 2단계는 2a(스킬 config·`load_skill`)와 2b(core 의 PR 제출 도구)로 나눠 구현한다. 2a·2b 구현됨. 3단계 구현됨: 3a(동시 실행 칸·작업 디렉터리·자원·git 신원)와 3b(취소·대기 표시·CLI 홈 격리). 4단계는 4a~4e 로 나눠 구현한다. 4a(runtime 의 uv·한도) 구현됨. 4e-1(이미지 빌드에서 dev-agent 제외, Argo finalizer)·4e-2(위임·개발 Job·`enikk-dev` 제거와 대체 표시) 구현됨. 역할 개념 자체의 정리는 5단계로 넘긴다. 5단계는 따로 구현한다)
- 날짜: 2026-11-23
- 관련: ADR-0005, ADR-0006, ADR-0007, ADR-0010, ADR-0011, ADR-0014, ADR-0017, ADR-0019, ADR-0021, ADR-0022, ADR-0025, ADR-0027, ADR-0030, ADR-0031, ADR-0032, ADR-0033, ADR-0034, ADR-0035, ADR-0036, I-10, I-11, I-12, I-14, I-16, I-17, I-25, I-28, I-29, I-32, I-37, I-38

> 결정은 2026-11-23 소유자가 Slack 스레드에서 확정했다. 이 ADR 은 여러 기존 ADR 과 불변식을 바꾸지만, 대체 표시는 **실제로 바뀌는 단계**에서 단다(아래 "바뀌는 ADR·불변식"). 1단계에서 바뀌는 것(ADR-0010 의 I-25, ADR-0033 §2.5)과 2a 에서 바뀌는 것(ADR-0032·I-36, ADR-0005·I-16 의 스킬 부분), 2b 에서 바뀌는 것(ADR-0019 §2, ADR-0021 §3·§7, ADR-0007 의 I-17, I-32), 3단계에서 바뀌는 것(ADR-0012 의 I-29, ADR-0014, ADR-0027, ADR-0034), 4e-2 에서 바뀌는 것(ADR-0007, ADR-0019, ADR-0021, ADR-0022, ADR-0025, ADR-0030, ADR-0031, I-11, I-12, I-16, I-17, I-20, I-28, I-32)만 표시했다.

## 배경

### 문제
역할(assistant, developer, summarizer)마다 실행 환경이 나뉘어 있다.
- 대화 역할(assistant)은 저장소를 정확히 보지 못한다. 저장소에 관한 도구는 열린 PR 만 돌려주는 `pull_requests` 뿐이고, WebFetch 는 작은 모델의 요약을 돌려준다(ADR-0033). 그래서 코드 근거로 리뷰하지 못했다.
- 개발 역할(developer)은 대화를 보지 못한다. 요청을 긴 위임문으로 옮기다 맥락이 빠진다. 실제로 "초안"을 원한 요청이 PR 이 되었다(PR #70).
- 위임한 작업은 취소할 수 없다.

소유자 판단: 지금의 "역할"은 지시문만 보면 사실상 **스킬**(필요할 때 불러 쓰는 절차와 지식)이고, 다른 Pod 에 위임하는 구조는 설계 오류다. 역할을 실제로 가르던 것은 권한과 실행 환경뿐이다.

### 제약
- 클러스터는 온프레미스이고 자원이 한정되어 있다. 늘 떠 있는 보조 프로세스(git-sync 사이드카 등)는 두지 않는다.
- Langfuse 는 클라우드다(ADR-0001).

### 지금 구조에서 확인한 사실 (main 6ebdbd8, PR #70 조사)
- runtime-claude 는 Deployment 복제본 1개이고 **작업을 한 번에 하나만** 임대한다(`enikk_runtime/worker.py`). 요청 100m/384Mi, 한도 1536Mi. `/work` emptyDir 64Mi 는 작업마다 비운다(ADR-0033 §2.5).
- 개발 Job(ADR-0021)은 `enikk-dev` 네임스페이스에서 작업마다 뜨고, **쉴 때는 자원을 쓰지 않는다**. 하나에 요청 512Mi/한도 3Gi, emptyDir 4Gi, 매번 `git clone --depth 100` 과 `uv sync` 를 하므로 시작이 수십 초 늦다. 이미지에 git·gh·uv·PostgreSQL 18 이 있다(ADR-0030).
- 개발용 GitHub 토큰은 개발 Pod 의 gh 설정 파일에 있고, push 와 PR 생성은 모델이 Bash 로 직접 한다. 모델은 그 토큰을 읽을 수 있다(ADR-0021 이 감수한 위험).
- PVC 는 네임스페이스에 속한다. 그래서 `enikk` 의 runtime 과 `enikk-dev` 의 개발 Job 은 PVC 하나를 함께 쓸 수 없다. `enikk-dev` 는 PSA `restricted` 라 hostPath 도 쓸 수 없다.
- 지금 PVC 는 CNPG 의 2Gi 하나이고 `storageClass` 를 지정하지 않는다(클러스터 기본값). 대상 클러스터는 kube context `docker-desktop` 이며 ADR-0011·ADR-0013 은 단일 노드라고 적었다. 지금도 노드가 하나인지, 기본 스토리지 클래스가 무엇인지는 이 ADR 을 쓰며 확인하지 못했다. **[추정: Docker Desktop 의 `hostpath`, RWO]**
- runtime 과 개발 Pod 는 모두 UID/GID 10001, fsGroup 10001 로 돈다. runtime 이미지에는 git 과 curl 이 없었다.

## 검토한 대안
- **A. 지금 구조 유지 + 대화 역할에 저장소 읽기 도구만 추가**: 싸고 보안 경계가 그대로다. 그러나 맥락 손실, 취소 불가, 토큰 노출은 남는다. → 기각.
- **B. 공유 볼륨 + 역할 구조 유지(개발 Job 도 같은 볼륨)**: PVC 가 네임스페이스 단위라 개발 Job 을 `enikk` 로 옮기거나 정적 PV 를 둬야 한다. 토큰을 가진 개발 Pod 와 토큰 없는 runtime 이 같은 `.git` 을 쓰게 되어, `.git` 의 hooks·config 로 토큰을 훔치는 길이 생긴다. → 기각.
- **C. 에닉 하나 + 스킬 + 공유 볼륨 + 권한 동작은 core 경유**: 채택. 상주 자원이 늘고 구현이 크다. 대신 맥락 손실과 Pod 기동 지연이 사라지고, 모델이 GitHub 토큰을 읽을 수 없게 된다.
- **D. 볼륨 없이 push·PR 만 core 도구로**: 토큰 문제만 푼다. 맥락 손실과 실행 환경 둘은 남는다. → C 의 2단계로 흡수.

## 결정

### 1. 에이전트는 에닉 하나
- 역할 개념을 없앤다. 기본 프롬프트 `enikk`(캐릭터, 정직함, 서식, 기억, 승인 원칙)는 모든 세션에 늘 로드한다.

### 2. 스킬 = Langfuse Prompt
- 스킬은 Langfuse 의 Prompt 다. Langfuse 의 베타 Skill Management 는 쓰지 않는다. 이름 예: `skill-developer`, `skill-conversation-summary`.
- prompt config 에 둔다:
  - `model`, `effort`
  - `description`: 언제 쓰는지 한 줄
  - `requires`: 이 스킬이 쓰는 강제 권한 목록. 예: `repo.push`, `pr.write`, `prompt.propose`
- 승격은 지금의 `propose_prompt` 흐름(ADR-0032)을 쓴다. `requires` 를 넓히는 변경은 승인 카드에 경고를 띄운다.
- 긴 절차는 스킬 본문에 복사하지 않고 저장소 문서를 가리킨다.

### 3. 스킬 선택
- 세션 시작 때 기본 프롬프트와 스킬 목록(이름, `description`)을 준다.
- 에이전트는 `load_skill(name)` 으로 본문을 불러온다. 불러온 스킬과 버전은 트레이스에 남긴다.
- 스케줄 작업(대화 요약 등)은 스킬을 미리 로드한 세션으로 연다.

### 4. 세션 = Slack 스레드
- 세션은 Slack 스레드(채널 + `thread_ts`)다. 같은 스레드의 댓글은 같은 세션을 이어 간다(Agent SDK 세션 재개. 방식은 구현 때 확인한다).
- 한 스레드 안은 직렬로 처리하고, 서로 다른 스레드는 동시에 **2개**까지 처리한다.
- 스레드에서 실행 중인 세션을 취소하는 수단을 둔다.

### 5. 도구는 세션 시작 때 모두 갖춘다
- 파일·Bash·웹, 기억, 운영 조회, 저장소 읽기와 로컬 git, 프롬프트 읽기·제안, PR 제출.
- `delegate` 는 없앤다.

### 6. 권한 강제 세 단계
- **(a) 자유**: 읽기, 로컬 파일과 로컬 git, 웹, 기억.
- **(b) core 경유**: 브랜치 push, PR 생성·갱신, 프롬프트 후보 등록.
  - GitHub 토큰은 core 만 갖는다.
  - 세션은 변경을 bundle 이나 patch 로 넘긴다. core 는 공유 볼륨 밖의 **자기 전용 clone** 에 적용해 push 한다.
  - core 는 공유 볼륨의 `.git` 에서 git 을 실행하지 않는다. 세션이 그 `.git` 에 hooks 나 config(`core.fsmonitor`, `core.hooksPath` 등)를 심어 core 의 권한으로 코드를 실행하는 길을 막는다.
  - core 는 확인한다: `main` 대상 금지, 강제 push 금지, 브랜치가 그 스레드에 묶였는지, 민감 경로 표시(ADR-0024).
- **(c) 소유자 승인**: 머지, 프롬프트 승격, 배포 되돌리기. 지금처럼 승인 채널에서 하고 버튼은 소유자만 누른다(ADR-0036, I-01).

### 7. 공유 볼륨
- `enikk` 네임스페이스에 PVC 하나를 둔다.

  | 경로 | 내용 |
  |---|---|
  | `/data/repo.git` | 기준 저장소(bare). fetch·gc 는 잠금을 잡은 한 곳에서만 한다. `gc.auto=0` |
  | `/data/worktrees/<스레드 ID>` | 그 스레드의 git worktree |
  | `/data/tmp/<스레드 ID>` | 웹에서 받은 파일 등 신뢰하지 않는 자료 |

- 세션 시작 때 `git worktree prune` 과 오래된 잠금 정리를 한다. 7일 동안 활동이 없는 스레드의 작업 공간은 지운다. 용량 한도를 둔다.
- 비밀값과 자격 증명은 볼륨에 두지 않는다.
- `enikk-dev` 네임스페이스는 4단계에서 정리한다.

### 8. runtime 이미지와 자원
- runtime 이미지에 개발 도구(git, uv, 검사 도구)를 넣는다.
- ~~Postgres 통합 테스트는 기존 Postgres(CNPG)의 테스트 전용 DB 로 돌린다.~~ **4a 에서 바뀜**: runtime 에는 테스트 DB 를 두지 않고, 영속성 통합 테스트는 CI 에서만 돌린다(아래 4a).
- runtime 메모리 한도는 동시 처리 2 기준으로 조정한다(3a 에서 requests 만 올렸고, 4a 에서 limits 1536Mi 를 유지하기로 했다).
- **대가**: 쉴 때 0 이던 개발 Job 의 자원이 상주 runtime 으로 옮겨 와 상주 자원이 늘어난다. 그 대가로 Pod 기동 지연(수십 초)과 위임문으로 인한 맥락 손실이 사라진다.

### 9. 요약 스킬은 도구 없이
- 대화 요약은 지금처럼 도구 없이 실행한다(ADR-0035, I-38 의 의도된 안전장치). 바꾸려면 별도로 결정한다.

### 없애는 것
- `delegate` 도구(ADR-0022), 개발 Job 흐름(ADR-0021), `enikk-dev` 네임스페이스, 역할별 프롬프트(`assistant` → 기본 프롬프트 `enikk`, `developer`·`conversation-summary` → 스킬), 역할 기준 작업 분류.

### 1단계 구현 세부 (2026-11-23, 이 ADR 과 같은 PR)
- **볼륨**: `deploy/helm/runtime-claude` 가 PVC `enikk-runtime-claude-data`(5Gi, RWO, 클러스터 기본 스토리지 클래스)를 만들고 runtime 의 `/data` 에 붙인다. `fsGroupChangePolicy: OnRootMismatch`. 노드가 하나라는 기록(ADR-0011·ADR-0013)을 근거로 RWO 를 골랐다. RWO 는 같은 노드의 여러 Pod 가 함께 마운트할 수 있으므로 롤링 업데이트 중 두 Pod 가 함께 쓸 수 있다.
- **준비 시점**: 기본 도구를 가진 공용 역할(지금은 assistant)의 작업이 시작될 때마다 runtime 이 볼륨을 준비한다(`enikk_runtime.workspace.SessionWorkspaces`). 사이드카는 없다. 스레드 ID 는 core 의 세션 ID(스레드 하나에 세션 하나, I-11)다. 준비에 실패해도 작업은 볼륨 없이 진행한다.
- **잠금**: 볼륨 루트의 `.lock` 을 `flock` 으로 잡고 아래를 순서대로 한다.
  1. 기준 저장소가 없으면 `git init --bare --template=` 로 만든다.
  2. 기준 저장소의 `config` 를 정해진 내용으로 다시 쓰고(`hooksPath=/dev/null`, `fsmonitor=false`, `gc.auto=0`, `maintenance.auto=false`, remote), `hooks/`, `objects/info/alternates`, `info/attributes` 를 지운다. 세션이 심은 설정이 다음 세션이나 runtime 의 git 에 남지 않게 한다.
  3. 마지막 fetch 가 60초보다 오래되었으면 GitHub 에서 `main` 을 fetch 한다. 공개 저장소라 토큰을 쓰지 않는다. 가져온 커밋 SHA 는 세션이 쓸 수 없는 `state/` 에 적는다. 세션은 기준 저장소의 ref 를 바꿀 수 있으므로 ref 가 아니라 이 기록을 믿는다.
  4. 그 커밋의 파일을 `/data/snapshots/<커밋>/` 에 풀어 둔다(`git archive` + `tarfile` 의 `data` 필터). 최근 두 개만 남긴다.
  5. 10분보다 오래된 git 잠금 파일(`*.lock`)을 지우고 `git worktree prune` 을 한다.
  6. 7일 동안 활동이 없는 다른 스레드의 worktree·브랜치·tmp 를 지운다.
  7. 이 스레드의 worktree(`/data/worktrees/<세션>`, 브랜치 `session/<세션>`)를 만들거나 다시 쓰고, `/data/tmp/<세션>` 을 만든다. 남은 공간이 512MiB 보다 적으면 새 worktree 는 만들지 않는다.
- **git 실행**: runtime 이 실행하는 git 은 시스템·전역 설정을 무시하고(`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`) hooks·fsmonitor 를 끄며, `timeout` 으로 시간을 제한하고, 엔진의 샌드박스 셸(`enikk-sandbox-shell`, ADR-0033) 안에서 돈다. 그래서 볼륨에 무엇이 심겨 있어도 core 토큰을 읽지 못한다. Landlock 이 없는 노드에서는 샌드박스 없이 돌고 경고를 남긴다.
- **모델이 닿는 곳**: Landlock 규칙에 스냅샷(읽기), 기준 저장소와 그 스레드의 worktree·tmp(쓰기)만 더한다. 다른 스레드의 worktree·tmp 와 `state/` 는 열리지 않는다.
- **알려 주기**: 작업 입력 맨 앞에 `[작업 공간 …]` 머리말로 저장소 이름, 커밋 SHA, 가져온 시각, 스냅샷·worktree·tmp 경로를 붙인다. fetch 에 실패했으면 이전 커밋일 수 있다고 적는다.
- **이미지**: 공용 Dockerfile 이 `PACKAGE=enikk-runtime-claude` 일 때만 git 과 ca-certificates 를 설치한다. uv·검사 도구·Postgres 는 4단계에서 넣는다.
- 아직 하지 않은 것: push 권한, `load_skill`, 스레드 세션화와 취소, 동시 처리 2, `delegate`·개발 Job 제거.

### 2a 구현 세부 (2026-11-23)
2단계는 둘로 나눈다. 2a 는 스킬의 형식과 불러오기, 2b 는 core 의 PR 제출 도구(patch·bundle → core 전용 clone → push·PR)다. 2a 는 `delegate`·개발 Job 을 건드리지 않고, Langfuse 에 스킬을 만들거나 assistant 프롬프트를 바꾸지 않는다.
- **스킬 config 스키마** (`enikk_contracts.skills`): 스킬은 이름이 `skill-` 로 시작하는 text 프롬프트다(`skill-` 뒤는 소문자·숫자·`-`, 63자 이하). config 는 아래 키만 가진다. 다른 키나 알 수 없는 값이 있으면 거부한다.
  - `model`(필수), `effort`(선택)
  - `description`(필수): 언제 쓰는지 한 줄, 200자 이하
  - `requires`(선택, 기본 없음): `repo.push`, `pr.write`, `prompt.propose` 중에서, 겹치지 않게. 2a 에서는 선언과 승격 경고에만 쓴다. core 가 이 값으로 동작을 허용·거부하는 것은 2b 부터다.
- **후보와 승격** (ADR-0032 의 흐름을 넓힘):
  - `propose_prompt` 의 대상은 역할의 프롬프트이거나 스킬(`skill-*`)이다. 스킬은 아직 어떤 역할 정의도 쓰지 않으므로 core 는 "역할이 쓰는 이름" 검사를 스킬에는 하지 않는다. 후보를 만들 수 있는 역할(`prompt-candidates`)과 그 밖의 검사는 그대로다.
  - config 변경에 `description`, `requires`(새 목록 전체)를 넘길 수 있다. 역할의 프롬프트에 이 둘을 넘기면 거부한다. 스킬 후보는 `production` 의 config 위에 덮어쓴 결과가 위 스키마를 통과해야 한다. 새 스킬은 `model` 과 `description` 이 필요하다.
  - core 는 후보의 `requires` 에서 `production` 에 없던 것(새 스킬이면 전부)을 승격 요청(`PromotionRequested.requires_added`)에 담는다. 승인 채널의 승격 카드는 그 값이 있으면 `:warning: 권한 확장` 경고를 버튼 위에 띄우고, [승격] 확인 창에도 새로 요구하는 동작을 적는다(ADR-0036). 좁히거나 그대로 두는 변경은 경고 없이 Config diff 로만 보인다.
- **`load_skill(name)`** (플랫폼 도구, runtime 이 Langfuse 에서 직접 읽는다): 스킬의 `production` 본문과 버전, `requires` 를 돌려준다. config 가 스키마에 맞지 않거나 `production` 이 없는 스킬은 거부한다. 불러온 스킬의 이름과 버전은 runtime 로그와 그 작업의 Langfuse 트레이스(trace·generation 의 metadata `skills`)에 남는다. 실패한 실행의 트레이스에는 남지 않고 로그에만 남는다.
- **스킬 목록**: 세션의 각 작업 입력 맨 앞에 `[스킬 …]` 머리말로 쓸 수 있는 스킬(이름, `description`)을 붙인다. Langfuse 의 `production` label 이 달린 프롬프트 가운데 `skill-*` 이고 스키마를 통과한 것만, 최대 50개를 이름 순으로 싣는다. 목록과 본문은 60초 캐시한다. 목록을 가져오지 못하면 빈 목록으로 알리고 `load_skill` 은 그대로 준다.
- **누가 받나**: 기본 도구를 가진 공용 역할(`execution: shared`, `tools.builtin` 이 있고 `platform_defaults: true`, 지금은 assistant)만. 개발 역할(격리)과 요약 역할(도구 없음, I-38)은 받지 않는다. 역할 정의 파일에 도구 이름을 더하지 않고 runtime 이 이 조건으로 준다. 스킬 본문은 지시만 담고 권한을 더하지 않는다(새 도구도, 새 core 동작도 생기지 않는다).
- **기능 플래그**: runtime 의 `ENIKK_RUNTIME_SKILLS__ENABLED`(Helm `deploy/helm/runtime-claude` 의 `skills.enabled`, 차트 기본 `true`, 코드 기본 `false`). 끄면 스킬 목록과 `load_skill` 이 모두 빠진다. 스킬 후보의 검증과 승격 경고는 core 에 늘 있다. 스킬이 쓰이지 않으면 아무 효과가 없으므로 따로 끄지 않는다.
- 아직 하지 않은 것(2b 이후): `requires` 의 강제, core 의 PR 제출 도구, 스케줄 작업에 스킬을 미리 로드하는 것, 기본 프롬프트 `enikk` 로의 전환.

### 2b 구현 세부 (2026-11-23)
2a 다음으로 §6(b)의 "브랜치 push, PR 생성·갱신"을 core 경유로 만든다. 개발 Job 과 `delegate` 는 4단계까지 그대로 동작한다.
- **도구**: 플랫폼 도구 `submit_pull_request(title?, body?)`. 역할 정의의 `tools.platform` 에 `pull-requests` 가 있고 그 작업에 스레드 worktree 가 있을 때만 준다(지금 `agents/assistant.yaml`).
- **runtime (모델이 아님)**: 볼륨 잠금을 잡고 1단계와 같은 샌드박스 git 으로 `session/<세션>` 을 `git bundle` 로 만든다. 범위는 `state/` 에 기록된 main 커밋과의 merge-base 이후, 커밋한 것만이다(작업 트리의 커밋하지 않은 변경은 가지 않는다). bundle 은 `state/bundles/` 에 잠시 쓰고 바로 지운다. 4MiB 를 넘으면 보내지 않는다.
- **전달**: runtime 이 bundle(base64)·base·head·제목·본문을 `POST /v1/tasks/{task}/pulls` 로 보낸다. core 는 `runtime` 역할과 그 작업의 활성 임대를 확인한다.
- **core 의 확인**: 역할에 `pull-requests` 가 있고 `execution: shared` 이며, 세션이 소유자가 Slack 에서 시작한 스레드다(위임받은 세션 아님). 브랜치는 요청이 아니라 세션으로 정한다: `agent/<세션 ID>`. 그래서 한 스레드는 자기 브랜치에만 push 할 수 있고, 그 PR 은 기존 승인 흐름(ADR-0020)과 스레드 상태 카드(ADR-0025·ADR-0036)에 그대로 이어진다. 상태 카드가 위임받은 세션뿐 아니라 Slack 스레드 자신의 세션도 찾도록 `ThreadNotes.origin` 을 넓혔다. 카드와 승인 채널 글의 연결(`ThreadNotes.card`, `ReviewLinks.opened`)은 `reply_to` 대신 `outbox_conversation` 을 쓴다. 처음에는 `reply_to` 만 봐서 스레드 자신의 PR 에 카드가 생기지 않았다(PR #76 에서 수정).
- **core 전용 clone** (`enikk_github.clone`, core Pod 의 emptyDir `/var/lib/enikk/clone`): bundle 은 바이트로만 받아 그 안에 파일로 쓰고 `git bundle list-heads`·`verify`·`unbundle` 한 뒤 지운다. core 는 공유 볼륨을 마운트하지 않으며, clone 위치가 `/data` 아래면 시작하지 않는다. 모든 git 은 시스템·전역 설정을 무시하고 hooks·fsmonitor 를 끄고 시간을 제한한다. 토큰은 환경변수의 git 설정(`http.extraheader`)으로만 git 에 가고 명령 인자에는 없다.
  - head 는 정확히 `refs/heads/session/<그 세션 ID>` 하나여야 하고 요청의 head 와 같아야 한다.
  - base 는 GitHub main 에 있는 커밋이어야 하고 head 는 base 위에 있어야 한다.
  - merge 커밋 금지, 커밋 50개·파일 300개 이하, `.github/workflows/` 를 건드린 커밋 금지(이 토큰에는 워크플로 권한도 없다).
  - 커밋을 GitHub App 봇을 작성자·커미터로 다시 쓴다(`git commit-tree`, 트리·메시지·작성/커밋 날짜 유지). 메시지에 `Co-Authored-By` 가 없으면 붙인다(core 설정 `submissions.co_author`). 같은 입력이면 같은 커밋이 나오므로, 같은 스레드가 커밋을 더해 다시 내면 앞의 커밋은 그대로이고 브랜치가 fast-forward 된다.
  - push 는 `agent/` 브랜치에만, 강제 없이 한다. GitHub 의 그 브랜치가 새 head 의 조상이 아니면(amend·rebase 등) 거부한다. `main` 은 대상이 될 수 없다.
- **PR**: 그 브랜치의 열린 PR 이 있으면 갱신한다(제목·본문을 넘겼을 때만 고친다). 없으면 만든다(제목이 없으면 첫 커밋의 제목). 민감 경로 표시와 승인은 기존 승인 흐름(ADR-0024, I-33)이 그대로 한다.
- **토큰**: GitHub App 의 새 용도 `submit`(`contents`·`pull_requests` 쓰기, `metadata` 읽기). 제출마다 발급하고 끝나면 폐기한다. 이미 설치에 있는 권한이라 새 비밀값이나 App 권한은 필요 없다.
- **이미지**: 공용 Dockerfile 이 `enikk-core` 에도 git 을 설치한다.
- **기능 플래그 (양쪽)**: runtime `ENIKK_RUNTIME_PULLS__ENABLED`(Helm `deploy/helm/runtime-claude` 의 `pulls.enabled`)는 도구를 주는지, core `ENIKK_CORE_SUBMISSIONS__ENABLED`(Helm `deploy/helm/core` 의 `submissions.enabled`)는 요청을 받는지 정한다. 둘을 따로 두는 이유: runtime 만 끄면 모델에게 도구가 보이지 않고, core 만 끄면 오래된 runtime 이나 다른 호출이 와도 거부된다. 둘 다 코드 기본값은 꺼짐, 차트 기본값은 켜짐이다.
- **스킬 `requires` 강제는 하지 않았다**: core 는 세션이 어떤 스킬을 불러왔는지 모른다(`load_skill` 은 runtime 이 Langfuse 에서 직접 읽는다). 강제하려면 "스킬을 불러온 세션만 `pr.write` 를 쓴다"를 core 가 확인할 근거(로드 기록을 core 에 남기거나 core 를 거쳐 로드)가 먼저 필요하다. 지금은 역할 정의의 `pull-requests` 가 권한이다. 3·4단계에서 다시 정한다.
- **첫 실제 제출 (2026-11-24)**: main `82ea7d4` 배포 뒤, 소유자가 시작한 Slack 스레드의 assistant 세션이 이 줄을 커밋해 이 도구로 처음 제출했다. 실제 push(`http.extraheader` 인증)·PR 생성·승인 채널 게시는 이 제출(PR #75)로 확인했다. 상태 카드는 올라오지 않았다(아래 수정).

### 3a 구현 세부 (2026-11-24)
3단계 중 동시 실행만 먼저 한다. 취소 버튼·대기 표시와 ADR-0012·0014·0027·0034 의 대체 표시는 3b 에서 한다.
- **칸**: `WorkerPolicy.slots`(1~4, 코드 기본 1). `TaskWorker.run_forever` 가 칸마다 임대 루프를 하나씩 동시에 돌린다. `stop()` 뒤 모든 칸이 지금 작업을 끝내고 멈춘다. runtime-claude settings `worker.slots` ← `ENIKK_RUNTIME_WORKER__SLOTS` ← Helm `deploy/helm/runtime-claude` 의 `worker.slots`(차트 기본 2). 같은 세션(스레드)의 작업이 두 칸에 동시에 가지 않는 것은 core 의 임대(I-29, `claim_next`)가 보장하므로 runtime 은 따로 막지 않는다.
- **작업 디렉터리**: 공유 역할은 `/work/<작업 ID>` 를 만들어 CLI 의 cwd 로 쓰고 끝나면 지운다(전에는 `/work` 하나를 매번 비웠다). 샌드박스 쓰기 경로도 `/work` 전체가 아니라 그 하위 디렉터리다. 격리 역할(개발 Job)은 전처럼 `workdir` 에서 돈다.
- **세션 재개**: SDK 는 cwd 에서 transcript 의 `project_key` 를 만든다. cwd 가 작업마다 달라지므로 `CoreSessionStore` 가 모든 키의 `project_key` 를 `workdir`(`/work`)에서 만든 고정값으로 바꾼다. core 에 저장되는 키는 3a 전과 같아서 기존 세션도 재개되고(ADR-0014·ADR-0034), 다른 작업 디렉터리에서도 재개된다.
- **임시 디렉터리**: SDK 가 재개할 세션을 쓰는 `tempfile.tempdir` 는 프로세스 전역이다. 실행 중인 작업이 하나라도 있으면 `scratch_dir` 를 가리키고, 마지막 실행이 끝날 때만 원래 값으로 돌린다.
- **자원**: requests memory 384Mi → 640Mi(실측: claude CLI 약 250MB, Python 약 100MB), limits 1536Mi 유지. `/work` emptyDir 64Mi → 128Mi(작업 파일은 `/data` worktree 에 있어 `/work` 는 거의 비어 있다). `/tmp` scratch 1Gi 는 그대로.
- **git 신원**: 기준 저장소 config 를 정규화할 때 `user.name = enikk`, `user.email = enikk@users.noreply.github.com` 을 넣는다. 전에는 worktree 의 `git commit` 이 "Author identity unknown" 으로 실패했다. core 가 제출 때 봇 작성자로 다시 쓰므로(2b) 값 자체는 의미가 없다.
- **되돌리기**: `worker.slots: 1`(한 번에 하나, 3a 전과 같은 직렬 처리).

### 3b 구현 세부 (2026-11-24)
- **취소(core)**: `POST /v1/tasks/{task_id}/cancel`(`connector` 역할, Slack connector 만). 그 작업이 속한 세션의 대기(QUEUED)·실행 중(LEASED) 작업을 모두 `CANCELLED` 로 바꾸고, 하나라도 바꿨으면 스레드에 안내 한 줄("취소했습니다.", outbox 종류 `cancelled`)을 남긴다. 이미 끝난 작업뿐이면 아무것도 바꾸지 않고 빈 `CancelResult.cancelled` 를 돌려준다. 저장소에는 `TaskRepository.active_in_session`(잠금)을 더했다. 스키마는 그대로다(outbox `kind` 는 문자열 열이라 마이그레이션이 없다).
- **실행 중단**: 취소된 작업은 임대가 없으므로 heartbeat·결과 보고가 conflict(409)로 거부된다. 결과 보고의 "이미 보고됨(멱등)" 판정에서 `CANCELLED` 를 뺐다. runtime 은 다음 heartbeat(임대 120초의 1/3, 약 40초 안)에서 conflict 를 받고 실행 태스크를 취소하며 결과를 버린다. SDK 는 그 취소에서 CLI 의 stdin 을 닫고 프로세스를 기다리며(필요하면 terminate·kill) 끝낸다. 가짜 CLI 로 프로세스가 실제로 사라지는 것을 테스트한다.
- **시작 알림**: core 가 Slack 대화의 작업을 처음 임대할 때(첫 시도만, 개발 Job 의 claim 포함) outbox 에 `started`("작업 중")를 넣는다. 이 글은 스레드 맥락(ADR-0027)과 날짜별 요약(ADR-0035)에서 뺀다. smoke·스케줄 대화에는 넣지 않는다.
- **[취소] 버튼(connector)**: `started` 를 받으면 스레드에 한 줄("작업 중" + [취소])을 올리고, 그 작업의 답·실패·취소 안내가 오면 지운다(`chat.delete`, 새 스코프 없음). 버튼 위치는 connector 메모리에만 있어 connector 가 재시작하면 남을 수 있다. 그때 누르면 "이미 끝난 작업" 으로 처리되고 지워진다. 누르는 사람은 승인 버튼과 같은 소유자 검사(I-01)를 거친다. 다른 사람에게는 ephemeral 로 거부한다. 소유자가 누르면 core 취소를 부르고 버튼 글을 지운다. 이미 끝났으면 소유자에게만 조용히 알린다. 메시지 텍스트("취소")로는 취소하지 않는다(소유자 결정).
- **대기 표시**: 요청 메시지에 접수 때 :hourglass_flowing_sand:, `started` 에 :eyes:, 끝나면 결과 표시(:white_check_mark:·:x:·취소는 :no_entry_sign:). 두 칸이 모두 차 있으면 모래시계가 남아 기다리는 중임을 보인다. 칸이 비어 있어도 `started` 가 전달될 때까지(보통 몇 초) 모래시계가 잠깐 보인다.
- **CLI 홈 격리**: 샌드박스 쓰기 경로에서 `scratch_dir` 전체를 빼고 그 실행의 홈만 연다. SDK 가 세션을 재개하려고 `scratch_dir` 아래에 만드는 디렉터리는 실행 직전에만 알 수 있으므로, 실행기(`enikk-claude-launch`)가 받은 `CLAUDE_CONFIG_DIR` 를 쓰기 경로에 더한다(ADR-0034).
- **되돌리기**: connector·core 이미지를 이전으로 되돌린다. 버튼과 모래시계만 끄는 설정은 두지 않았다.

### 4a 구현 세부 (2026-11-24)
4단계는 다섯 단위로 나눈다. 4a 실행 환경 → 4b `skill-developer`(Langfuse, `propose_prompt`) → 4c 시험 기간(위임과 스킬이 함께 있음) → 4d assistant 프롬프트의 위임 길을 스킬 직접 실행으로 → 4e `delegate`·개발 Job·`enikk-dev` 제거와 대체 표시. 4e 는 "스킬로 만든 코드 PR 2건이 머지되고 runtime 메모리 사고가 없을 때" 시작한다. 마이그레이션 0004·0005 의 열은 4e 에서 지우지 않고 나중에 따로 정한다(소유자 결정, 2026-11-24).
- **uv**: 공용 Dockerfile 이 `PACKAGE=enikk-runtime-claude` 일 때만 uv 바이너리를 `/usr/local/bin/uv` 에 둔다(샌드박스의 읽기 경로 `/usr` 안). ruff·mypy·pytest·import-linter 는 이미지에 넣지 않고, 세션이 스레드 worktree 에서 `uv sync --all-packages` 로 `uv.lock` 대로 설치한다. 그래서 검사 도구의 버전은 저장소와 같다. PyPI 접근은 샌드박스가 막지 않는다(Landlock 은 파일만 제한한다).
- **테스트 DB 는 두지 않는다 (§8 변경)**: 소유자 결정. 영속성 통합 테스트는 runtime 에서 `--ignore=src/libs/adapters/persistence-sql/tests` 로 빼고 CI 에서만 돌린다. CNPG 테스트 DB 는 모델에게 운영 DB 서버의 접속 정보를 주고 하나뿐인 DB 인스턴스에 부하를 주며, 픽스처의 루프백 제한(ADR-0030)을 풀어야 한다. 이미지 안의 Postgres 는 메모리를 더 쓴다. 지금 변경의 크기에 비해 둘 다 불필요하다고 판단했다. 픽스처와 "건너뛰지 않고 오류"(ADR-0012 §1)는 그대로이고, 빼는 것은 명령에서만 한다. 방법은 `docs/development/testing.md`.
- **uv 캐시**: 세션은 `UV_CACHE_DIR` 를 그 스레드의 `/data/tmp/<세션>/` 아래로 둔다. 기본 위치(실행의 홈)는 `/tmp` emptyDir(1Gi) 아래라 넘치면 Pod 가 축출된다. 스레드 사이에 캐시를 함께 쓰지 않는다(한 스레드가 심은 패키지가 다른 스레드에서 실행되는 길을 막는다). 대가로 스레드마다 PVC 를 더 쓴다. 7일 정리와 남은 공간 확인(1단계)이 한도다.
- **한도**: assistant 의 `max_turns` 20 → 100, `timeout_seconds` 600 → 3600(계약의 상한). 4b 부터 같은 세션이 개발 작업을 하기 때문이다. 대화 작업도 같은 한도를 받는다. 한 작업이 칸 하나를 최대 한 시간 차지할 수 있으며, 소유자의 [취소](3b)가 대책이다.
- **메모리**: limits 1536Mi 유지(소유자 결정). 두 칸에서 CLI 둘과 mypy·pytest 가 겹치면 넘칠 수 있다. 넘치면 두 작업이 함께 끝나므로 4c 에서 peak 를 본다.
- **머지된 세션 브랜치 따라가기 (4c 에서 보완)**: PR 이 squash 로 머지되고 `agent/` 브랜치가 지워지면, 같은 스레드의 worktree 는 옛 main 위에 이미 머지된 커밋을 가진 채 남는다. 그대로 다시 제출하면 그 커밋이 또 간다. 그래서 세션 준비(1단계 7번) 때 아래를 모두 만족하면 세션 브랜치를 새 main 으로 옮긴다(`reset --hard`): worktree 가 자기 브랜치에 있고, 커밋하지 않은 변경·추적하지 않는 파일이 없으며(무시 파일 제외), 브랜치가 main 이후 바꾼 파일이 새 main 에서 모두 같은 내용이다. 커밋이 없는 깨끗한 세션도 새 main 을 따라간다. 하나라도 어긋나면 손대지 않는다. git 은 worktree 의 `.git` 파일이 아니라 기준 저장소의 `worktrees/<세션>` 으로 가리켜 실행한다(세션이 `.git` 파일을 바꿀 수 있다).
- **되돌리기**: 이전 이미지로 되돌리면(ADR-0023) uv 가 빠진다. 한도는 `agents/assistant.yaml` 을 되돌린다.

### 4e-1 구현 세부 (2026-11-24)
4e 는 4e-1(준비)과 4e-2(제거)로 나눈다. 4e-1 은 4e-2 의 삭제가 다른 것을 깨거나 클러스터에 찌꺼기를 남기지 않게 미리 배포해 두는 변경이다.
- **이미지 빌드에서 dev-agent 제외**: `.github/workflows/images.yml` 의 `verify`·`build`·`publish` matrix 에서 `dev-agent` 를 빼고 Dockerfile 을 `deploy/docker/python-app.Dockerfile` 로 고정했다. 4e-2 에서 dev-agent 패키지와 Dockerfile 을 지우면 그 빌드가 실패하고, `publish` 가 `needs: build` 라서 모든 앱의 이미지 게시가 멈추기 때문이다. 이 뒤로 dev-agent 이미지는 만들어지지 않고, 운영 중인 dev-agent 는 마지막 이미지로 4e-2 까지 돈다.
- **Argo finalizer**: `deploy/argocd/apps/enikk-dev-agent.yaml` 에 `resources-finalizer.argocd.argoproj.io` 를 달았다. 없으면 root 앱(`prune: true`)이 이 Application 을 지워도 `enikk-dev` 네임스페이스, ServiceAccount, core 의 Role, Job 템플릿 ConfigMap 이 클러스터에 남는다. finalizer 가 먼저 배포되어 있어야 4e-2 의 삭제가 연쇄된다. 이 단위에서는 Application 을 지우지 않는다.
- **개발 역할로 낸 이유**: 새 길(`submit_pull_request`, 2b)은 `.github/workflows/` 를 바꿀 수 없다. 그래서 workflow 를 바꾸는 이 단위는 workflow 권한이 있는 개발 역할로 마지막으로 냈다(소유자 지시).
- **4e-2 예고**: dev-agent 패키지·Dockerfile·Helm 차트·Argo 앱, core 의 디스패처·isolated API·개발 토큰, `delegate`, `agents/developer.yaml` 을 지우고, 위 표의 ADR·불변식에 대체 표시를 단다. 새 길로 낸다.

### 4e-2 구현 세부 (2026-11-24)
4d(assistant 프롬프트 v9, `skill-developer` v2)가 production 이 된 뒤, 스레드 세션이 `skill-developer` 로 만든 PR 이다.
- **위임**: `delegate` 플랫폼 도구, runtime 의 `Delegator` 포트, core client 의 `delegate_task`, `POST /v1/tasks/{task}/delegate` 와 그 use case, 계약 `DelegateRequest`·`DelegationAccepted`, 역할 정의의 `delegates` 키를 없앴다. `delegates` 가 있는 역할 정의는 이제 거부된다.
- **개발 Job**: dev-agent 앱, `agents/developer.yaml`, core 의 Job 디스패처·`POST /v1/tasks/{task}/claim`·`POST /v1/github/token`(`DevTokenIssuer`)·일일 한도·`dev_agent` 설정, `dev-agent` 서비스 역할, Kubernetes Job launcher, `enikk-ops smoke dev` 를 없앴다. Argo CD Application·Pod 로그 읽기는 남기고 공통 인증은 `enikk_k8s_jobs.kube` 로 옮겼다. 패키지 이름 `enikk-k8s-jobs` 는 그대로 둔다.
- **배포**: `deploy/helm/dev-agent`, Argo 앱 `enikk-dev-agent`(4e-1 의 finalizer 로 `enikk-dev` 네임스페이스와 그 안의 Role·ServiceAccount·ConfigMap 도 지워진다), `deploy/docker/dev-agent.Dockerfile`, Image Updater 항목, core values 의 `devAgent`·역할 바인딩·감시 앱 목록의 `enikk-dev-agent`, 민감 경로 목록의 차트 줄을 없앴다.
- **불변식**: I-11·I-12(위임 없음, 예전 위임 세션의 글만 남음), I-17(개발 역할 없음), I-28(core 의 `enikk-dev` 권한 없음), I-32(설치 토큰도 core 밖으로 나가지 않음), I-16(위임 대상이라는 권한 없음), I-20(민감 경로 설명에서 개발 역할 정의를 역할 정의로)을 고쳤다.
- **진단**: `service_logs` 의 `dev-job` 과 `LogsQuery.task_id`, core 의 `diagnostics.dev_namespace` 를 없앴다.
- **남긴 것**: 도메인·SQL 의 isolated 작업(`Execution.ISOLATED`, `queue.next_isolated`·`claim_isolated`)과 `dispatches`, 세션의 `reply_to` 는 마이그레이션 0004·0005 의 열과 함께 남긴다(소유자 결정: DB 정리는 나중에). 이미 있는 위임 세션의 PR 상태 카드, 위임 답의 상태 줄, 스레드 맥락은 그대로 동작한다. GitHub App 의 `develop` 용도는 core 의 되돌리기 PR(ADR-0023)이 계속 쓴다.
- **남은 isolated 작업**: 배포 시점에 대기 중인 developer 작업이 있으면 실행할 곳이 없어 남는다. 4e-2 전에 열린 위임이 없는지 확인하고 배포한다.
- **되돌리기**: 이전 이미지와 매니페스트로 되돌린다(ADR-0023). `enikk-dev` 네임스페이스는 Argo 앱을 되살리면 다시 만들어진다.

## 근거
- 맥락 손실과 취소 불가는 "다른 실행 환경에 위임한다"는 구조에서 온다. 같은 세션이 대화와 작업을 함께 하면 둘 다 사라진다.
- 권한을 역할 대신 **동작**에 묶으면(6) 스킬은 지시만 담으면 된다. 되돌릴 수 없는 동작은 모두 core 를 거치거나 소유자 승인을 받으므로, 프롬프트 인젝션이 닿는 범위가 넓어져도 결과가 GitHub 이나 운영에 바로 닿지 않는다.
- 공유 볼륨의 가장 큰 위험(`.git` 의 hooks·config 를 통한 권한 상승)은 "토큰 없는 쪽이 쓴 `.git` 에서 토큰 있는 쪽이 git 을 실행할 때" 생긴다. core 가 볼륨의 `.git` 에서 git 을 실행하지 않고 patch·bundle 을 데이터로만 받으면 그 길이 없다. runtime 자신의 git 도 샌드박스 안에서 돌린다.
- 볼륨에는 다시 만들 수 있는 것만 둔다. 원본은 GitHub(코드), core DB(작업·세션), Langfuse(프롬프트·스킬), 볼트(기억)다.

### 외부 사실 (확인 2026-11-23)
- git worktree 는 `HEAD`, `index` 같은 worktree 별 파일을 빼고 저장소를 공유하며, 저장소 `config` 도 기본으로 함께 쓴다. 다른 worktree 가 checkout 한 브랜치로는 `worktree add` 가 거부한다. https://git-scm.com/docs/git-worktree
- Kubernetes `ReadWriteOnce` 는 한 노드에서의 읽기·쓰기 마운트를 허용하며, 같은 노드의 여러 Pod 가 함께 쓸 수 있다(한 Pod 만 허용하려면 `ReadWriteOncePod`). https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes

## 결과
- 상주 자원이 늘어난다(8). 노드 메모리 여유는 확인하지 못했다.
- 볼륨이 망가지거나 차면 모든 스레드의 작업 공간이 함께 멈춘다. 다시 만들 수 있는 것만 두고, 준비 실패는 작업을 막지 않는다.
- 같은 스레드의 파일이 다음 요청까지 남는다(의도). 다른 스레드로는 넘어가지 않는다(Landlock).
- I-25 를 고친다: 런타임의 공유 볼륨은 다시 만들 수 있는 캐시와 작업 공간일 뿐이다(1단계).

### 위험과 대책
| 위험 | 대책 |
|---|---|
| 인젝션이 닿는 범위가 넓어진다(한 세션이 대화·저장소·웹을 함께 다룬다) | 되돌릴 수 없는 동작은 core 경유(6b)·소유자 승인(6c). 신뢰하지 않는 자료는 `/data/tmp/<스레드>` 에 둔다 |
| 세션이 기준 저장소의 config·hooks·ref 를 바꾼다 | 매 준비 때 config·hooks 를 다시 쓴다. 커밋은 `state/` 의 기록을 믿는다. runtime 의 git 은 샌드박스 안. core 는 볼륨에서 git 을 실행하지 않는다 |
| 긴 개발 작업이 대화를 막는다 | 스레드별 처리, 동시 한도 2(3a), 대기 표시와 소유자의 [취소](3b) |
| 볼륨 손상·용량 부족 | 다시 만들 수 있는 것만 둔다. 7일 정리, 남은 공간 확인, 스냅샷 2개만 |
| runtime 불안정(무거운 명령) | 명령별 시간·메모리 한도, 실패하면 그 세션만 끝낸다(2~3단계) |
| 동시 처리 2 에서 한 스레드가 다른 스레드의 `repo.git/worktrees/<다른 스레드>` 를 건드림 | **3a 에서 막지 않았다(남은 위험)**. 세션의 쓰기 경로에 `repo.git` 전체가 있어 다른 스레드의 worktree 메타데이터(`HEAD`·`index` 등)를 바꿀 수 있다. 결과는 그 스레드의 작업 공간 손상이고 GitHub·운영에는 core 의 bundle 확인(2b)을 거쳐야 닿는다. 3b 에서 쓰기 경로를 `objects`·`refs`·`logs`·자기 `worktrees/<이름>` 으로 좁혀 보았지만, git 이 ref 를 바꿀 때 `repo.git/packed-refs.lock` 을 `repo.git` 바로 아래에 만들어야 해서 Landlock 의 경로 규칙으로는 열 수 없다. 그래서 **남은 위험으로 둔다**. 4단계에서 스레드마다 따로 clone 하거나 git 을 core 경유로 바꿀 때 다시 본다 |
| 같은 Pod 에서 동시에 도는 두 작업이 서로의 CLI 홈(`scratch_dir` 아래)을 쓸 수 있다 | **3b 에서 막았다**. 샌드박스는 그 실행의 홈과 SDK 의 재개 디렉터리(`CLAUDE_CONFIG_DIR`)만 쓰기로 연다 |

### 바뀌는 ADR·불변식 (대체 표시를 다는 단계)
| 대상 | 무엇이 바뀌나 | 단계 |
|---|---|---|
| ADR-0010 §3, I-25 | 런타임은 상태를 갖지 않는다 → 공유 볼륨(다시 만들 수 있는 캐시·작업 공간)은 예외 | **1 (이번)** |
| ADR-0033 §2.2·§2.5 | 쓰기 허용 경로에 그 스레드의 볼륨 경로 추가. 작업 폴더는 지우되 스레드 worktree 는 남긴다 | **1 (이번)** |
| ADR-0019, ADR-0021 §3·§7, I-32, ADR-0007(I-17) | 개발 Pod 가 받는 GitHub 토큰 → core 만 토큰을 갖고 push·PR 은 core 도구 | **2b (표시함)**·**4e-2 (표시함)** |
| ADR-0032, I-36 | 스킬 config(`requires`)와 승격 경고 | **2a (표시함)** |
| ADR-0005, I-16 | Langfuse 의 스킬 config 에 `requires`(core 경유 동작의 선언)를 둔다 | **2a (표시함)** |
| ADR-0005, I-10, I-14, I-16 | 역할 정의 파일 → 기본 프롬프트 + 스킬 + 동작 권한 | 2b·4 |
| ADR-0012(I-29), ADR-0014, ADR-0027, ADR-0034 | 세션 = 스레드, 스레드 사이 동시 2, 취소 | **3 (표시함)** |
| ADR-0007, ADR-0021, ADR-0022, ADR-0025, ADR-0030, ADR-0031, I-11, I-12, I-17, I-28 | 위임·개발 Job·`enikk-dev` 제거, 영속성 테스트는 CI 에서만(4a), core 의 Job 권한 제거. 역할 개념 자체는 5 | **4e-2 (표시함)** |
| ADR-0035, I-38 | 요약 역할 → 도구 없는 요약 스킬 세션, 스케줄러 정리 | 5 |
| ADR-0006, I-13 | 그대로(스킬도 Langfuse 가 원본) | — |

## 이행 순서
각 단계는 따로 배포할 수 있고, 4단계 전까지는 이전 방식(위임, 개발 Job)이 그대로 동작한다.
1. 공유 볼륨·기준 저장소·worktree. runtime 이 저장소 원문을 읽는다. **(이 PR)**
2. 2a: 스킬 config 스키마, 스킬 후보의 승격 경고, `load_skill` 과 스킬 목록. **(구현됨)** 2b: core 의 PR 제출 도구(bundle → core 전용 clone → push·PR). **(구현됨)**
3. 스레드 세션화(스레드 직렬, 스레드 사이 동시 2)와 취소, runtime 메모리 한도 조정. 3a: 동시 실행 칸·작업 디렉터리·자원·git 신원 **(구현됨)**. 3b: 취소·대기 표시·CLI 홈 격리 **(구현됨)**.
4. developer 스킬로 전환, `delegate`·개발 Job·`enikk-dev` 제거, runtime 이미지에 uv, 관련 ADR·불변식 대체 표시. 4a: runtime 의 uv·assistant 한도 **(구현됨)**. 4b~4d: 스킬·시험·프롬프트 전환 **(완료)**. 4e-1·4e-2: 이미지 빌드 정리·제거 **(구현됨)**.
5. conversation-summary 스킬로 전환, 스케줄러 정리.

### 되돌리기
- 1~3단계: 기능을 끈다. 1단계는 `deploy/helm/runtime-claude` 의 `workspace.enabled: false`(PVC 와 마운트가 빠지고 runtime 은 볼륨 없이 돈다). 2a 는 같은 차트의 `skills.enabled: false`(스킬 목록과 `load_skill` 이 빠진다). 이미 승격한 스킬 프롬프트는 Langfuse 에 남지만 아무 세션도 읽지 않는다. 2b 는 같은 차트의 `pulls.enabled: false`(도구가 빠진다)와 `deploy/helm/core` 의 `submissions.enabled: false`(core 가 제출을 거부한다). 이미 연 PR 은 GitHub 에 남으므로 소유자가 거절하거나 닫는다. 3a 는 같은 차트의 `worker.slots: 1`.
- 4단계부터: 이전 이미지와 이전 매니페스트로 되돌린다(ADR-0023 의 되돌리기).

## 재검토 조건
- 노드가 여러 대가 되면 → RWX 스토리지나 Pod 배치 고정을 정한다.
- 상주 자원이 노드 메모리를 압박하면 → 동시 한도, 무거운 작업만 Job 으로 보내는 선택을 다시 본다.
- Claude Agent SDK 의 세션 재개가 스레드 단위 세션에 맞지 않으면 → 3단계 설계를 다시 본다.
