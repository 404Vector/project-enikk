# ADR-0033: 모든 역할에 SDK 기본 도구를 주고, 서비스의 비밀값과 떼어 놓는다

- 상태: 승인 (도구 없는 요약 역할 `summarizer` 는 ADR-0035 로, §2.2 쓰기 경로와 §2.5 작업 폴더는 ADR-0037 로 부분 대체. 세션의 단기 GitHub 토큰을 도구가 읽는 예외는 ADR-0041 3a)
- 날짜: 2026-11-23
- 관련: ADR-0002, ADR-0005, ADR-0007, ADR-0013, ADR-0017, ADR-0019, ADR-0021, ADR-0022, ADR-0028, ADR-0031, ADR-0032, I-02, I-05, I-07, I-14, I-17, I-19, I-28, I-35, I-36

> §2.2 의 쓰기 허용 경로와 §2.5 의 작업 폴더 정리는 ADR-0037 이 부분 대체한다. 공용 역할의 작업에는 공유 볼륨의 그 스레드 경로(기준 저장소, worktree, tmp)가 쓰기로, 스냅샷이 읽기로 더해진다. `/work` 는 지금처럼 작업마다 비우지만 스레드의 worktree 는 같은 스레드의 다음 작업까지 남는다. 다른 스레드의 경로는 열리지 않는다.

## 배경
소유자는 정확한 작업을 위해 에닉의 모든 역할(`agents/*.yaml`)이 Claude Agent SDK 의 기본 도구, 즉 파일·검색·명령 실행·웹·작업 관리 도구를 쓰도록 결정했다(2026-11-23). 웹과 명령 실행 도구가 비밀값 유출 경로가 될 수 있다는 점을 알고 내린 결정이다. 다만 다음은 지켜야 한다.
- 공용 런타임(runtime-claude)은 여러 대화를 처리하는 상시 서비스다. 명령과 파일 도구가 런타임의 비밀값(Claude 토큰, core 와 통신하는 토큰, Langfuse 키)을 읽을 수 없어야 한다.
- 웹 검색어와 접속한 URL, 명령은 역할·작업 ID 와 함께 기록한다(비밀값은 가린다).
- 웹에서 가져온 내용은 신뢰하지 않는 입력이다.
- 브라우저 자동화(Chrome MCP 등)는 넣지 않는다.

**바꾸기 전 (2026-11-23, main 7ebbcbf 기준)**
- assistant(공용): 기본 도구 없음. ADR-0017 이 `tools=[]` 로 막았고, 계약 검증(ADR-0021 §7)이 `builtin` 을 격리 역할에만 허용했다.
- developer(격리): `Bash, Read, Write, Edit, Glob, Grep`. **WebSearch·WebFetch 는 없었다.**
- 두 역할 모두 Claude 토큰을 CLI 의 환경변수 `CLAUDE_CODE_OAUTH_TOKEN` 으로 받았다. 그래서 developer 의 명령은 그 토큰을 읽을 수 있었다.

**확인한 사실 (2026-11-23, 저장소의 SDK 0.2.187 에 들어 있는 Claude Code 2.1.330 을 직접 실행)**
- 도구 이름: `--tools` 로 받아들이는 이름은 `Read, Write, Edit, NotebookEdit, Glob, Grep, Bash, Monitor, TaskStop, WebSearch, WebFetch, TaskCreate, TaskGet, TaskList, TaskUpdate, Task` 다. `MultiEdit, BashOutput, KillShell, TodoWrite` 는 이 버전에 없다. 백그라운드 명령은 `Monitor`/`TaskStop`, 할 일 목록은 `TaskCreate/TaskGet/TaskList/TaskUpdate` 가 맡는다.
- WebSearch 는 CLI 가 Anthropic Messages API 의 서버 도구 `web_search_20250305` 를 같은 인증으로 부르는 방식이다. 별도 키가 없다. CLI 의 도구 설명에 "Web search is only available in the US" 가 있다.
- WebFetch 는 CLI 가 직접 URL 을 가져온다. 그 전에 `api.anthropic.com/api/web/domain_info` 로 도메인을 확인하고, 가져온 내용은 작은 모델로 요약해 돌려준다. 점(.)이 없는 호스트 이름은 거부한다.
- Bash 도구는 `CLAUDE_CODE_SHELL_PREFIX` 가 있으면 명령 스크립트 전체를 인자 하나로 넘겨 `<prefix> '<script>'` 로 실행한다.
- CLI 는 `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` 가 가리키는 파일 디스크립터에서 토큰을 읽는다. 이렇게 하면 토큰이 어떤 프로세스의 환경에도 없다.
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 은 bubblewrap 을 요구하고, 없으면 CLI 가 시작하지 않는다.
- 클러스터 노드(Docker Desktop, 커널 6.18): Pod 의 seccomp(RuntimeDefault)가 사용자 네임스페이스를 막는다(`unshare -U` 실패). 그래서 bubblewrap 과 Claude Code 의 자체 샌드박스는 쓸 수 없다. Landlock 은 ABI 7 로 쓸 수 있다. `ptrace_scope` 는 1 이다.

