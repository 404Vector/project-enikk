# ADR-0032: Slack 에서 프롬프트 후보를 만들고, 소유자가 승격한다

- 상태: 승인 (§2 의 대상과 Config 검증은 ADR-0037 로 부분 대체: 스킬 후보와 `requires` 확장 경고)
- 날짜: 2026-11-23
- 관련: ADR-0005, ADR-0006, ADR-0007, ADR-0013, ADR-0016, ADR-0020, ADR-0023, ADR-0026, ADR-0031, ADR-0036, ADR-0037, I-01, I-13, I-14, I-16, I-17, I-19, I-24, I-27, I-28

> §2 의 "대상은 역할이 쓰는 프롬프트"와 "Config 는 `model`, `effort`"는 ADR-0037(2a 구현 세부)이 부분 대체한다. 대상에 스킬(`skill-*`)이 더해지고, 스킬의 Config 에는 `description`, `requires` 를 넘길 수 있으며 스킬 config 스키마를 통과해야 한다. `requires` 를 넓히는 후보는 승격 카드에 경고가 뜬다. 그 밖의 검증, label, 권한 경계(§3·§4)는 그대로다.

## 배경
ADR-0007 은 프롬프트 변경 경로를 "새 버전 작성(`candidate` 라벨) → 평가 → Slack 승인 → `production` 라벨로 승격"으로 정했다. 그 경로는 아직 없다. 지금 소유자는 Langfuse 화면에서 프롬프트를 직접 고친다(README "프롬프트 수정 방법").

소유자의 요구(2026-11-23):
- Slack 에서 "프롬프트를 이렇게 바꿔 줘"라고 하면 에닉이 Langfuse 에 후보 버전을 만들고, 소유자 DM 에 이전 판과의 차이와 [승격] 버튼을 보낸다.
- 소유자가 [승격]을 누를 때만 `production` label 이 새 버전으로 옮겨진다.
- 후보를 만드는 도구로는 `production` 을 절대 바꿀 수 없어야 한다. 코드와 권한 양쪽에서 막는다.

