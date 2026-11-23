# ADR-0037: 공유 볼륨, 역할을 스킬로 재구성, 권한 동작을 core 도구로 분리

- 상태: 제안
- 날짜: 2026-11-23
- 관련: ADR-0005, ADR-0007, ADR-0010, ADR-0013, ADR-0019, ADR-0021, ADR-0022, ADR-0028, ADR-0029, ADR-0031, ADR-0033, ADR-0035, I-14, I-17, I-25, I-28, I-32, I-37

> 제안 상태다. 구현 근거로 쓰지 않는다. 기존 ADR 의 상태는 바꾸지 않았다. 결정되면 "결정 뒤 바꿀 것"의 목록대로 대체 표시를 단다.
> 표시: **[확인]** 은 저장소의 코드·매니페스트로 확인한 것(main 6ebdbd8 기준), **[추정]** 은 확인하지 못한 것이다.

## 배경

### 문제
- 대화 역할(assistant)은 enikk 저장소의 코드와 PR 원문을 정확히 볼 수 없다.
  - 운영 도구는 진단(`recent_tasks`, `task_detail`, `deployments`, `service_logs`, `pull_requests`), 기억(`remember`, `update_memory`, `forget_memory`, `recall`), 프롬프트(`read_prompt`, `propose_prompt`), `summarize_day`, `delegate` 다. **[확인]** `enikk_engine_claude/platform_tools.py`
  - `pull_requests` 는 인자가 없고 **열린 PR 만** 돌려준다. ADR-0029 의 표에는 `pull_requests(state?)` 로 적혀 있지만 코드에는 `state` 인자가 없다. **[확인]**
  - WebFetch 는 가져온 내용을 작은 모델로 요약해 돌려준다(ADR-0033). 그래서 원문을 정확히 옮기거나 줄 단위로 근거를 댈 수 없다.
- 결과: "구현된 시스템을 리뷰해 달라"는 요청에 코드 근거로 답하지 못했다. 머지된 PR #68 의 `conversation-summary` 프롬프트 본문을 옮기지 못했다. 프롬프트와 실제 동작이 어긋나도 알아채지 못한다.
  - 예: PR #69(ADR-0036) 이후 프롬프트 승격 요청은 승인 채널로 가는데, assistant 프롬프트는 DM 이라고 적고 있다(Langfuse, 소유자 기록). 코드 안에도 같은 어긋남이 있다. `propose_prompt` 도구 설명은 아직 "in a Slack DM with the diff" 다. **[확인]**
- 참고: assistant 는 이미 Bash 를 가지며 runtime 이미지에 Python 이 있다. 그래서 명령으로 `raw.githubusercontent.com` 이나 GitHub API 에서 원문을 받아 볼 수는 있을 것이다. **[추정]** runtime 이미지(`python-app.Dockerfile`)에는 git 과 curl 이 없고, 비인증 GitHub API 는 한도가 낮다. 실제로 동작하는지는 확인하지 않았다. 그리고 프롬프트가 이 방법을 알려 주지 않는다.

### 제약
- 클러스터는 클라우드가 아니라 자원이 한정된 온프레미스다. 소유자는 늘 떠 있는 프로세스(예: git-sync 사이드카)를 피하고 싶어 한다.
- 소유자 방향: 역할마다 격리하지 말고 공유 볼륨을 두어 함께 쓴다. 동시 쓰기 같은 문제는 정책과 코드로 막는다. 저장소 작업은 기본으로 git worktree 로 한다. 저장소 말고 다른 용도는 아직 정하지 않았다.
- 소유자 관찰: 지금 "역할"은 지시문만 보면 사실상 스킬(필요할 때 불러 쓰는 절차와 지식)에 가깝다. 역할을 실제로 가르는 것은 **권한**(누가 GitHub 쓰기 토큰을 갖는가, 어떤 도구를 쓰는가)과 **실행 환경**(작업마다 뜨는 개발 Pod 인가, 상주 runtime Pod 인가)이다.

### 지금 구조 [확인]

**역할** (`agents/*.yaml`)

| 역할 | 실행 | 기본 도구 | 플랫폼 도구 | 위임 | 한도 |
|---|---|---|---|---|---|
| assistant | `shared` (runtime-claude) | ADR-0033 의 16개 전부 | 기억·진단(기본) + `prompt-candidates`(`read_prompt`, `propose_prompt`) + `conversation-digest`(`summarize_day`) | developer | 20턴, 600초 |
| developer | `isolated` (작업마다 Job) | 16개 전부 | 기억·진단(기본) | 없음 | 100턴, 3600초 |
| summarizer | `shared` | 없음 | 없음(`platform_defaults: false`) | 없음 | 2턴, 600초 |

- 역할 정의의 차이는 실행 위치(`execution`), 플랫폼 도구, 위임, 한도, 프롬프트 이름뿐이다. 기본 도구 목록은 assistant 와 developer 가 같다.
- 계약은 `Skill` 도구를 허용하지 않는다(`BUILTIN_TOOLS` 밖, ADR-0033 §1).