## 검토한 대안
- **Claude Code 의 자체 샌드박스(bubblewrap)**: 사용자 네임스페이스가 필요하다. 쓰려면 Pod 를 privileged 로 바꾸거나 seccomp 를 풀어야 한다. → 기각.
- **명령마다 따로 컨테이너(Job·사이드카)에서 실행**: 격리는 가장 강하다. 그러나 SDK 의 Bash 도구를 대신할 도구를 직접 만들어야 하고, core 나 런타임에 Pod 실행 권한(I-28)이 필요하다. → 이번에는 기각, 재검토 조건에 둔다.
- **환경변수만 지운다**: 쉽다. 그러나 마운트된 core 토큰 파일, CLI 프로세스의 `/proc/<pid>/environ`, Read 도구는 그대로 열린다. → 부족하다.
- **Landlock 으로 CLI 와 명령을 가두고, 토큰은 파이프로 넘긴다**: 권한이 필요 없고 Pod 보안 설정을 바꾸지 않는다. → 채택.

## 결정

### 1. 역할별 도구
- 모든 역할(지금은 assistant, developer)에 다음 도구를 준다: `Read, Write, Edit, NotebookEdit, Glob, Grep, Bash, Monitor, TaskStop, WebSearch, WebFetch, TaskCreate, TaskGet, TaskList, TaskUpdate, Task`.
- 역할 정의 계약은 `builtin` 을 격리 역할에만 허용하던 검증을 없애고, 위 목록 밖의 이름을 거부한다. 예약 실행·worktree·스킬·워크플로 같은 다른 도구, 그리고 브라우저 자동화(MCP)는 정의에 넣을 수 없다.
- enikk 의 플랫폼 도구(위임, 기억, 진단, `propose_prompt`)와 그 권한 경계는 그대로다.

### 2. 비밀값과 떼어 놓기 (엔진 `enikk_engine_claude.sandbox`)
1. **토큰은 파이프로**: SDK 의 `cli_path` 는 `enikk-claude-launch` 다. 런처는 `ENIKK_CLAUDE_OAUTH_TOKEN` 을 환경에서 빼서 파이프에 쓰고, `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` 로 CLI 에 넘긴다. 그래서 CLI 와 그 하위 프로세스의 환경에는 토큰이 없다. Langfuse 키와 core 토큰은 원래 CLI 에 넘기지 않는다.
2. **CLI 를 Landlock 으로 가둔다**: 런처는 CLI 를 실행하기 전에 자신을 제한한다.
   - 읽기만 허용하는 경로: `/usr, /bin, /sbin, /lib*, /etc, /opt, /app, /proc, /sys, /var/lib, /var/cache`, Python 과 CLI 설치 경로.
   - 쓰기까지 허용하는 경로: 작업 폴더, 작업별 홈과 `TMPDIR` 가 있는 scratch 폴더, `/dev`, 역할이 더한 경로(developer 는 저장소와 git·gh 설정 폴더).
   - 그 밖은 열 수 없다. Kubernetes 가 비밀값을 마운트하는 `/run`(`/var/run`)도 포함된다.
   - 런처는 제한한 뒤 지켜야 할 경로(projected core 토큰 파일과 그 폴더)가 정말 열리지 않는지 확인한다. 열리면 CLI 를 시작하지 않는다.
