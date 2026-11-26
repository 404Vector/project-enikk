# ADR-0043: Gemini 이미지 생성 도구 서버와 화풍

- 상태: 승인
- 날짜: 2026-11-25
- 관련: ADR-0004, ADR-0013, ADR-0016, ADR-0032, ADR-0037, ADR-0039, ADR-0041, ADR-0042, I-05, I-09

> 결정은 2026-11-25 소유자가 Slack 스레드에서 확정했다(기본 모델, 하루 상한, 도구 범위, Slack 전달, Langfuse 트레이싱, 화풍을 Langfuse 에 두는 3층 구조).

## 배경

- 소유자는 게임 에셋 이미지를 에닉이 만들기를 원했다. 에닉에는 이미지를 만드는 도구가 없었다.
- Google 의 이미지 모델(Nano Banana 계열)은 API 키로 쓰고 장당 값을 낸다. 구독 계정의 OAuth 로 API 를 부르는 것은 Google 약관에 어긋나 제외했다.
- 외부 자격증명은 그것을 쓰는 MCP 서버만 가져야 한다(I-09). 엔진은 agentgateway 를 통해서만 MCP 도구를 쓴다(I-05).
- 게임 에셋은 화풍이 일정해야 한다. 화풍 설명을 부르는 쪽(에닉, 나중의 다른 에이전트)의 지시문에 두면 옮겨 적다 빠지고 버전이 남지 않는다.

## 검토한 대안

- **runtime 안에서 Gemini 를 직접 부르는 플랫폼 도구**: 서버가 하나 줄어든다. 그러나 runtime 이 Gemini 키를 갖게 되어 I-09 에 어긋난다. → 기각.
- **외부의 Gemini MCP 서버를 그대로 띄움**: 하루 상한, 파일 저장 위치 제한, 트레이싱, 화풍을 우리 규칙대로 넣기 어렵다. → 기각.
- **이미지 전용 MCP 서버 `imagegen` 을 gateway 뒤에 둠**: 스케줄러(ADR-0039)와 같은 길이다. → 채택.
- 화풍의 위치: 저장소 파일(바꿀 때마다 PR), 공유 볼륨 파일(버전·승인 없음), **Langfuse 프롬프트 `style-*`**(버전, 후보·승격 절차를 그대로 씀). → Langfuse 채택.

## 결정

### 1. 서버
- 새 앱 `enikk-imagegen`(`src/apps/imagegen`, 차트 `deploy/helm/imagegen`). gateway 의 `/mcp/imagegen` 으로만 닿는다. 도구 상한은 gateway 설정과 에이전트 정의(`agents/enikk.yaml`)가 함께 정한다.
- 도구: `generate_image`, `edit_image`, `list_styles`, `add_style_reference`, `remove_style_reference`.
- 서버는 누가 부르는지 모른다. 부르는 세션은 gateway 가 core 의 토큰에서 넣어 주는 `x-enikk-session-id` 로만 안다(ADR-0041 §3).
- 새 역할 `tool`: core 에서 자기 자격증명만 받는다. 다른 core API 는 부르지 못한다. 역할 바인딩은 `enikk-imagegen` 에 `gemini.api-key`, `langfuse.public-key`, `langfuse.secret-key` 를 준다.

### 2. 모델, 상한, 비용
- 기본 모델은 Nano Banana 2(`gemini-3.1-flash-image`). Pro(`gemini-3-pro-image`)는 호출이 이름을 댈 때만 쓴다. 허용 모델 목록 밖의 모델은 거부한다.
- ~~하루 상한 100장(Asia/Seoul 자정 기준, 차트 값 `dailyLimit`, 0 이면 생성 중지).~~ ADR-0045 로 대체: 하루 상한은 추정 비용(USD)이고, 값은 Langfuse 도구 설정 `config-imagegen` 에 둔다. 세는 값은 메모리에만 있어 서버가 다시 뜨면 0 부터 센다. 넘치는 위험은 Google Cloud 의 결제 한도로 막는다.
- 장당 추정 비용을 응답과 트레이스에 적는다. 값은 설정의 가격표이고, 실제 청구는 Google 의 토큰 단위다.