**토큰의 위치**
- GitHub App 개인 키: core 전용 Secret `enikk-core-credentials` 에만 있다(I-32). core 는 용도별 설치 토큰(`develop`, `approve`, `watch`, `memory`, `setup`)을 만든다. `enikk_github/permissions.py`
- 개발용 GitHub 토큰(`develop`: `contents`·`pull_requests` 쓰기, `checks`·`statuses` 읽기, 저장소 하나, 1시간): core 가 작업의 활성 임대를 확인한 뒤 개발 Pod 에 준다. Pod 는 gh 설정 파일(`$HOME=/tmp/home`, 0600)에만 쓰고 git 은 `gh auth git-credential` 로 읽는다. **push 와 PR 생성은 모델이 Bash 로 직접 한다.** 그래서 모델은 이 토큰을 읽을 수 있다(ADR-0021 이 감수한 위험).
- Claude 토큰, Langfuse 키: core 가 runtime 과 개발 Pod 에 준다(`roleBindings[].credentials`). Claude 토큰은 파이프로 CLI 에만 가고, 명령은 Landlock 안에서 돈다(ADR-0033, I-37).
- core 토큰: 각 Pod 의 projected SA 토큰(audience `enikk-core`). Landlock 으로 명령이 읽지 못한다.

**개발 Job Pod 의 생애** (ADR-0021, `deploy/helm/dev-agent`)
- core 가 `enikk-dev` 네임스페이스에 ConfigMap 템플릿으로 Job 을 만든다(동시 최대 10, 하루 상한 없음).
- Pod: 비루트(UID/GID 10001, fsGroup 10001), 루트 FS 읽기 전용, `enikk-dev` 는 PSA `restricted`. 볼륨은 `workspace` emptyDir 4Gi, `/tmp` emptyDir 1Gi, projected core 토큰뿐이다.
- 요청 512Mi / 한도 3Gi, `activeDeadlineSeconds` 4500, 끝난 뒤 `ttlSecondsAfterFinished` 3600 에 지워진다.
- 매번 `git clone --depth 100` 으로 저장소를 받고 `uv sync` 로 환경을 만든다. 시작이 수십 초 늦다.
- 이미지 `dev-agent.Dockerfile`: Python + git, gh, uv, PostgreSQL 18 서버(테스트용, ADR-0030).

**runtime-claude** (`deploy/helm/runtime-claude`)
- Deployment 복제본 1개, 상주. 요청 100m/384Mi, 한도 1536Mi. **작업을 한 번에 하나만 임대한다**(`enikk_runtime/worker.py`).
- 볼륨: `/work` emptyDir 64Mi(작업마다 비움, ADR-0033 §2.5), `/tmp` scratch emptyDir 1Gi. 네임스페이스 `enikk`(PSA 라벨 없음).

**볼트 접근** (ADR-0028)
- 볼트 저장소 `vault.hyeongseok-kim` 은 core 가 GitHub API 로 1분마다 읽어 DB `vault_notes` 에 색인한다. 쓰기는 core 가 기억 전용 토큰으로 contents API 를 써서 커밋한다.
- 역할은 볼트 파일을 직접 보지 않는다. 플랫폼 도구(`recall` 등)와 작업 배정의 기억 목록으로만 본다.

**상주 자원(요청 기준)**: core 50m/128Mi, connector-slack 20m/96Mi, runtime-claude 100m/384Mi, Postgres(CNPG) 100m/256Mi. 개발 Job 은 일이 있을 때만 뜬다(하나에 250m/512Mi).

**스토리지와 노드**
- PVC 를 쓰는 것은 CNPG 클러스터(2Gi) 하나이며 `storageClass` 를 지정하지 않아 클러스터 기본값을 쓴다. **[확인]**
- 대상 클러스터는 kube context `docker-desktop` 이고, ADR-0011·ADR-0013 은 "단일 노드"라고 적었다. **[확인: 문서 기록]** 지금도 노드가 하나인지, 기본 스토리지 클래스가 Docker Desktop 의 `hostpath`(노드 디렉터리를 쓰는 RWO 볼륨)인지는 확인하지 않았다. **[추정]**
- PVC 는 네임스페이스에 속한다. 그래서 `enikk`(runtime)와 `enikk-dev`(개발 Job)의 Pod 가 PVC 하나를 그대로 함께 마운트할 수 없다. `enikk-dev` 는 PSA `restricted` 라 hostPath 볼륨도 쓸 수 없다. **[확인: 매니페스트, Kubernetes 규칙]**

## 검토한 대안

비용은 늘 떠 있는 자원 기준이다. "바꿀 ADR·불변식"은 그 안을 채택할 때 대체·수정해야 하는 것이다.