3. **명령은 한 겹 더**: `CLAUDE_CODE_SHELL_PREFIX` 는 `enikk-sandbox-shell` 이다. 이 프로그램은 명령마다 다음을 한다.
   - 이름이나 값이 비밀값처럼 보이는 환경변수와 토큰 디스크립터를 지운다.
   - 같은 규칙으로 중첩 Landlock 도메인에 들어가고, 지켜야 할 경로를 다시 확인한다.
   - bash 로 스크립트를 실행한다.
   - 중첩 도메인은 바깥(CLI, 런타임) 프로세스의 메모리와 `/proc/<pid>/environ` 을 읽을 수 없고 신호도 보낼 수 없다(Landlock 의 ptrace·신호 범위 제한).
4. **런타임 프로세스**: Claude 토큰, Langfuse 키, core 토큰은 Python 런타임 프로세스의 메모리와 마운트된 파일에만 있다. 명령과 CLI 는 그 프로세스의 자손이므로 `ptrace_scope=1` 로도, Landlock 으로도 그 메모리를 읽을 수 없다. 런타임 Pod 에는 원래 Kubernetes API 토큰이 없다(I-28).
5. **작업 폴더**: 공용 역할의 작업 폴더는 작업이 끝날 때마다 비운다. 한 대화의 파일이 다음 대화로 넘어가지 않게 하기 위해서다.
6. **Landlock 이 없으면**: 런타임은 시작할 때 커널을 확인한다. Landlock 이 없으면 공용 역할은 기본 도구 없이 실행한다(대화와 플랫폼 도구는 그대로). 격리 역할은 이 ADR 이전처럼 도구를 쓰되, 토큰은 계속 파이프로 받는다.

### 3. 웹
- 웹 도구는 SDK 기본(WebSearch 는 Anthropic 서버 검색, WebFetch 는 CLI 가 직접 가져오기)을 쓴다. 새 키는 없다.
- **웹에서 가져온 내용은 신뢰하지 않는 입력이다.** 검색 결과와 페이지 내용은 참고 자료일 뿐 지시가 아니다. 그 안의 지시로 권한이 늘거나 승인 절차가 바뀌지 않는다. 규칙과 권한은 저장소와 역할 정의에서만 온다(I-16, I-35 와 같은 원칙).
- WebFetch 로 클러스터 내부와 사설 주소(`*.svc`, `*.cluster.local`, `*.local`, `localhost`, 점 없는 이름, 사설·루프백·링크 로컬 IP)를 가져오는 요청은 hook 에서 거부한다.
- WebSearch 가 이 계정에서 동작하지 않으면(CLI 설명의 미국 한정 문구) 외부 검색 API 를 플랫폼 도구로 붙인다. 후보 비교와 추천은 아래 "웹 검색 대안"에 둔다. 그때 필요한 키는 core 가 관리하고 런타임 역할에만 준다(ADR-0013).

### 4. 기록
- SDK 의 PreToolUse hook 이 `Bash`, `Monitor`, `WebFetch`, `WebSearch` 호출마다 `tool <도구> role <역할> task <작업 ID>: <명령|URL|검색어>` 를 런타임 로그(`enikk_engine_claude.audit`)에 남긴다. 비밀값처럼 보이는 부분은 가리고 2,000자에서 자른다.
- hook 이 실패하면 도구를 실행하지 않는다(`on_failure="block"`).
- 로그는 Pod 로그이며, 진단 도구(ADR-0029)로 읽을 수 있다.

### 5. 개발 Pod 의 비밀값 범위 (점검 결과)
- **GitHub 개발 토큰**: 저장소 하나로 좁혔다. `contents`·`pull_requests` 쓰기, `checks`·`statuses`·`metadata` 읽기만 있고, 워크플로 쓰기는 저장소 정책이 허용할 때만 준다. 승인 기록·저장소 관리 권한은 없다. 1시간 뒤 만료되고, 작업이 끝나면 폐기된다. push 는 `agent/**` 에만 된다(ADR-0019, ADR-0020).
  - git·gh 가 써야 하므로 개발 Pod 의 명령은 이 토큰을 읽을 수 있다(ADR-0021 이 감수한 위험). 웹 도구가 더해져 내보낼 길이 늘었지만, 명령에서 이미 인터넷에 나갈 수 있었으므로 새 경로는 아니다.