사실:
- 역할의 프롬프트는 text 형식이고 Config 에 `model` 이 있어야 한다. 아니면 그 역할은 실행을 거부한다(ADR-0016).
- Langfuse 공개 API(2026-11-23 확인, https://cloud.langfuse.com/generated/api/openapi.yml):
  - `POST /api/public/v2/prompts`: 같은 `name` 의 새 버전을 만든다. 본문은 `name`, `type`(`text`), `prompt`, `config`, `labels`, `commitMessage`. `tags` 는 모든 버전에 적용되므로 보내지 않는다.
  - `PATCH /api/public/v2/prompts/{name}/versions/{version}`: `newLabels` 로 label 을 붙이고 `removeLabels` 로 뗀다. label 은 버전 사이에서 유일하다. 한 버전에 `production` 을 붙이면 다른 버전에서는 떨어진다.
  - `GET /api/public/v2/prompts/{name}?label=…|version=…&resolve=false`: 저장된 그대로의 버전을 읽는다.
  - API 키(공개 키 + 비밀 키)는 Basic 인증으로 쓴다. 키별로 쓰기 권한을 나누는 방법은 이 명세에 없다.

## 검토한 대안
- **개발 역할(developer)이 후보를 만든다 (ADR-0007 원안)**: 격리 Pod 를 띄우고 저장소를 clone 해야 한다. 프롬프트 한 줄을 바꾸는 데 수십 초가 걸리고, Langfuse 키를 셸 도구가 있는 Pod 에 둔다. → 기각.
- **assistant 역할의 런타임이 Langfuse 에 직접 쓴다**: 단순하다. 그러나 Langfuse 키는 쓰기 범위를 나눌 수 없어, 같은 키로 `production` 도 옮길 수 있다. 막는 장치가 런타임 코드 하나뿐이다. → 기각.
- **core 가 Langfuse 에 쓰고, assistant 는 core API 로 후보만 요청한다**: 채택.

## 결정

### 1. 흐름과 맡는 곳
1. **후보 만들기** (assistant 역할 → 런타임 → core)
   - 소유자가 Slack 에서 프롬프트 변경을 말하면 assistant 가 플랫폼 도구 `propose_prompt` 를 부른다. 입력: 대상 프롬프트 이름, 새 본문, 변경 이유, (선택) Config 에서 바꿀 값.
   - 런타임은 core 의 `POST /v1/tasks/{task_id}/prompts/candidates` 를 부른다.
   - core 가 검증하고, 통과하면 Langfuse 에 후보 버전을 만들고, 승격 요청을 DB(`prompt_promotions`)에 기록하고, 소유자 DM 알림(`notices`)을 큐에 넣는다. 도구 결과로 후보 버전 번호를 돌려준다.
2. **승격 요청 DM** (core → connector-slack)
   > ADR-0036 으로 부분 대체: 승격 요청은 소유자 DM 이 아니라 승인 채널(설정값 `approval_channel`)에 올라가고, 실패하면 소유자 DM 으로 간다. [승격] [거절] [되돌리기] 는 채널에서도 소유자만 실행한다(커넥터와 core 가 모두 확인).
   - 본문: 프롬프트 이름, 그 프롬프트를 쓰는 역할, `production vN → 후보 vM`, 변경 이유 한두 줄, [승격] [거절] 버튼.
   - 스레드 댓글: 원래 요청 스레드 링크, 본문 diff, Config diff. 길면 여러 댓글로 나눈다(ADR-0020 의 PR 승인 DM 과 같은 방식).
   - 커넥터는 DM 을 올린 뒤 그 링크를 core(`POST /v1/prompt-promotions/{id}/posted`)에 알리고, core 는 원래 요청 스레드에 상태와 DM 링크만 올린다(ADR-0026).
3. **[승격]** (connector-slack → core → Langfuse)
   - 커넥터가 누른 사람이 소유자인지 확인한다(I-01). core 도 요청의 `decided_by` 가 `config/slack.yaml` 의 `owner` 와 같은지 다시 확인한다.
   - core 는 요청 행을 잠그고 상태가 `requested` 인지 본다. 아니면 아무것도 하지 않고 지금 상태를 돌려준다(중복 클릭, 재시도).
   - Langfuse 에서 지금 `production` 버전을 읽어, 요청 때 기록한 버전과 같을 때만 후보 버전에 `production` 을 붙인다. 다르면 승격하지 않고 `stale` 로 기록하고 다시 요청하라고 알린다.
   - 결과를 DM 메시지와 원래 요청 스레드에 알린다. 반영 시점도 적는다: 공용 런타임 역할(assistant)은 최대 60초 캐시 뒤, 격리 역할(developer)은 다음 작업부터.
4. **[거절]**: Langfuse 는 바꾸지 않는다. 후보 버전은 남겨 두고 `rejected` 로 기록하고 알린다.
5. **[되돌리기]**: 승격 결과 메시지에 둔다. 누르면 core 가 지금 `production` 이 그 후보 버전일 때만 `production` 을 승격 전 버전으로 옮기고 `reverted` 로 기록한다. 그 사이 다른 버전이 `production` 이 됐으면 되돌리지 않는다.
   - 프롬프트가 잘못되면 assistant 가 실행을 거부해 Slack 에서 새 후보를 만들 수도 없게 된다. 되돌리기는 LLM 없이 커넥터와 core 만으로 동작하므로 이때의 복구 경로가 된다.

### 2. 검증 (core, 후보를 만들기 전)
다음 중 하나라도 어기면 후보를 만들지 않고 이유를 돌려준다.
- 대상은 `agents/*.yaml` 의 어떤 역할이 쓰는 프롬프트 이름이어야 한다.
- 본문은 비어 있지 않은 text 이고 100,000자 이하다. 비밀값처럼 보이는 문자열이 있으면 거부한다(I-07).
- Config 는 지금 `production` 의 Config 위에 요청한 값(`model`, `effort`)을 덮어쓴 것이다. 결과에 비어 있지 않은 문자열 `model` 이 있어야 한다.
- 지금 `production` 이 있으면 text 형식이어야 한다(역할의 프롬프트는 text 다).
- 본문과 Config 가 지금 `production` 과 모두 같으면 만들지 않는다.

### 3. label
- 후보 버전은 `candidate` label 하나만 달고 만든다(ADR-0007). label 은 버전 사이에서 유일하므로, 새 후보가 생기면 이전 후보의 `candidate` 는 떨어진다. 승격과 되돌리기는 label 이 아니라 기록한 버전 번호로 한다.
- `latest` 는 Langfuse 가 관리한다. 런타임은 `production` 만 읽으므로 후보는 승격 전까지 쓰이지 않는다.

### 4. 권한 경계
- **Langfuse 에 쓰는 것은 core 뿐이다.** core 는 비밀값 브로커로서 이미 Langfuse 키(`langfuse.public-key`, `langfuse.secret-key`)를 갖고 있다. 새 비밀값은 없다. 키가 없으면 이 기능은 "core 에 Langfuse 키가 없다"는 오류로 꺼진다.
- **후보 만들기**: `runtime` 역할(공용 런타임)만 부를 수 있다. core 는 활성 임대를 확인하고, 그 작업의 역할 정의가 `tools.platform` 에 `prompt-candidates` 를 가졌는지, 그 작업이 소유자가 Slack 에서 시작한 대화인지 확인한다. 지금은 `agents/assistant.yaml` 만 이 도구를 가진다(I-16: 권한은 git 의 정의 파일에서만 바뀐다).
- **`production` 을 옮기는 코드**는 승격·되돌리기 처리에만 있고, 그 API(`/v1/prompt-promotions/{id}/decision`)는 Slack 커넥터만 부를 수 있다(ADR-0020 의 승인 API 와 같다). 후보 만들기 계약에는 label 필드가 없고, Langfuse 어댑터의 후보 생성 함수는 `candidate` 외의 label 을 받지 않는다.
- 승격 처리(`src/apps/core/enikk_core/prompts.py`)와 Langfuse 쓰기 어댑터(`src/libs/adapters/langfuse/enikk_langfuse/registry.py`)를 민감 경로(ADR-0024)에 더한다. `agents/assistant.yaml` 의 `tools.platform` 은 정의 파일 검토로 관리한다.
- 런타임과 개발 Pod 도 Langfuse 키를 받는다(프롬프트 읽기, 트레이스). 이 키로도 label 을 옮길 수 있다. 공용 역할에는 셸·HTTP 도구가 없어(I-14) 모델이 키를 직접 쓸 수 없다. 키 자체를 읽기 전용으로 나누는 것은 Langfuse 가 지원할 때 재검토한다.

### 5. 기록과 중복 처리
- `prompt_promotions` 한 행이 승격 요청 하나다: 프롬프트 이름, 요청 때의 `production` 버전, 후보 버전, 변경 이유, 원래 작업, 상태(`requested` → `promoted` | `rejected` | `stale`, `promoted` → `reverted`), 결정 시각과 사람.
- 버튼 처리는 행을 잠근 채 상태를 확인하고 바꾼다. 같은 버튼을 두 번 눌러도, 커넥터가 재시도해도 Langfuse 는 한 번만 바뀐다.
- Langfuse 를 부르다 실패하면 상태를 바꾸지 않고 오류를 돌려준다. 커넥터는 버튼을 남겨 두고 다시 눌러 달라고 알린다.

### 6. 평가
ADR-0007 의 "평가" 단계는 이 ADR 에 넣지 않는다. 평가 파이프라인(M6 `eval-runner`)이 생기면 승격 요청에 평가 결과를 붙인다.

### 7. 이미 시작된 스레드에 승격을 반영한다 (구현 세부, 2026-11-23)
- **증상**: 2026-11-23 assistant 의 production 이 v6 였고, 런타임 로그도 `prompt assistant v6` 를 남겼다. 그런데 04:41 에 시작한 스레드(`1795366132.766359`)의 이어진 작업들은 v4 문구로 답했다.
- **원인**: Claude Code(2.1.330)는 세션을 시작할 때 시스템 프롬프트를 세션 기록에 `prompt_snapshot` 으로 남긴다. `--resume` 으로 이어받을 때는 새로 받은 `--system-prompt` 대신 그 저장된 프롬프트를 다시 쓴다. 세션 기록은 core 에 있으므로(ADR-0014), 런타임이 v6 을 가져와 넘겨도 그 스레드는 처음 프롬프트(v4)로 계속 실행됐다. 개발 Pod 에서 실제 CLI 와 가짜 API 로 재현했다: V4 로 시작한 세션을 V6 로 이어받으면 API 가 받은 system 은 V4 였다.
- **결정**: 엔진은 세션을 이어받기 전에 core 의 세션 기록에서 마지막 `prompt_snapshot` 을 읽는다. 그것이 지금 프롬프트와 다르면 이어받지 않고 새 세션을 시작하며, 이전 대화의 마지막 부분을 입력 앞에 글로 붙인다(ADR-0034 의 대체 경로와 같다). 머리말은 `[이 역할의 프롬프트가 바뀌어 새 대화로 시작한다. 아래는 저장된 이전 대화의 마지막 부분이다]` 다. 그래서 승격하면 기존 스레드도 다음 메시지부터 새 프롬프트를 쓴다.
  - 남는 점: 그 스레드의 도구 호출과 결과는 새 세션에 넘어가지 않는다. 글로 붙는 이전 대화는 최대 12,000자다.
  - 기록에 `prompt_snapshot` 이 없으면(다른 CLI 버전 등) 비교할 수 없어 이전처럼 이어받는다.
- **기록**: 실제 쓴 버전은 이미 런타임 로그(`task <ID>: prompt <이름> v<버전>, model …`)와 Langfuse trace(`langfuse.observation.prompt.name`/`version`)에 남는다. 프롬프트가 바뀌어 새 세션을 시작하면 그 사실도 로그에 남긴다.

### 8. 프롬프트 읽기 (구현 세부, 2026-11-23)
- 후보를 만들 수 있는 역할(`tools.platform: [prompt-candidates]`)은 플랫폼 도구 `read_prompt` 로 역할 프롬프트의 본문, Config, 버전, label 을 읽는다. 기본은 `production` 이고, label 이나 version 하나를 고를 수 있다.
- core 가 `POST /v1/tasks/{id}/prompts/read` 에서 임대, 권한, 대상이 역할의 프롬프트인지를 확인하고 자기 Langfuse 키로 읽는다. 쓰지 않으며 키는 core 밖으로 나가지 않는다(I-36).
- 목적: `propose_prompt` 는 본문 전체를 받으므로, 원문을 읽지 못하면 옮겨 적다 틀릴 수 있다.

## 근거
- core 는 이미 승인 흐름(ADR-0020)과 되돌리기(ADR-0023)를 맡고, 소유자 DM 큐와 버튼 처리가 있다. 같은 구조를 그대로 쓴다.
- Langfuse 에 쓰는 곳이 core 하나면, `production` 을 옮기는 경로가 "소유자 버튼 → 커넥터 → core" 하나뿐이라는 것을 코드와 API 역할로 확인할 수 있다.
- 승격 직전 `production` 버전을 다시 확인하면, 소유자가 본 diff 와 실제로 바뀌는 내용이 같다. ADR-0020 이 승인을 커밋 SHA 에 묶은 것과 같은 이유다.
- 이 확인과 label 변경 사이에는 Langfuse 에 조건부 갱신이 없어 아주 짧은 틈이 남는다. 그 틈에 다른 사람이 Langfuse 화면에서 `production` 을 바꾸는 일은 단일 사용자 환경에서 드물다.

## 결과
- 불변식 I-36 을 추가한다.
- ADR-0007 의 프롬프트 경로 중 "누가 후보를 만드는가"(개발 에이전트)를 이 ADR 이 대체한다(부분 대체). `candidate` label, Slack 승인, `production` 승격, 라벨로 되돌리기는 그대로다.
- 역할 정의 형식에 `tools.platform` 이 생긴다(ADR-0005 의 도구 허용 목록을 넓힌 것).
- core 가 Langfuse 공개 API 를 부른다. core 는 LLM 호출 경로에 들어가지 않는다(I-23).
- **배포 순서**: 계약이 늘어난 두 곳은 받는 쪽이 먼저 새 버전이어야 한다.
  - 역할 정의의 `tools.platform`: core 가 작업과 함께 런타임에 보낸다. 새 core 가 이전 런타임에 보내면 이전 런타임은 응답을 해석하지 못해 멈추고, 그 작업은 임대가 끝난 뒤 새 런타임이 다시 처리한다(시도 횟수 하나를 쓴다). core 는 배포 전에 마이그레이션 Job 을 돌리므로 보통 런타임보다 늦게 바뀐다.
  - 새 알림 종류(`prompt-promotion-requested`): 이전 커넥터는 해석하지 못한다. 새 core 와 새 런타임이 있어야 생기므로, 배포가 끝나기 전 몇 분 동안 이 기능을 쓰지 않으면 된다.

## 재검토 조건
- Langfuse 가 읽기 전용 키나 label 조건부 갱신을 지원할 때.
- 프롬프트 평가 파이프라인이 생길 때(§6).
- 프롬프트를 쓰는 역할이 공용·격리 말고 다른 실행 방식을 가질 때(반영 시점 안내).