### A. 지금 구조 유지 + 대화 역할에 저장소 읽기 수단만 추가
세 가지 방법을 함께 또는 따로 쓸 수 있다.
- A1. **이미지에 저장소 사본을 굽는다**: runtime 이미지에 빌드 시점의 소스를 넣는다(이미 `agents/`, `config/` 는 들어 있다). 이미지와 같은 커밋의 코드를 정확히 본다. 그러나 머지 뒤 배포 전의 코드나 열린 PR 의 diff 는 볼 수 없고 이미지가 커진다.
- A2. **필요할 때만 받아 온다**: 플랫폼 도구 `read_repo(path, ref)`·`pr_diff(number)` 를 core 가 `watch` 토큰으로 GitHub API 를 불러 원문 그대로 돌려준다. 또는 runtime 이미지에 git 을 넣고 작업 scratch 에 얕은 clone 을 받는다(공개 저장소라 토큰이 필요 없다).
- A3. **`pull_requests` 를 넓힌다**: `state`(open/closed/merged)와 번호 조회, 본문·diff 포함 여부를 더한다. ADR-0029 표와 코드의 차이도 함께 맞춘다.
- 상주 비용: 0(A1 은 이미지 크기만, A2 의 clone 은 작업 동안 scratch 만).
- 보안 경계: 그대로. 읽기 전용이고 새 쓰기 권한이 없다. 원문은 신뢰하지 않는 입력으로 다룬다(I-37 의 웹 내용과 같은 원칙).
- 구현 규모: 작다(core API 2~3개, 플랫폼 도구, 프롬프트 한 줄).
- 바꿀 ADR·불변식: 없음. ADR-0029 에 도구를 더하는 수정 정도.
- 해결하지 못하는 것: 개발 역할이 토큰을 읽을 수 있는 위험(ADR-0021), 작업마다 clone 하는 지연, "역할 = 스킬" 정리.

### B. 공유 볼륨 + 지금 역할 구조 유지
- PVC 하나에 저장소 기준 사본(`repo/`)을 두고, 개발 Job 과 runtime 이 같은 볼륨을 마운트한다. 개발 작업은 `git worktree add` 로 자기 작업 폴더를 만든다.
- 걸림돌 **[확인]**: PVC 는 네임스페이스 단위다. runtime(`enikk`)과 개발 Job(`enikk-dev`)이 함께 쓰려면 다음 중 하나가 필요하다.
  - 개발 Job 을 `enikk` 네임스페이스로 옮긴다 → ADR-0021 §1 의 네임스페이스 격리(다른 SA 로 Pod 를 띄울 수 없다는 보장)를 잃는다.
  - 같은 노드 디렉터리를 가리키는 PV 두 개를 정적으로 만든다 → 동적 프로비저닝을 벗어난다. 노드 경로를 직접 관리해야 한다.
  - NFS 같은 RWX 스토리지를 둔다 → NFS 서버라는 상주 프로세스가 생긴다(제약에 어긋난다).
- 상주 비용: 볼륨 디스크만. 프로세스는 늘지 않는다(정적 PV 방식).
- 보안 경계: **나빠진다.** 토큰을 가진 개발 Pod 와 토큰이 없는 runtime 이 같은 `.git` 을 쓴다. 아래 위험 1 이 그대로 생긴다. 개발 Job 사이에도 서로의 worktree 와 `.venv` 가 보인다.
- 구현 규모: 중간(볼륨·PV, 시작 시 정리, 잠금, 개발 Pod 의 clone 을 worktree 로, runtime 의 Landlock 쓰기 경로 추가).
- 바꿀 ADR·불변식: ADR-0021(§1 또는 §2 의 emptyDir, "작업 사이에 상태가 남지 않는다"), ADR-0033 §2.2(쓰기 허용 경로), I-28(네임스페이스를 옮기면 core 권한 범위).
- 얻는 것: clone 지연이 준다. 대화 역할이 최신 저장소를 바로 읽는다.

