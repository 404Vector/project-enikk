# ADR-0005: 에이전트 정의 공통 형식

- 상태: 승인 (Langfuse 쪽 책임(I-16)은 ADR-0037 로 부분 대체: 스킬 config 의 `requires`)
- 날짜: 2026-11-21
- 관련: ADR-0003, ADR-0006, ADR-0037, I-10, I-13, I-14, I-15, I-16

> 결정 2 의 "Langfuse 쪽 변경으로는 권한을 바꿀 수 없다"는 ADR-0037(2a 구현 세부)이 부분 대체한다. 스킬 프롬프트(`skill-*`)의 config 에는 그 스킬이 쓰는 core 경유 동작(`requires`)을 선언한다. 이 선언은 소유자의 승격으로만 `production` 이 되고, 넓히는 변경은 승격 카드에 경고가 뜬다. 역할 정의 파일의 도구 허용 목록과 위임 대상은 여전히 git 에서만 바뀐다.

## 배경
ADR-0003 에 따라 엔진은 에이전트 정의 파일에서 정한다. Claude Agent SDK 와 Google ADK 는 정의 형식이 다르므로,
프로젝트 공통 형식을 두고 실행 시 엔진별 형식으로 변환한다.

## 검토한 대안
- **형식**
  - Markdown + YAML 머리말(본문 = 프롬프트): 처음 승인했으나, ADR-0006 으로 프롬프트를 git 에 둘 수 없게 되어 근거가 사라짐. 본문을 설명용으로 쓰면 "설계·설명은 docs/" 원칙과 겹침. → 대체.
  - **순수 YAML**: 채택.
- **모델·생성 파라미터 위치**
  - 정의 파일(git): 한 파일에서 모두 보이지만, 프롬프트와 함께 움직여야 할 값이 배포 주기에 묶임.
  - **Langfuse 프롬프트 `config`**: 채택. 프롬프트·모델·파라미터를 하나의 버전 단위로 관리.

## 결정
1. 형식은 **순수 YAML**, 위치는 최상위 `agents/<name>.yaml`.
2. **git 과 Langfuse 의 책임 분리**

   | 위치 | 담는 것 | 성격 |
   |---|---|---|
   | git (정의 파일) | 이름, 설명, 엔진, 프롬프트 참조, 도구 허용 목록, 위임 대상, 실행 한도 | 권한·구조. 코드 리뷰 대상 |
   | Langfuse (프롬프트 + `config`) | 프롬프트 본문, 모델 ID, 생성 파라미터 | 품질 튜닝. 배포 없이 버전 관리·평가·롤백 |

   Langfuse 쪽 변경으로는 에이전트의 권한(도구, 위임 대상)을 바꿀 수 없다 (I-16).
3. 실행 시점에 **엔진과 모델 ID 의 호환성**을 검증하고, 맞지 않으면 실행을 거부한다 (I-16).
4. 프롬프트 본문은 정의 파일에 넣지 않고 이름·라벨(또는 버전)로 참조한다 (ADR-0006, I-13).
5. Slack 연결 규칙은 정의 파일과 분리된 인터페이스 쪽 설정에 둔다 (I-15).
6. 엔진 내장 도구는 기본 전부 차단, 정의에 명시한 것만 허용한다 (I-14).
7. MCP 도구는 허용 목록 방식이다.
8. 정의는 로딩 시 도메인 객체로 검증하며, 에디터 검증용 JSON Schema 를 함께 제공한다.

## 필드 (초안)

```yaml
name: image-designer                 # 고유 ID
description: 요청에 맞는 이미지를 생성하고 수정한다
engine: gemini                       # claude | gemini
prompt:
  name: image-designer               # Langfuse 프롬프트 이름 (모델·파라미터는 그 config 에)
  label: production                  # 또는 version: 7
tools:
  mcp:
    - server: storage
      allow: [upload_file]
  builtin: []                        # 기본 전부 차단
delegates: []
limits:
  max_turns: 20
  timeout: 10m
engine_options: {}                   # 공통 필드로 표현할 수 없는 엔진 고유 설정 (권한 관련 설정은 금지)
```

세부 스키마는 구현 단계에서 JSON Schema 로 확정한다.