### 3. 화풍 (3층 구조)
- 1층, 도구: `imagegen` 은 화풍과 부르는 쪽을 모른 채 이미지를 만든다.
- 2층, 화풍: Langfuse 프롬프트 `style-<이름>`. 본문은 화풍 설명, 설정은 `model`·`description` 만 허용한다. 참조 이미지는 공유 볼륨의 `styles/<style-이름>/` 에 둔다(최대 6장). 도구가 `style` 을 받으면 서버가 설명과 참조 이미지를 붙인다. production 만 읽고 60초 동안 캐시한다.
- 화풍의 생성·수정은 스킬과 같은 후보·승격 절차(ADR-0032)를 쓴다. `propose_prompt` 가 `style-*` 를 받고, 승격 카드에 "(화풍)" 으로 표시된다.
- 3층, 부르는 쪽: 지금은 에닉이 스킬(`skill-game-art`, Langfuse)로 부른다. 별도 에이전트는 ADR-0041 §2(A2A)를 구현할 때 `imagegen` 도구만 가진 에이전트 정의로 만든다. 서버와 화풍은 바꾸지 않는다.

### 4. 파일
- 서버는 runtime 의 공유 볼륨(ADR-0037 §7, `enikk-runtime-claude-data`)을 같은 노드에서 함께 마운트한다.
- 결과는 부른 스레드의 `tmp/<세션>/images/` 에 저장한다. 입력 이미지는 그 스레드의 `tmp` 와 `worktrees` 아래에서만 읽는다. 다른 스레드의 파일은 읽지 않는다.
- Slack 전달은 ADR-0042 의 `send_file` 로 한다.

### 5. 트레이싱
- 요청마다 Langfuse 트레이스 하나(`generation`)를 부른 세션의 Langfuse 세션에 남긴다: 설명문, 모델, 해상도, 비율, 참조 이미지 수, 화풍과 그 버전, 토큰 수, 추정 비용, 저장 경로, 실패 사유.
- 이미지 바이트는 Langfuse 에 보내지 않는다. 게임 에셋을 외부 서비스에 쌓지 않기 위해서다.

## 근거

- Google AI Studio 가격 문서(2026-11-25 확인, https://aistudio.google.com/docs/pricing): `gemini-3.1-flash-image` 1K $0.067, 2K $0.101, 4K $0.151, 512 $0.045. `gemini-3-pro-image` 1K·2K $0.134, 4K $0.24. 100장/일은 기본 모델 1K 로 약 $6.7.
- 스케줄러가 이미 gateway 뒤 MCP 서버, core 토큰, 세션 헤더의 틀을 갖췄다(ADR-0039). 같은 틀을 쓰면 새 보안 경계가 생기지 않는다.

## 결과

- Gemini 키는 `imagegen` 만 받는다. 소유자가 머지 뒤 `enikk-ops secret set gemini.api-key` 와 `enikk-ops secret sync` 로 넣는다. 그 전까지 서버는 키를 기다린다.
- `imagegen` 은 공유 볼륨 전체를 마운트한다. 다른 스레드의 파일을 읽지 않는 것은 서버 코드(`files.py`)가 지킨다. 볼륨에 비밀값은 없다(ADR-0037 §7).
- 하루 상한은 재시작에 약하다. 정확한 누적이 필요해지면 core 에 기록한다.
- 런타임이 RWO 볼륨을 쓰므로 노드가 둘 이상이 되면 이 구조는 다시 봐야 한다.

## 되돌리기

- 차트 값 `enabled: false` 로 서버를 멈춘다. 에이전트 정의에서 `imagegen` 항목을 빼면 도구가 사라진다.

## 재검토 조건

- 노드가 둘 이상이 되거나 공유 볼륨의 접근 모드가 바뀔 때.
- 이미지 비용이 예산을 넘거나, 하루 상한을 재시작 너머로 지켜야 할 때.
- 별도 그림 에이전트를 만들 때(3층의 전환).
- Google 의 모델 이름이나 가격이 바뀔 때.
