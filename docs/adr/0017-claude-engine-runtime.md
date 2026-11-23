# ADR-0017: Claude 엔진 런타임 — 도구 차단, 대화 이어 가기, 트레이스

- 상태: 승인 (공용 역할의 도구 차단과 토큰 전달 방식은 ADR-0033 으로 부분 대체)
- 날짜: 2026-11-22
- 관련: ADR-0002, ADR-0005, ADR-0013, ADR-0014, ADR-0015, ADR-0016, I-02, I-08, I-14, I-25, I-28

> 공용 역할은 기본 도구를 갖지 않는다(`tools=[]`)는 부분과 구독 토큰을 환경변수 `CLAUDE_CODE_OAUTH_TOKEN` 으로 넘긴다는 부분은 ADR-0033 이 대체한다(부분 대체). 이제 모든 역할이 정의에 적은 기본 도구를 Landlock 샌드박스 안에서 쓰고, 토큰은 파일 디스크립터로 CLI 에만 넘긴다. 정의에 없는 도구가 열리면 중단하는 검증은 그대로다.

## 배경
첫 엔진 런타임인 `runtime-claude` 를 Claude Agent SDK(Python, 0.2.187, Claude Code CLI 동봉)로 만든다.

구현 전에 로컬 WSL 에서 실제 SDK 로 다음을 확인했다(2026-11-22).
- **인증**: 구독 토큰을 환경변수 `CLAUDE_CODE_OAUTH_TOKEN` 으로 넘기면 응답한다. 시작 메시지의 `apiKeySource` 는 `none` 이다.
- **도구 차단**: `tools=[]` 이면 시작 메시지의 도구 목록이 `[]` 이다. "파일 목록을 보여 달라"고 해도 도구가 없다고 답한다.
  - 설치된 스킬과 내장 서브에이전트 이름은 목록에 나오지만, 이를 부르는 도구(`Skill`, `Agent`)가 없으므로 쓸 수 없다.
- **다른 Pod 에서 이어 가기**: `SessionStore` 를 쓰면, `CLAUDE_CONFIG_DIR` 을 새로 만든 프로세스도 `resume` 으로 같은 대화를 이어 간다. 세션 ID 는 바뀌지 않는다.
- **트레이스**: 런타임 프로세스에서 만든 OTel generation 스팬에 프롬프트 속성을 붙이면, Langfuse 가 프롬프트 버전(`promptId`)과 연결한다.

## 검토한 대안
- **Claude Code 자체 OTel 트레이스만 쓰기**: 베타 기능이고, 스팬에 Langfuse 프롬프트 속성을 붙일 방법이 없다. → 쓰지 않는다. 필요하면 나중에 하위 스팬으로 추가한다. SDK 는 활성 스팬의 trace context 를 CLI 에 넘긴다.
- **Langfuse 의 Claude Agent SDK 연동(OpenInference 계측)**: 의존성이 늘고 전역 계측에 의존한다. 우리가 만든 한 개의 generation 스팬으로 필요한 정보(입출력, 모델, 사용량, 프롬프트 버전, 세션)를 모두 남길 수 있다. → 기각.

## 결정

### 1. 엔진 실행 (`enikk_engine_claude.ClaudeEngine`)
- 옵션은 다음과 같다.
  - 도구와 권한: `tools=[]`, `permission_mode="dontAsk"`.
  - 외부 설정 차단: `setting_sources=[]`, `strict_mcp_config=True`.
  - 프롬프트 처리: `verbatim_prompts=True`. 메시지 안의 `@경로` 가 파일 읽기로 해석되지 않게 한다.
  - 모델과 지시문: 시스템 프롬프트와 모델은 Langfuse 프롬프트에서, 최대 턴 수는 에이전트 정의에서 가져온다.
  - 생성 파라미터: Langfuse config 의 `effort` 만 반영한다. 다른 키는 경고만 남긴다.