### C. 공유 볼륨 + 역할을 스킬로 재구성 + 권한 동작은 core 도구
- 에이전트 실행 환경을 하나로 줄인다. 개발·요약·대화는 스킬(절차와 지식)이 되고, 실행 환경과 권한은 "어떤 작업인가"로 정한다.
- push, PR 생성처럼 권한이 필요한 동작은 core 가 실행하는 플랫폼 도구(`publish_branch`, `open_pull_request` 등)로 뺀다. GitHub 쓰기 토큰은 core 만 갖는다. 모델과 그 명령에는 GitHub 자격증명이 전혀 없다.
- 상주 비용: **늘 수 있다.** 지금 개발 작업은 일이 있을 때만 Pod 를 띄우므로 쉴 때 비용이 0 이다. 개발 작업(100턴, 1시간, 검사에 Postgres, 한도 3Gi)을 상주 runtime 에서 돌리면 runtime 의 메모리 한도를 개발 작업 기준으로 올려야 하고, 그 이미지에 git·gh·uv·Postgres 가 들어간다. 줄어드는 것은 Pod 시작과 clone 의 **지연**이다. "Pod 를 띄우는 비용이 준다"는 논의의 기대는 지연에 대해서는 맞고 자원에 대해서는 반대일 수 있다.
- 동시성: runtime 은 작업을 한 번에 하나만 처리한다 **[확인]**. 개발 작업이 1시간 동안 runtime 을 잡으면 그동안 대화가 멈춘다. 그래서 C 는 runtime 의 동시 처리(작업별 폴더와 Landlock 도메인, ADR-0033 재검토 조건)나 복제본 여러 개가 필요하다. 복제본이 여럿이면 같은 볼륨을 쓰기 위해 같은 노드에 묶어야 한다.
- 보안 경계: **좋아진다.** 모델이 읽을 수 있는 GitHub 토큰이 사라진다(ADR-0021 이 재검토 조건으로 남긴 "push·PR 생성을 모델 밖의 플랫폼 도구로"). 대신 잃는 것이 있다(위험 7).
- 구현 규모: 크다(core 의 push·PR 도구, runtime 동시성, 역할 정의 계약의 변화, 이미지 통합, 프롬프트를 스킬로 옮기기, 개발 Job 경로 정리).
- 바꿀 ADR·불변식: ADR-0005(역할 정의 형식), ADR-0021(작업마다 Job), ADR-0022(위임의 의미), ADR-0031(역할의 정의), ADR-0033(도구 목록에 `Skill`, 쓰기 경로), ADR-0006(스킬 본문을 어디에 두는가, 아래 열린 질문), I-11·I-12(스레드-역할 묶임, 위임 입력), I-14, I-17, I-32.

### D. 볼륨 없이 권한 동작만 core 도구로 (C 의 앞부분만)
- 볼륨을 만들지 않는다. 작업마다 emptyDir 에 clone 하는 지금 방식을 유지하되, push·PR 생성을 core 도구로 뺀다. 개발 Pod 에는 GitHub 토큰을 주지 않는다(공개 저장소의 clone 은 토큰이 필요 없다. 비공개 대상 저장소는 core 가 읽기 전용 토큰으로 clone 만 해 주거나 tarball 을 건넨다).
- 상주 비용: 0. 보안 경계: C 와 같은 토큰 이득, 개발 작업의 Pod 격리도 유지한다.
- 구현 규모: 중간(core 의 push·PR 도구, 개발 Pod 의 자격증명 제거). 바꿀 ADR: ADR-0019·ADR-0021 의 토큰 전달 부분, I-32.
- 해결하지 못하는 것: clone 지연, 실행 환경 둘.

### 비교 요약

| | 상주 비용 | 보안 경계 | 구현 규모 | 대화 역할의 저장소 읽기 |
|---|---|---|---|---|
| A | 0 | 그대로 | 작음 | 해결 |
| B | 디스크만 | 나빠짐(위험 1) | 중간 | 해결 |
| C | 늘 수 있음(runtime 한도·이미지) | 토큰 면에서 좋아짐, 격리는 잃음 | 큼 | 해결 |
| D | 0 | 토큰 면에서 좋아짐, 격리 유지 | 중간 | A 와 함께 써야 해결 |

## 공유 볼륨의 위험과 대책 (B, C 에 해당)

### 1. `.git` 공유로 인한 권한 상승
- worktree 는 저장소의 `config`, `refs`, hooks 디렉터리를 함께 쓴다(per-worktree 는 `HEAD`, `index` 등뿐). 쓰기 권한이 있는 쪽이 `.git/hooks/*` 나 `core.fsmonitor`, `core.sshCommand`, `core.hooksPath`, `credential.helper`, `diff.external`, `filter.*` 같은 설정을 심으면, **토큰을 가진 쪽이 git 을 실행할 때 그 코드가 그쪽 권한으로 실행된다.** worktree 안의 `.gitattributes` 가 가리키는 필터 이름도 같은 경로가 된다.
- 대책
  - 토큰을 가진 프로세스는 볼륨의 `.git` 에서 git 을 돌리지 않는다. 꼭 돌려야 하면 `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<자기 고정 파일>`, `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.sshCommand= -c credential.helper=` 등으로 저장소 설정을 무력화한다. 다만 저장소 로컬 config 는 이 방법으로 다 막을 수 없다(모든 위험 키를 열거해야 한다). 그래서 첫 번째 원칙이 우선이다.
  - `.git`(공통 디렉터리)은 정리 프로세스만 쓸 수 있게 하고, 작업 쪽은 worktree 폴더만 쓴다. 단 commit 은 `.git/objects`·`refs` 쓰기가 필요하므로 이 분리는 완전하지 않다. 작업마다 별도 클론(`git clone --reference` 또는 `--shared` 로 객체만 공유)을 쓰면 config·hooks 를 작업별로 나눌 수 있다.
  - 토큰과 자격 증명 파일(gh 설정 등)은 볼륨에 두지 않는다. 지금처럼 Pod 의 emptyDir 에 둔다.
