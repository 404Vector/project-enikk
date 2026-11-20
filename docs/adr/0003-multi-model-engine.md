# ADR-0003: 에이전트 정의 단위로 Claude / Gemini 엔진을 선택하는 멀티 모델 구조

- 상태: 승인
- 날짜: 2026-11-21
- 관련: ADR-0002, ADR-0004, I-05, I-08, I-10, I-11, I-12

## 배경
- Claude 만으로는 이미지 생성 등 일부 작업이 불가능하다.
- 작업 성격에 따라 Claude 가 나은 작업과 Gemini 가 나은 작업이 있으며, 작업 단위로 모델을 선택하고 싶다.
- Claude 는 구독 토큰을 쓰므로 Claude Code / Agent SDK 경로로만 호출해야 한다 (ADR-0002).

## 검토한 대안

### 구조
- **A. Gemini 를 MCP 도구로만 연결**: Gemini 가 작업 전체를 주도하는 경우를 표현할 수 없다. → 범위가 좁아 기각.
- **B. 엔진 추상화 + 에이전트 정의 기반 선택**: 채택.
- **C. 범용 LLM 게이트웨이(LiteLLM 등)로 모델 통일**: 구독 토큰을 게이트웨이로 중계해야 하므로 ADR-0002 와 충돌. → 기각.

### 엔진 선택 주체
- 규칙 기반 라우터 / 사용자 명시 / LLM 판단 / **에이전트 정의 파일** 중 에이전트 정의 파일을 채택.

### Gemini 엔진 구현체
- **Gemini CLI**: Claude Code 와 유사한 터미널 에이전트(Apache-2.0). 헤드리스 모드(`-p`, `stream-json`), `GEMINI_API_KEY`, MCP(streamable HTTP), 서브에이전트 정의 파일, OpenTelemetry 를 지원한다. 그러나 다음 이유로 기각.
  - 앱에 내장하는 공식 SDK 가 없다. 서브프로세스로 감싸는 방법뿐이다.
  - 현재 대상이 Code Assist 조직 사용자로 좁혀졌고, 무료 티어 / Google One 사용자는 2026-06-18 부로 Antigravity CLI 로 이전되었다.
- **Google ADK**: 채택. Python 라이브러리로 직접 내장 가능하다.
  - AI Studio API 키 인증(`GOOGLE_API_KEY`)을 지원한다.
  - MCP 도구(`McpToolset` + streamable HTTP)를 지원하므로 agentgateway 에 그대로 연결된다.
  - 선언형 YAML 에이전트 설정과 하위 에이전트(`sub_agents`)를 지원한다.
  - OpenTelemetry GenAI 시맨틱 컨벤션으로 트레이스를 내보낸다.

### Gemini 인증
- **Google AI Studio API 키**를 채택.

## 결정

1. 도메인 코어는 `ModelEngine` 포트만 안다. 구현체는 두 가지다.
   - **ClaudeEngine**: Claude Agent SDK, 구독 토큰 (ADR-0002)
   - **GeminiEngine**: Google ADK, AI Studio API 키
2. **엔진은 에이전트 정의 파일에서 정한다.**
   - 에이전트 정의 = 프롬프트, 사용할 도구(MCP), 엔진, 모델 등을 담은 선언형 파일.
   - 런타임은 어떤 에이전트 정의를 실행할지만 고르고, 엔진은 그 정의를 따른다. 코드에 엔진 분기 로직을 두지 않는다 (I-10).
3. **맥락 관리**
   - Slack 스레드 하나는 처음 실행된 에이전트 정의 하나에 묶인다. 그 스레드의 대화 이력은 해당 엔진의 네이티브 세션으로 관리한다 (I-11).
   - 한 에이전트가 다른 엔진의 에이전트에 작업을 위임할 때(예: Claude 에이전트가 이미지 생성을 Gemini 에이전트에 위임), 대화 이력 전체가 아니라 **명시적인 입력과 출력만** 주고받는다 (I-12).
   - 따라서 엔진 사이에 대화 이력을 변환하는 문제는 생기지 않는다.

```
 Slack 스레드 ─► 에이전트 정의 선택 ─► 정의에 적힌 엔진으로 실행
                                       │
                 ┌─────────────────────┴─────────────────────┐
                 ▼                                           ▼
           ClaudeEngine                                 GeminiEngine
           (Agent SDK, 구독 토큰)                        (ADK, AI Studio 키)
                 │                                           │
                 │◄──── 위임: 명시적 입력 / 출력만 교환 ────►│
                 │                                           │
                 └──────────────── agentgateway (MCP) ───────┘
```

## 근거
2026-11-21 확인. 출처:
https://github.com/google-gemini/gemini-cli ,
https://geminicli.com/docs/cli/headless/ ,
https://geminicli.com/docs/get-started/authentication/ ,
https://adk.dev/ ,
https://adk.dev/tools-custom/mcp-tools/ ,
https://adk.dev/agents/config/ ,
https://adk.dev/observability/traces/

## 결과
- 도메인 코어는 특정 모델 공급사를 모른다 (I-08).
- 새 에이전트 = 정의 파일 추가. 새 모델 공급사 = 엔진 구현 1개 추가.
- 엔진마다 정의 파일 형식이 다르므로(Claude 서브에이전트 Markdown, ADK YAML), **프로젝트 공통 정의 형식**을 두고 엔진별 형식으로 변환한다. 공통 형식의 구체 스키마는 별도로 정한다.

## 후속 확인 필요
- Gemini API 약관(무료 / 유료 티어 차이, 무료 티어의 데이터 활용 범위)과 무료 티어 사용량 한도: https://ai.google.dev/gemini-api/terms
- ADK 트레이스를 Langfuse(OTLP)로 보낼 때의 인증 헤더 설정. ADK 문서에 Langfuse 언급은 없다.
- ADK 의 라이선스와 현재 버전.

## 재검토 조건
- 엔진 간 기능 차이가 커서 공통 포트나 공통 정의 형식으로 흡수하기 어려울 때.
- Google 이 ADK 대신 다른 공식 에이전트 SDK 를 권장하게 될 때.
