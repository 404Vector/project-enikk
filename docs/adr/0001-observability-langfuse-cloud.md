# ADR-0001: LLM 관측 도구로 Langfuse Cloud(Hobby) 사용

- 상태: 승인
- 날짜: 2026-11-21
- 관련: I-06, I-07

## 배경
에이전트(Slack → 에이전트 → LLM / MCP 도구)의 트레이싱, 프롬프트 관리, 평가, 사용자 피드백 수집이 필요하다.
로컬 클러스터(Docker Desktop: 16 CPU, 메모리 약 15GiB)는 Langfuse self-host 공식 최소 사양을 감당하기 어렵다.

## 검토한 대안
- **Langfuse Cloud Hobby**: 무료. 기능이 가장 완결적. 데이터가 외부(Langfuse 서버)에 저장됨.
- **Langfuse self-host**: 무료(MIT), 사용량 무제한. 단, Web·Worker·Postgres·Redis·ClickHouse·S3가 필요하고 공식 최소 사양 합계가 약 11 CPU / 25.5GiB.
- **Arize Phoenix self-host**: 가벼움(컨테이너 1개 + DB). 피드백 점수, 프롬프트 버전 관리 등 운영 기능의 성숙도가 낮음.
- **MLflow, Opik, Helicone, LangSmith, Braintrust 등**: 범위·라이선스·무게 면에서 우선순위가 낮음.

## 결정
**Langfuse Cloud Hobby 플랜(무료)** 을 사용한다.

## 근거
2026-11-21 확인. 출처: https://langfuse.com/pricing , https://langfuse.com/self-hosting
- Hobby: 월 5만 unit, 조회 기간 30일, 사용자 2명, 카드 등록 불필요. 한도 초과 시 추가 과금 없이 해당 월 수집 중단.
- unit = Langfuse로 보내는 기록 하나(trace, observation, score 각각 1).
- 대화 1건당 10~20 unit 가정 시 월 2,500~5,000건. 개인 프로젝트에 충분.

## 결과
- 인프라 부담 없이 LLMOps 기능을 바로 사용한다.
- Slack 메시지와 응답 원문이 Langfuse Cloud에 저장된다.
- 계측은 OpenTelemetry 기준, Langfuse는 수신 백엔드로만 취급한다 (I-06).
- Langfuse 전용 기능(프롬프트 관리, 피드백 점수, 데이터셋)은 별도 포트 뒤에 둔다.
- 전송 전 마스킹 훅을 둘 수 있는 구조로 만든다 (초기 비활성).

## 재검토 조건
- 월 사용량이 5만 unit을 지속적으로 넘을 때 → Core($29) 또는 self-host 검토.
- 민감 데이터를 다루게 될 때 → 마스킹 활성화 또는 self-host 전환.