- **C 에서 어떻게 사라지는가**: 볼륨을 쓰는 모델 쪽에는 토큰이 없으므로 심을 이유가 있어도 훔칠 것이 없다. 단, core 가 push 할 때 **볼륨의 `.git` 에서 git 을 실행하면 같은 위험이 core 로 옮겨 간다**(core 는 App 개인 키를 가진 더 큰 표적이다). 그래서 C 의 core 도구는 볼륨의 내용을 **데이터로만** 읽어야 한다.
  - 권장: core 가 작업 브랜치의 변경을 `git diff`/파일 목록 형태로 받아, GitHub Git Database API(blob·tree·commit·ref)로 커밋을 만든다. core 는 git 을 실행하지 않는다. 지금 core 이미지에는 git 이 없다 **[확인]**.
  - 차선: core 가 자기만 쓰는 별도 클론(볼륨 밖)에 패치를 적용해 push 한다. 패치는 데이터이므로 hooks·config 경로가 없다. 단 `.gitattributes` 필터는 core 의 클론 설정에 필터가 없으면 동작하지 않는다.

### 2. 역할 사이의 오염
- 웹에서 받은 신뢰하지 않는 파일이나 다른 작업이 만든 파일이, 다음 작업에서 저장소의 일부처럼 읽힐 수 있다. 예: `CLAUDE.md`, `.claude/skills/`, `AGENTS.md` 를 worktree 에 심어 다음 작업의 지시처럼 읽히게 한다. 스킬이 파일시스템에서 자동으로 발견되는 C 에서 특히 위험하다(SDK 는 `cwd` 와 그 위 디렉터리의 `.claude/skills/` 를 읽는다).
- 대책
  - 디렉터리를 나눈다: `repo/`(기준 사본, 정리 프로세스만 쓴다), `worktrees/<task-id>/`(작업 하나), `scratch/<task-id>/`(웹 다운로드 등 임시). 작업이 끝나면 `scratch/<task-id>/` 를 지운다.
  - 스킬은 볼륨이 아니라 이미지(읽기 전용 경로)에서만 읽고, SDK 의 `skills` 목록을 정의에서 정한 이름으로 고정한다. `setting_sources` 에서 project 를 빼 볼륨의 `.claude/` 를 읽지 않는다.
  - Landlock 쓰기 경로를 작업 자기 폴더(`worktrees/<task-id>`, `scratch/<task-id>`)로만 연다. 다른 작업 폴더는 읽기만 하거나 열지 않는다.

### 3. 중간에 죽은 작업의 잔해
- 남은 worktree, `index.lock`, worktree 의 `locked` 파일, 반쯤 끝난 rebase·merge. 한 브랜치는 두 worktree 에서 동시에 checkout 할 수 없어(`git worktree add` 가 거부한다), 이어서 하는 작업(ADR-0025, 같은 브랜치)이 막힌다.
- 대책: worktree 이름과 경로는 task ID 로 한다. 정리 프로세스가 시작할 때와 주기적으로 `git worktree prune` 을 하고, 살아 있는 임대가 없는 작업의 worktree 를 `git worktree remove --force` 로 지운다. 일정 시간보다 오래된 `*.lock` 은 그 작업의 임대가 끝났을 때만 지운다. 같은 브랜치를 이어받는 작업은 앞 작업의 worktree 를 먼저 지운 뒤 새로 만든다(I-29 로 한 스레드의 작업은 한 번에 하나다).

### 4. git 동시 실행
- 여러 작업이 동시에 `fetch` 하면 `refs`·`packed-refs` 잠금에서 실패하고, 자동 gc 가 다른 작업이 쓰는 객체를 정리하는 중에 겹칠 수 있다.
- 대책: 저장소에 `gc.auto=0`, `maintenance.auto=false` 를 둔다. `fetch` 와 `gc` 는 파일 잠금(예: `flock repo/.enikk-fetch.lock`)을 잡은 한 곳(정리 프로세스 또는 core 가 시작하는 짧은 Job)에서만 한다. 작업은 fetch 하지 않고 이미 받은 ref 에서 worktree 를 만든다.

### 5. 디스크
- worktree, `.venv`(uv), pytest 의 임시 Postgres, 빌드 산출물이 쌓여 볼륨이 차면 **모든 역할이 함께 멈춘다.** 지금은 작업마다 emptyDir 한도(4Gi)로 끊기고 Pod 가 사라지면 함께 사라진다.
- 대책: PVC 용량 한도, 작업당 상한 확인(시작 전 남은 용량 검사), 정리 Job(끝난 작업의 worktree·scratch 삭제). uv 캐시는 볼륨에서 공유해 `.venv` 생성 시간을 줄이되, 캐시 크기 상한(`uv cache prune`)을 둔다. `hostpath` 같은 로컬 볼륨은 용량 한도를 강제하지 않을 수 있다 **[추정]** → 정리 작업이 유일한 방어선이 된다.

