# ADR-0044: 소유자 PC 의 Unity 에디터 MCP 를 중계 서버로 쓴다

- 상태: 승인
- 날짜: 2026-11-26
- 관련: ADR-0004, ADR-0013, ADR-0039, ADR-0041, ADR-0043, I-05, I-07, I-09, I-30

> 결정은 2026-11-26 소유자가 Slack 스레드에서 확정했다(도구 전부 허용, 한 PR, 매 호출 인스턴스 지정, 중계 서버안).

## 배경

- 소유자는 윈도우 PC 의 Unity 에디터에서 MCP for Unity(CoplayDev, 서버 `mcpforunityserver` 10.3.0)로 게임을 만든다. 에닉(맥북의 클러스터)도 그 에디터를 쓰기를 원했다.
- 네트워크는 윈도우가 핫스팟을 열고 맥북이 거기에 붙는 구조다. 서버는 `http://192.168.137.1:8765/mcp` 에서 HTTP 로 듣는다. 클러스터 Pod 에서 이 주소로 닿는 것을 확인했다(2026-11-26).
- 서버는 `X-API-Key` 헤더로 `tools/list`·`tools/call`·`resources/read` 를 검사한다. 세션(`Mcp-Session-Id`)을 쓰며 30분 유휴 뒤 끝난다. 활성 에디터는 `set_active_instance` 로 세션에 두거나 호출마다 `unity_instance` 인자로 정한다.
- 도구는 Unity 가 붙으면 48개다. 에디터 안에서 코드 실행(`execute_code`), 스크립트 생성·삭제, 메뉴 실행, 빌드가 있어 사실상 그 PC 사용자 권한으로 코드를 돌린다.
- 외부 자격증명은 그것을 쓰는 MCP 서버만 갖고(I-09), 비밀값은 core 의 비밀값 API 로만 나간다(I-07). agentgateway 는 비밀값을 갖지 않는다.

## 검토한 대안

- **gateway 가 `X-API-Key` 를 붙여 윈도우 서버로 바로 보냄**: 서비스가 하나 줄어든다. 그러나 gateway 는 core 에서 비밀값을 받을 수 없어 Secret 을 직접 마운트해야 하고, I-07·I-09 를 깬다. → 기각.
- **HTTP 를 그대로 넘기는 프록시(세션 유지)**: gateway route 를 stateful 로 두고 세션 만료 뒤 재연결을 엔진에 맡겨야 한다. → 기각.
- **MCP 수준의 중계 서버 `enikk-unity`**: 키는 이 서버만 core 에서 받는다. 요청마다 윈도우 서버와 짧은 세션을 연다. → 채택.

## 결정

### 1. 서버
- 새 앱 `enikk-unity`(`src/apps/unity`, 차트 `deploy/helm/unity`). gateway 의 `/mcp/unity` 로만 닿고, 엔진은 윈도우 주소를 모른다(I-05).
- 윈도우 서버 주소와 기본 인스턴스는 차트 값(`upstreamUrl`, `instance`)이다.
- 새 비밀값 `unity.api-key`(윈도우의 `enikk-macbook` 라벨 키). 역할 바인딩은 `enikk-unity` 에 이것 하나만 준다(역할 `tool`, ADR-0043).

### 2. 세션과 인스턴스
- gateway route 는 다른 서버처럼 stateless 다. 중계 서버는 받은 요청마다 윈도우 서버에 `initialize` → 요청 → 세션 종료를 한다. 윈도우 쪽 세션 상태와 유휴 만료에 기대지 않는다.
- `set_active_instance` 는 요청이 끝나면 사라지므로 쓰지 않는다. 호출이 `unity_instance` 를 받는 도구인데 값을 주지 않으면 중계 서버가 차트의 `instance` 를 넣는다. 호출이 값을 주면 그대로 둔다.

### 3. 도구
- 도구 상한 없이 MCP for Unity 10.3.0 의 48개를 모두 연다. gateway 상한과 `agents/enikk.yaml` 에 이름을 하나씩 적는다.
- 리소스는 도구 두 개로 연다: `list_unity_resources`(resources/list 와 templates/list), `read_unity_resource`(resources/read). gateway 의 허가는 도구 이름으로 하므로 리소스도 같은 토큰 검사를 받는다.
- 윈도우 쪽에서 그룹을 끄면 그 도구는 목록에서 빠진다. 패키지 버전을 올려 이름이 바뀌면 상한과 정의를 함께 고친다(버전은 10.3.0 고정).

### 4. 신뢰와 실패
- 서버의 instructions 와 도구 결과는 신뢰하지 않는 입력이다. 중계 서버는 윈도우 서버의 instructions 를 넘기지 않고 자기 안내문을 쓴다.
- 윈도우·핫스팟·에디터가 꺼져 있으면 도구 호출은 오류 결과(`isError`)로 끝난다. 중계 서버의 준비 상태는 윈도우와 상관없다.

## 근거

- 윈도우 쪽 보고와 클러스터 Pod 에서의 시험(2026-11-26): `initialize` 200, 키 없는 `tools/list` 거부("API key authentication required").
- 이미지 도구(ADR-0043)가 gateway 뒤 MCP 서버, 역할 `tool`, core 의 비밀값으로 같은 틀을 쓴다. 새 보안 경계가 생기지 않는다.

## 결과

- 키를 가진 `enikk-unity` 와 그 도구를 받은 세션은 소유자 PC 에서 코드를 돌릴 수 있다. 도구는 `enikk` 정의에만 있다.
- 핫스팟 안의 통신은 HTTP 평문이다. 키와 내용은 핫스팟 암호(WPA2)가 지킨다.
- 소유자는 머지 뒤 `enikk-ops secret set unity.api-key` 와 `enikk-ops secret sync` 로 키를 넣는다. 그 전까지 서버는 키를 기다리며 준비되지 않는다.
- 요청마다 `initialize` 가 하나 더 든다. 같은 LAN 안이라 지연은 작다.

## 되돌리기

- 차트 값 `enabled: false` 로 서버를 멈춘다. 에이전트 정의에서 `unity` 항목을 빼면 도구가 사라진다.

## 재검토 조건

- 윈도우 PC 가 핫스팟이 아닌 다른 네트워크로 옮기거나 주소가 바뀔 때.
- MCP for Unity 버전을 올릴 때(도구 이름).
- Unity 도구만 가진 별도 에이전트를 만들 때(ADR-0041 §2).