- 작업마다 임시 디렉터리를 만들어 `CLAUDE_CONFIG_DIR` 와 `HOME` 으로 쓰고, 작업이 끝나면 지운다. 런타임은 상태를 남기지 않는다(I-25).
- 구독 토큰은 CLI 하위 프로세스의 환경변수로만 넘긴다. `ANTHROPIC_API_KEY` 는 비운다(I-02).
- **도구 검증**: 시작 메시지의 도구 목록이 비어 있지 않으면 즉시 중단하고 재시도하지 않는 실패로 보고한다(I-14).
- 실패 분류(ADR-0015)는 다음과 같다.

| 상황 | 재시도 |
|---|---|
| 대화 이력 저장 실패(`MirrorErrorMessage`) | 예 |
| CLI 프로세스 오류 | 예. 사유에는 오류 종류만 넣는다 |
| 최대 턴·예산 초과 | 아니오 |
| API 오류: 400·401·403·404 | 아니오 |
| API 오류: 그 밖(429, 5xx 등) | 예 |

- 응답이 40,000자를 넘으면 잘라 내고 표시를 붙인다.

### 2. 대화 이력 (`CoreSessionStore`)
- SDK `SessionStore` 의 `append`, `load`, `list_subkeys` 를 core 의 대화 이력 API(ADR-0014)로 구현한다. 호출에는 작업의 임대 ID 를 쓴다.
- SDK 가 제공하는 SessionStore 적합성 검사(`run_session_store_conformance`)를 테스트에서 통과시킨다.
- CLI 작업 디렉터리는 모든 Pod 에서 `/work` 로 고정한다. 대화 이력의 `project_key` 가 이 경로에서 만들어지기 때문이다.

### 3. 트레이스 (`enikk_langfuse.LangfuseTracing`)
- 작업마다 트레이스를 하나 만든다.
  - 부모 스팬은 `enikk.task` 이다.
  - 자식으로 generation 스팬 하나를 둔다. 여기에 입력, 출력, 실제 모델, 사용량(캐시 포함), 프롬프트 이름과 버전, core 세션 ID 를 기록한다.
  - 실패하면 두 스팬을 ERROR 로 표시하고 사유를 남긴다.
- 런타임 전용 `TracerProvider` 를 쓴다. 전역 OTel 상태는 건드리지 않는다.
- 대화 내용(입력·출력)은 소유자의 Langfuse 프로젝트에 남는다(ADR-0001).

### 4. 배포
- `deploy/helm/runtime-claude` 로 배포한다.
  - ServiceAccount 는 `enikk-runtime-claude` 이다.
  - Kubernetes API 토큰은 마운트하지 않는다. audience=`enikk-core` projected token 만 쓴다(I-28).
  - 루트 파일시스템은 읽기 전용이다. `/tmp`, `/work` 는 emptyDir 이다.
  - Secret 은 마운트하지 않는다. 비밀값은 시작할 때 core 에서 받는다(ADR-0013). core 가 아직 그 값을 갖고 있지 않으면 받을 때까지 백오프하며 기다린다.
- Argo CD 앱(wave 20)과 Image Updater 대상, 이미지 빌드 대상에 추가한다.
- 배포 후 확인은 `enikk-ops smoke claude` 로 한다. 커넥터 SA 토큰으로 한 스레드에 메시지 두 개를 보내고, 두 번째 답이 첫 메시지의 코드워드를 기억하는지 본다. 실제 모델을 호출한다.

## 근거
- 실험으로 확인한 동작만으로 구성했다. 도구 차단은 옵션에 기대지 않고, 매 실행의 시작 메시지로 다시 검증한다.
- 런타임이 만든 스팬 하나로 Langfuse 의 프롬프트, 세션, 비용 화면이 모두 채워진다.

## 결과
- 구독 사용량은 작업마다 소모된다. 모델은 Langfuse 프롬프트 config 로 바꾼다(I-16).
- CLI 동봉으로 이미지가 커진다.
- SDK 가 바뀌면(옵션 이름, 시작 메시지 형식) 도구 검증 테스트와 smoke 로 확인한다.

## 재검토 조건
- MCP 도구를 붙일 때(M4). `tools=[]` 를 유지한 채 MCP 서버만 허용하는 구성을 검증한다.
- Claude Code 트레이스를 하위 스팬으로 보고 싶어질 때.