### 6. 스토리지와 배치
- 노드가 하나면 로컬 볼륨(RWO 는 "한 노드"에서의 동시 마운트를 허용한다)을 여러 Pod 가 함께 쓸 수 있다. 노드가 여럿이면 RWX 스토리지(NFS 등, 상주 서버 필요)가 필요하거나 Pod 를 같은 노드에 묶어야 한다(nodeAffinity/podAffinity).
- 네임스페이스: PVC 는 네임스페이스 단위다(B 의 걸림돌). C 처럼 실행 환경이 하나면 이 문제가 없다.
- UID: 지금 runtime 과 개발 Pod 모두 UID/GID 10001, fsGroup 10001 이라 맞다 **[확인]**. 다른 이미지가 볼륨을 쓰게 되면 fsGroup 을 맞춘다. 큰 볼륨은 fsGroup 재귀 변경이 느리므로 `fsGroupChangePolicy: OnRootMismatch` 를 쓴다.

### 7. 잃는 보장
- **개발 작업이 대화 환경을 망가뜨리지 않는다는 격리**: 지금은 개발 Pod 가 무엇을 하든 runtime 과 다른 개발 Pod 에 남지 않는다. 공유 볼륨에서는 디스크를 채우거나, 다른 worktree·캐시를 망가뜨리거나, 위험 2 의 파일을 심을 수 있다. C 에서는 같은 프로세스(runtime)에서 돌므로 무거운 검사가 대화의 CPU·메모리를 빼앗는다.
- **역할마다 모델·도구를 엄격히 나누는 것**: 지금은 정의 파일(I-10, I-14, I-16)이 역할마다 도구·위임·실행 위치를 고정한다. 스킬은 같은 세션 안에서 불리는 지시일 뿐이라, 스킬 단위로 도구를 막는 장치가 아니다(SDK 문서: 스킬은 세션의 도구로 실행되고, `allowed-tools` 는 미리 승인일 뿐 제한이 아니다). 그래서 C 에서도 **권한의 단위는 스킬이 아니라 작업(정의)으로 남겨야 한다.** 예: "개발 작업"은 `publish_branch` 도구가 열린 정의로 시작하고, "대화 작업"은 열리지 않은 정의로 시작한다. 스킬은 그 안에서 쓰는 절차일 뿐이다.
- **프롬프트 인젝션에 속은 에이전트가 할 수 있는 일**
  - 지금: 개발 Pod 의 모델은 GitHub 토큰으로 `agent/**` 에 push 하고 PR 을 만들 수 있고, 토큰을 내보낼 수 있다(1시간, 저장소 하나). 대화 역할은 GitHub 쓰기가 없다.
  - B: 위에 더해, 대화 역할(웹 내용을 가장 많이 읽는다)이 공유 `.git` 에 hooks 를 심어 다음 개발 작업의 토큰으로 코드를 실행할 수 있다. **범위가 넓어진다.**
  - C·D: 토큰을 내보낼 수 없다. push·PR 은 core 도구로만 되고, core 가 대상 저장소·브랜치(`agent/<task>`)·크기를 확인한다. 단 C 에서는 대화 작업이 공유 볼륨의 다른 작업 worktree 를 고쳐, 그 작업이 core 도구로 내보낼 내용에 끼워 넣을 수 있다(Landlock 으로 작업 폴더를 나누면 막힌다). 머지는 여전히 소유자 승인(I-19)이 막는다.

## 결정 (제안 — 권고안)

결정은 소유자가 한다. 권고는 **"A 를 먼저, 그다음 D, 볼륨과 스킬화(C)는 측정 뒤"** 의 단계적 이행이다.

1. **A2 + A3 를 지금 한다.** 대화 역할에 원문 읽기 도구(`read_repo`, `pr_diff`, 머지된 PR 까지 보는 `pull_requests`)를 core 가 GitHub API 로 제공한다. 문제(원문을 못 본다)를 가장 싸게, 보안 경계를 바꾸지 않고 해결한다. 프롬프트·도구 설명의 어긋남(승격 위치 등)을 찾는 데도 바로 쓴다.
2. **D 를 다음에 한다.** push·PR 생성을 core 도구로 빼서 GitHub 쓰기 토큰을 core 에만 둔다. C 의 가장 큰 보안 이득을, 볼륨과 상주 비용 없이 먼저 얻는다. C 로 가더라도 이 도구는 그대로 쓴다.
3. **공유 볼륨과 스킬화(C)는 다음을 확인한 뒤 따로 결정한다.** 노드 수와 스토리지, clone·`uv sync` 지연이 실제로 얼마인지, runtime 동시 처리를 넣을 가치가 있는지, 스킬 본문을 어디에 둘지.
   - C 로 간다면: 실행 환경을 하나로 만들되 **권한 단위는 정의(작업 종류)로 남기고**, 스킬은 절차만 맡긴다. 볼륨에서는 core 가 git 을 실행하지 않는다(위험 1). 무거운 개발 작업은 지금처럼 Job 으로 띄우는 선택을 남겨 둔다(같은 이미지, 같은 스킬, 다른 실행 위치).