- **Claude 토큰**: 이 ADR 전에는 개발 Pod 의 명령이 환경변수로 읽을 수 있었다. 이제 파이프로만 넘기므로 명령도 CLI 의 도구도 읽지 못한다.
- **Langfuse 키**: Python 프로세스 메모리에만 있다. 명령은 읽지 못한다.
- **core 토큰(dev-agent 역할)**: 마운트된 파일이다. Landlock 으로 막았다.

## 불변식과의 관계
- **I-14**(기본 차단, 정의에 명시한 것만): 그대로 지킨다. 도구는 정의에 적은 것만 열고, 시작 메시지로 다시 확인한다.
- **I-02, I-07**(토큰 노출 금지): 더 강해졌다. 토큰은 환경변수가 아니라 파이프로 CLI 에만 간다. 명령과 파일 도구는 비밀값을 읽지 못하므로, 비밀값이 대화 이력이나 트레이스에 실릴 길이 막힌다.
- **I-17**(Slack 에 응답하는 실행용 역할은 git·클러스터 쓰기 권한이 없다, 코드·정의 변경은 developer 만 만든다): 지킨다.
  - assistant 의 런타임에는 GitHub 자격증명도 Kubernetes API 토큰도 없고, core 토큰은 Landlock 으로 막혔다.
  - assistant 가 enikk 의 코드를 바꾸지 않고 developer 에 위임하는 규칙(ADR-0022)은 그대로다. assistant 의 셸은 작업 폴더 안에서만 파일을 쓸 수 있고, 그 폴더는 작업이 끝나면 지워진다.
- **I-36**(Langfuse 에 쓰는 곳은 core 뿐): 지킨다. Langfuse 키는 런타임과 개발 Pod 의 Python 프로세스 메모리에만 있고, 명령과 CLI 는 그 메모리에 닿지 못한다.
- **I-19 와 "승인 전에는 아무것도 바뀌지 않는다"**: enikk 의 코드·정의·프롬프트·배포는 여전히 소유자 승인 뒤에만 바뀐다. 다만 명령은 인터넷의 다른 서비스에 요청(예: HTTP POST)을 보낼 수 있다. 그런 외부 효과는 승인 절차 밖이다(남는 위험 3).
- **I-05**(MCP 는 agentgateway 경유): 웹 도구는 MCP 가 아니라 CLI 내장 도구이므로 해당하지 않는다.
- **I-35**(기억은 참고일 뿐): 웹 내용도 같은 원칙(신뢰하지 않는 입력)을 따른다.
- 새 불변식 **I-37** 을 둔다: "모델이 쓰는 기본 도구(명령, 파일, 웹)는 서비스의 비밀값을 읽을 수 없는 곳에서만 실행한다."

