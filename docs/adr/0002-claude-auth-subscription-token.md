# ADR-0002: Claude 인증에 본인 구독 토큰 사용 (개인 전용)

- 상태: 승인
- 날짜: 2026-11-21
- 관련: ADR-0003, ADR-0004, I-01, I-02, I-03, I-04, I-07

## 배경
이 프로젝트는 소유자 1인만 사용하는 개인 프로젝트다. 소유자는 Claude 구독(Pro/Max)을 보유하고 있으며,
`claude setup-token` 으로 발급한 토큰으로 에이전트를 구동하고자 한다.

## 검토한 대안
- **본인 구독 토큰 (`CLAUDE_CODE_OAUTH_TOKEN`)**: 추가 비용 없음. 사용 범위가 정책으로 제한됨.
- **Console API 키 (`ANTHROPIC_API_KEY`)**: 서버형 에이전트의 공식 권장 방식. 사용량만큼 별도 과금.
- **클라우드 제공사 (Bedrock / Vertex 등)**: API 키와 같은 성격. 클라우드 계정 결제.

## 결정
- **본인 구독 토큰**을 사용한다. 단, 아래 조건을 지킨다.
  1. 사용자는 소유자 본인 1명뿐이다 (I-01).
  2. 토큰은 Claude Code / Claude Agent SDK 런타임에서만 쓴다 (I-02, I-03).
  3. 모델 호출은 게이트웨이를 거치지 않고 Anthropic으로 직접 보낸다 (ADR-0004).
- 인증 방식은 **설정 하나로 API 키로 전환**할 수 있게 만든다 (I-04).

## 근거
2026-11-21 확인. 출처:
https://code.claude.com/docs/en/authentication.md ,
https://code.claude.com/docs/en/agent-sdk/overview.md ,
https://code.claude.com/docs/en/legal-and-compliance.md ,
https://code.claude.com/docs/en/llm-gateway.md

- `claude setup-token`: 구독으로 인증하는 1년 유효 OAuth 토큰. CI·스크립트 등 브라우저 로그인이 불가한 환경용.
- 금지 대상은 **제3자에게 제품으로 제공**하거나 **다른 사용자를 대신해** 구독 자격증명으로 요청을 중계하는 경우다.
  개인 단독 사용은 여기에 해당하지 않는다.
- "Pro, Max, Ultra 사용량 한도는 Claude Code 와 Agent SDK 의 일반적인 개인 사용을 전제로 한다"는 문구가 있다.
  즉 개인의 Agent SDK 구독 사용은 상정된 사용 형태다.
- Anthropic은 사전 통지 없이 정책을 집행할 수 있다고 명시한다.

## 결과
- 추가 비용 없이 구동한다. 사용량은 소유자의 Claude Code 사용과 같은 구독 한도(5시간 / 주간)를 공유한다.
- 대량 무인 자동화는 "일반적 개인 사용"을 넘을 수 있으므로 피한다.
- 토큰은 1년마다 재발급이 필요하다.

## 재검토 조건
- 다른 사람이 사용하게 될 때 → **즉시** API 키 또는 클라우드 제공사로 전환 (새 ADR).
- Anthropic 정책 변경, 구독 한도 부족, 자동화 비중 증가.