## 근거
- 문제의 핵심은 "원문을 읽을 수단"이다. 이것은 읽기 전용 도구로 해결되며 볼륨이 필요하지 않다(A).
- 공유 볼륨의 가장 큰 위험(위험 1)은 "같은 `.git` 을 토큰 없는 쪽이 쓰고 토큰 있는 쪽이 실행한다"는 데서 온다. 토큰을 모델 쪽에서 없애면(D) 이 위험의 원인이 사라지고, 볼륨을 도입할지 말지와 상관없이 ADR-0021 이 감수한 위험도 줄어든다.
- 온프레미스 제약에서는 "작업마다 뜨는 Pod"가 쉬는 동안 0 자원이므로 오히려 유리하다. 상주 runtime 에 개발 작업을 합치면 지연은 줄지만 늘 잡아 두는 메모리와 이미지 크기가 커진다. 측정 없이 합치면 제약에 어긋날 수 있다.
- 스킬은 지시를 나누는 단위이지 권한을 나누는 단위가 아니다(외부 사실 참고). 권한은 정의와 core 도구에서 나와야 I-14·I-16 이 유지된다.

### 외부 사실 (확인 2026-11-23)
- Claude Agent SDK 스킬: `.claude/skills/<name>/SKILL.md` 를 `setting_sources`(user/project) 경로에서 파일로 발견한다. `skills` 옵션으로 쓸 수 있는 스킬 이름을 고르며, 명시적 `tools` 목록을 쓰면 `Skill` 을 넣어야 한다. 스킬은 세션의 도구로 실행되고, `allowed-tools` 는 미리 승인일 뿐 다른 도구를 제한하지 않는다. 목록에 없는 스킬도 파일은 Read·Bash 로 읽힌다. 출처: https://code.claude.com/docs/en/agent-sdk/skills
- git worktree: 새 worktree 는 `HEAD`, `index` 같은 per-worktree 파일을 빼고 모두 공유한다. 저장소 `config` 는 기본으로 모든 worktree 가 함께 쓴다(`extensions.worktreeConfig` 로 worktree 별 설정을 둘 수 있다). 다른 worktree 가 checkout 한 브랜치로는 `add` 가 거부한다(`--force` 제외). `prune` 은 작업 폴더가 없는 worktree 정보를 지운다. 출처: https://git-scm.com/docs/git-worktree
- hooks 위치가 공통 디렉터리라는 점과 `core.hooksPath` 동작은 githooks 문서를 따른다. 출처: https://git-scm.com/docs/githooks (이 ADR 을 쓰며 본문을 다시 읽지는 않았다)

## 결정 뒤 바꿀 것과 이행 순서

### 1단계: A (읽기 도구)
- 코드: core API(`/v1/tasks/{id}/diagnostics/...` 또는 새 경로)와 플랫폼 도구 `read_repo`, `pr_diff`, `pull_requests(state, number)`. `watch` 토큰(읽기)만 쓴다. 결과에서 비밀값을 가리고 크기를 자른다.
- 문서: ADR-0029 에 도구를 더한다(부분 수정). 불변식 변화 없음.
- 프롬프트(Langfuse): assistant 에 "코드 근거가 필요하면 `read_repo`/`pr_diff` 를 쓴다"를 더한다. 승격 위치를 "승인 채널"로 고친다. `propose_prompt` 도구 설명의 "DM" 도 코드에서 함께 고친다.
- 되돌리기: 도구를 정의에서 빼거나 PR 을 revert 한다. 상태가 남지 않는다.

### 2단계: D (권한 동작을 core 도구로)
- 코드: core 도구 `publish_branch(files|patch)`·`open_pull_request(title, body)`. core 는 작업 임대, 대상 저장소(`config/github.yaml`), 브랜치 `agent/<task>`, 워크플로 파일 수정 허용 여부, 크기를 확인한 뒤 Git Database API 로 커밋하고 PR 을 연다. 개발 Pod 는 `/v1/github/token` 을 부르지 않는다. 같은 PR 을 이어 고치는 흐름(ADR-0025)도 이 도구로 한다.
- 문서: 새 ADR 로 ADR-0019(개발 토큰 전달)·ADR-0021(§3 4~5단계, §7, 감수한 위험)을 부분 대체. I-32 를 "GitHub 쓰기 토큰은 core 밖으로 나가지 않는다"로 고친다. `src/libs/adapters/github/` 와 새 도구 코드를 민감 경로에 둔다.
- 매니페스트: core values 의 `roleBindings[dev-agent]` 는 그대로(GitHub 토큰은 원래 별도 API). 개발 이미지에서 gh 제거 검토.
- 프롬프트: developer 의 "push 하고 `gh pr create`" 를 도구 사용으로 바꾼다.
- 되돌리기: `/v1/github/token` 을 지우지 않고 한 릴리스 동안 남겨 두면, 프롬프트를 되돌리는 것만으로 이전 흐름으로 돌아간다(I-34 와 같은 expand/contract).