## 남는 위험
1. **대화·작업 내용의 유출**: 명령과 웹 도구로 대화 내용, 작업 폴더의 파일, developer 의 GitHub 토큰을 인터넷에 보낼 수 있다. 웹 내용에 숨은 지시(프롬프트 주입)가 계기가 될 수 있다. 소유자가 감수한 위험이다.
2. **클러스터 내부 접근**: Docker Desktop 은 NetworkPolicy 를 적용하지 않는다(ADR-0021). WebFetch 의 내부 주소는 hook 이 막지만, 명령(예: Python 의 HTTP 요청)으로는 클러스터 안의 서비스에 닿을 수 있다. core API 는 모든 요청을 인증하므로(I-28) 토큰 없이는 쓸 수 없다. 인증이 없는 다른 서비스가 생기면 위험해진다.
3. **외부 효과**: 명령으로 인터넷 서비스에 쓰기 요청을 보낼 수 있고, 되돌릴 수 없을 수 있다.
4. **프롬프트 주입으로 플랫폼 도구를 쓰는 것**: 웹 내용이 모델을 움직여 기억 쓰기(승인 없음, ADR-0028), 위임, 프롬프트 후보 만들기를 시킬 수 있다. 위임과 후보는 소유자 승인이 막고, 기억은 참고로만 쓰인다(I-35).
5. **같은 Pod 안의 잔여물**: 공용 런타임의 작업 폴더와 작업별 홈은 지우지만, scratch 폴더의 다른 곳에 쓴 파일은 Pod 가 재시작할 때까지 남을 수 있다.
6. **자원**: 공용 런타임에서 무거운 명령은 그 Pod 의 CPU·메모리 한도를 쓴다. 작업 시간 한도(`limits.timeout_seconds`)가 상한이다.
7. **Landlock 이 없는 노드**: 공용 역할은 도구를 잃고, 격리 역할은 Landlock 없이 실행된다(토큰은 계속 파이프).
8. **CLI 프로세스 메모리**: Claude 토큰은 CLI 메모리에 있다. CLI 의 파일 도구로 자기 메모리를 읽는 것은 실제로 확인하지 않았다(`/proc/self/environ` 읽기는 CLI 가 거부했다).
9. **WebSearch 가용성**: 미국 한정일 수 있다. 검색어는 Anthropic 으로 간다.

## 웹 검색 대안 (SDK WebSearch 가 동작하지 않을 때)
가격과 한도는 이 ADR 을 쓰며 확인하지 않았다. 도입할 때 확인한다.
- **Brave Search API**: 자체 색인을 가진 일반 웹 검색이다. 결과가 단순한 JSON(제목, URL, 요약)이라 플랫폼 도구로 감싸기 쉽다. 내용 추출은 없으니 WebFetch 와 함께 쓴다. → **추천.** 검색과 가져오기를 나눠 두면 가져올 페이지를 모델이 고르고 기록도 남는다.
- **Tavily**: LLM 용 검색이다. 결과에 페이지 본문 요약이 함께 온다. 호출이 적고 편하지만, 가져온 본문이 신뢰하지 않는 입력으로 한꺼번에 들어온다. → 차선.
- **Exa**: 의미 기반 검색과 본문 가져오기다. 연구형 질의에 강하다. 일반 질의에는 과하다.
- 도입하면 키는 core 가 관리한다(ADR-0013). 이름 예: `search.brave-api-key`. `CredentialName` 과 core values 의 `roleBindings[runtime].credentials` 에 함께 넣고, 소유자가 PC 에서 `uv run enikk-ops secret set search.brave-api-key` → `uv run enikk-ops secret sync` 로 등록한다. 키가 없으면 검색 도구만 꺼진다.

## 결과
- ADR-0017 의 "공용 역할은 도구 없음(`tools=[]`)"과 "토큰은 환경변수로", ADR-0021 §5·§7 의 "`builtin` 은 격리 역할만, developer 만 셸·파일 도구"를 이 ADR 이 대체한다(부분 대체).
- `agents/` 전체, 엔진의 `sandbox.py`·`audit.py` 를 민감 경로(ADR-0024)에 둔다.
- 명령마다 샌드박스 프로그램이 한 번 더 시작한다(약 0.1초).
- runtime-claude 와 dev-agent 이미지에 `enikk-claude-launch`, `enikk-sandbox-shell` 이 들어간다(엔진 패키지의 스크립트). Pod 의 보안 설정은 바꾸지 않는다.

## 재검토 조건
- 클러스터가 NetworkPolicy 를 적용하게 되면 → 런타임과 개발 Pod 의 나가는 연결을 허용 목록으로 제한한다(남는 위험 2).
- Pod 에서 사용자 네임스페이스를 쓸 수 있게 되면 → Claude Code 자체 샌드박스와 비교한다.
- 공용 런타임이 여러 작업을 동시에 처리하게 되면 → 작업별 폴더와 도메인을 나눈다.
- Claude Code 가 도구 이름이나 `CLAUDE_CODE_SHELL_PREFIX`·토큰 디스크립터 동작을 바꾸면 → `test_sandbox.py` 의 실제 CLI 테스트로 확인한다.