### 3단계: C (결정할 경우)
1. 노드·스토리지 확인 → PVC(또는 정적 PV) 하나, `fsGroupChangePolicy: OnRootMismatch`. 매니페스트: `deploy/helm/runtime-claude`(볼륨, 한도), 정리 CronJob(짧게 떴다 사라짐), 이미지 통합(`dev-agent.Dockerfile` 의 도구를 runtime 이미지로).
2. runtime 동시 처리: 작업별 폴더·Landlock 도메인(ADR-0033 재검토 조건), 볼륨 경로를 작업 폴더로만 연다.
3. 스킬: 이미지의 읽기 전용 경로에 `SKILL.md` 를 두고 `setting_sources` 에서 project 를 뺀다. 계약 `BUILTIN_TOOLS` 에 `Skill` 을 더하고 정의에 `skills: [..]` 를 둔다. 스킬 본문을 git 에 두면 I-13(프롬프트 본문은 git 에 없다)과 부딪히므로 원본 위치를 먼저 정한다(열린 질문).
4. 역할 정의: `execution` 은 남기되 값의 뜻을 "Job 으로 띄울지"로 좁히고, 개발·대화·요약 정의를 하나의 이미지·runtime 에서 쓰게 한다. summarizer 는 도구 없는 정의 그대로 둔다(I-38).
- 문서: 새 ADR 로 ADR-0005, ADR-0021, ADR-0022, ADR-0031, ADR-0033 을 부분 대체. 불변식 I-11, I-12, I-14, I-17, I-25(볼륨은 상태다 — "런타임은 상태를 갖지 않는다"와 충돌, 볼륨은 캐시이고 원본이 아님을 명시해야 한다), I-28 을 고친다. `config/sensitive-paths.yaml` 에 스킬 경로를 더한다.
- 프롬프트(Langfuse): assistant·developer 프롬프트를 스킬로 나눌 경우 `production` 승격(I-36)으로 바꾼다.
- 되돌리기: Job 경로(ADR-0021)를 지우지 않고 설정으로 남겨 두면(`execution: isolated`), 정의 한 줄로 개발 작업을 다시 Job 으로 보낼 수 있다. 볼륨은 캐시이므로 지워도 원본(GitHub)에서 다시 만든다.

## 결과 (권고대로 결정될 경우)
- 1단계 뒤: 대화 역할이 코드·PR 원문으로 답한다. 보안 경계는 그대로다.
- 2단계 뒤: 모델이 읽을 수 있는 GitHub 쓰기 토큰이 없어진다. core 의 GitHub 쓰기 범위(코드)가 넓어지므로 그 코드는 민감 경로가 된다.
- 3단계는 별도 결정이다. 하지 않으면 실행 환경 둘과 작업당 clone 지연은 남는다.

## 열린 질문 (소유자)
1. 노드는 지금도 하나인가(Docker Desktop 의 단일 노드인가, kind 기반 다중 노드인가)? 기본 스토리지 클래스는 무엇인가(`kubectl get nodes`, `kubectl get storageclass`)? 노드 메모리는 얼마인가?
2. 공유 볼륨을 저장소 말고 어디에 쓸 생각인가? 예: uv·pip 캐시, 대화 역할이 만든 파일을 개발 작업에 넘기기, 긴 작업의 중간 산출물.
3. 볼트도 같은 볼륨으로 다룰까? 지금은 core 가 GitHub API 로 읽고 쓰며 역할은 파일을 직접 보지 않는다(ADR-0028). 볼륨에 두면 쓰기 규칙(경로, `draft: true`, 소유자 시작 작업)을 core 가 아닌 곳에서 지켜야 한다.
4. 스킬 본문의 원본은 어디인가? git(리뷰·민감 경로로 관리, I-13 수정 필요)인가, Langfuse(지금 프롬프트처럼, 승격 절차 유지)인가?
5. 개발 작업이 대화 응답을 늦추는 것을 받아들일 수 있는가? 아니면 runtime 동시 처리나 복제본 증설에 자원을 쓸 것인가?
6. 1단계(A)만으로 "저장소를 못 본다"는 문제가 충분히 풀리는지, 써 본 뒤 2·3단계를 결정할 것인가?

## 재검토 조건
- 노드가 여러 대가 되거나 RWX 스토리지를 쓰게 될 때.
- 개발 작업의 시작 지연(clone, `uv sync`)이 실제 병목으로 측정될 때.
- Claude Agent SDK 가 스킬 단위로 도구를 제한하는 기능을 제공할 때(위험 7 의 전제가 바뀐다).
- Pod 에서 사용자 네임스페이스를 쓸 수 있게 될 때(ADR-0033 과 함께, 작업별 격리 수단이 늘어난다).
