# ADR-0052: Google Drive 폴더로 파일을 주고받는 도구 서버

- 상태: 승인
- 날짜: 2026-11-28
- 관련: ADR-0004, ADR-0013, ADR-0037, ADR-0039, ADR-0041, ADR-0043, ADR-0044, I-05, I-07, I-09

> 결정은 2026-11-28 소유자가 Slack 스레드에서 확정했다(Drive 를 가운데 둠, PC 쪽은 Drive for Desktop 동기화, 소유자 본계정, 교환 도구는 REST 로 직접 하고 구글 공식 MCP 는 이 서버 뒤에 나중에 붙임, 파일당 100MB, 삭제 도구 없음, 에닉·리타에게 권한).

## 배경

- 에닉의 클러스터(맥북)와 소유자 윈도우 PC 의 Unity 에디터 사이에 파일을 옮길 길이 없었다. Unity 도구(ADR-0044)는 명령을 보낼 뿐이고, 파일 내용을 도구 인자에 실으면 바이트가 모델의 맥락을 지나 토큰이 든다(2MB 이미지는 base64 로 약 2.7MB 의 문자열).
- 소유자는 Google Drive 를 가운데 두기를 원했고, 나중에 본계정 Drive 의 편의 기능도 붙일 계획이다.
- 외부 자격 증명은 그것을 쓰는 MCP 서버만 갖고(I-09), 비밀값은 core 의 비밀값 API 로만 나간다(I-07). 클러스터에는 NetworkPolicy 가 없어 샌드박스의 셸에서도 서비스에 직접 닿는다(ADR-0044 §5).

## 검토한 대안

- **구글 공식 Drive MCP(`drivemcp.googleapis.com`)를 gateway 에 바로 붙임**: 도구를 구글이 유지한다. 그러나 요청마다 OAuth 토큰이 필요해 gateway 가 비밀값을 가져야 하고(I-09 위반), 폴더 제한이 없으며 `trash_file`·`update_file` 이 있다. 본계정이라 드라이브 전체가 열린다. → 기각.
- **중계 서버가 구글 MCP 로 파일 바이트까지 옮김**: 바이트가 JSON-RPC 메시지에 base64 로 실린다. 공식 문서에 다운로드 형식과 업로드 상한이 적혀 있지 않다(2026-11-28 확인). → 바이트 경로로는 기각. 편의 기능의 백엔드로는 §7 에 남긴다.
- **Unity 가 Drive API 를 직접 부름(PC 에 토큰)**: 동기화 지연이 없다. 그러나 리타가 `execute_code` 로 에디터 안에서 임의 코드를 돌릴 수 있어 토큰이 에이전트 손에 닿는다. → 기각.
- **공개 링크로 내려받기**: PC 에 토큰이 필요 없다. 그러나 받는 방향만 되고 링크가 열린 동안 에셋이 외부에 노출된다. → 기각.
- **볼트를 교환 통로로 씀**: 새 연동이 필요 없다. 볼트가 윈도우에 동기화되는지 확인되지 않았고 바이너리를 git 에 쌓는다. → 기각.
- **Drive 전용 도구 서버 `drive` 를 gateway 뒤에 둠**: imagegen(ADR-0043)·Unity 중계(ADR-0044)와 같은 틀이다. → 채택.

## 결정

### 1. 흐름
- enikk → Unity: 에이전트가 스레드의 파일을 `drive_put` 으로 Drive 의 교환 폴더(기본 `enikk`)에 올린다. 소유자 PC 의 Drive for Desktop 이 그 폴더를 로컬 폴더로 동기화하고, Unity 도구가 거기서 읽어 `Assets/` 로 가져온다.
- Unity → enikk: Unity 도구가 PC 의 교환 폴더에 파일을 쓰고, 동기화된 뒤 에이전트가 `drive_get` 으로 스레드의 tmp 에 받는다.
- 동기화에는 수 초에서 수 분이 걸린다. 에이전트는 `drive_list` 나 Unity 쪽의 파일 확인으로 도착을 본다.
- PC 쪽 준비는 소유자가 한다: Drive for Desktop 에서 교환 폴더를 미러링하거나 '오프라인 사용 가능'으로 둔다(스트리밍 상태로는 Unity 가 못 읽을 수 있다).

### 2. 서버와 도구
- 새 앱 `enikk-drive`(`src/apps/drive`, 차트 `deploy/helm/drive`). gateway 의 `/mcp/drive` 로만 닿는다. 역할은 `tool`.
- 도구: `drive_put(path, dest, overwrite)`, `drive_get(path)`, `drive_list(folder)`. 결과에는 경로·크기·수정 시각만 있고 파일 바이트는 없다.
- 바이트는 서버가 공유 볼륨(ADR-0037 §7)과 Drive 사이에서 직접 옮긴다. 올리기는 그 스레드의 `tmp`·`worktrees` 아래 파일만 읽고, 받기는 `tmp/<세션>/drive/` 아래에만 쓴다.
- Drive v3 REST 를 httpx 로 부른다(구글 SDK 없음). 올리기는 재개 가능 업로드(8MiB 조각), 받기는 파일로 흘려 쓴다. 같은 이름이 있으면 `overwrite=true` 일 때만 새 판(revision)으로 바꾼다.
- 삭제 도구는 두지 않는다. 지우는 일은 소유자가 한다.
- 한 번에 옮기는 파일은 100MB 까지(차트 값 `maxFileMb`). 비용이 들지 않아 하루 상한은 두지 않는다.

### 3. 폴더 제한
- 토큰은 소유자 본계정의 `drive` 전체 범위다. `drive.file` 범위로는 앱이 만들지 않은 파일, 곧 PC 에서 넣은 파일을 읽지 못한다.
- 그래서 제한은 서버 코드가 지킨다: 모든 경로는 My Drive 맨 위의 교환 폴더(차트 값 `rootFolder`)에서 이름으로 한 단계씩 찾는다. `..`·빈 이름·역슬래시·제어 문자는 거부한다. 바로가기(shortcut)는 따라가지 않고, Google 문서는 받지 않는다. 같은 이름이 둘 이상이면 거부한다.
- 교환 폴더가 없으면 처음 쓸 때 만든다. 같은 이름의 폴더가 맨 위에 둘 이상이면 거부한다.

### 4. 토큰 검사
- 샌드박스의 셸에서 서버에 직접 닿을 수 있으므로 ADR-0044 §5 와 같이 한다. gateway 는 검사한 작업 토큰을 `x-enikk-mcp-token` 으로 이 서버에만 넘기고, 서버는 core 의 공개 키(`/v1/mcp/jwks`)로 서명·발급자·대상·만료를 확인한다. 도구 호출은 토큰이 `drive:<도구>` 를 담을 때만 한다.
- 부른 세션은 헤더가 아니라 확인한 토큰의 `session` 으로 정한다.
- 검사 코드는 `enikk_unity.tokens` 를 옮겨 왔다(앱끼리는 import 하지 않는다). 세 번째 서버가 같은 검사를 쓰게 되면 공용 라이브러리로 옮긴다.

### 5. 인증과 비밀값
- 비밀값 셋: `drive.oauth-client-id`, `drive.oauth-client-secret`, `drive.oauth-refresh-token`. 역할 바인딩은 `enikk-drive` 에만 준다(I-09).
- 소유자는 Google Cloud 에서 Drive API 를 켜고, OAuth 동의 화면을 만들고, '데스크톱 앱' OAuth 클라이언트를 만든다. 동의 화면은 '프로덕션' 상태로 둔다. '테스트' 상태에서 받은 refresh token 은 7일 뒤 만료된다.
- 클라이언트 ID·시크릿은 `enikk-ops secret set` 으로 넣는다. refresh token 은 `enikk-ops drive-auth` 로 받는다: PKCE 와 127.0.0.1 루프백으로 브라우저 동의를 받고, 토큰을 화면에 내지 않고 비밀값 파일에 바로 쓴다. 그 뒤 `enikk-ops secret sync`.
- 서버는 refresh token 으로 짧은 access token 을 받아 만료 1분 전까지 쓴다. 401 이 오면 한 번 새로 받아 다시 시도한다. 오류 메시지에 토큰을 넣지 않는다.

### 6. 권한
- `drive_put`·`drive_get`·`drive_list` 를 에닉(ADR-0041)과 리타(Unity 작업)에 준다. 아르카나에게는 주지 않는다.

### 7. 넓히는 절차 (편의 기능)
- 본계정 Drive 의 편의 기능(검색, 문서 읽기 등)은 이 서버에 더한다. 토큰은 계속 이 서버만 갖는다.
- 구글 공식 Drive MCP 를 이 서버 뒤의 백엔드로 붙이고, 고른 도구만 내보낸다(예: `search_files`, `read_file_content`, `get_file_metadata`, `list_recent_files`). 서버가 토큰을 끼우고, 허용 폴더 밖의 결과를 걸러 내며, `trash_file`·`update_file`·권한 도구는 막는다. 그때 `tools/list` 로 실제 스키마와 상태(미리보기 여부, 승인 질문 방식)를 확인한다.
- 허용 폴더를 늘리거나 문서·시트처럼 다른 범위가 필요하면 설정을 바꾸고 `drive-auth` 로 다시 동의받는다. 각각 이 ADR 을 고치거나 새 ADR 로 정한다.

## 근거

- 구글 Drive MCP 레퍼런스(2026-11-28 확인, https://developers.google.com/workspace/drive/api/reference/mcp): 도구 15개(`/mcp/v1`)와 승인 질문이 없는 클라이언트용 12개(`/mcp/v1self`). `trash_file`·`update_file` 포함. 다운로드 형식, 업로드 상한, 범위, 출시 상태는 문서에 없다.
- '테스트' 상태 OAuth 앱의 refresh token 7일 만료: Google Ads API 포럼의 구글 측 답변이 구글 문서의 규칙을 인용한다(2026-11-28 확인, https://groups.google.com/g/adwords-api/c/Z_kihrf6VCE). 구글 공식 문서에서 다시 확인하지 못했다.
- imagegen 과 Unity 중계가 gateway 뒤 서버, 역할 `tool`, core 의 비밀값, 토큰 재검사의 틀을 이미 갖췄다. 새 보안 경계가 생기지 않는다.

## 결과

- 토큰은 소유자 Drive 전체에 닿는다. 교환 폴더 밖으로 나가지 않는 것은 서버 코드와 그 테스트가 지킨다. 서버 코드의 결함이나 토큰 유출은 본계정 Drive 전체의 노출이 된다.
- 동기화 지연은 소유자 PC 와 Drive for Desktop 에 달려 있다. PC 가 꺼져 있으면 Unity 쪽에 늦게 도착한다.
- 공유 볼륨을 함께 마운트하므로(RWO, 한 노드) ADR-0043 §4 와 같은 제약을 받는다.
- 토큰 검사 코드가 `enikk_unity` 와 `enikk_drive` 에 하나씩 있다.

## 되돌리기

- 차트 값 `enabled: false` 로 서버를 멈춘다. 에이전트 정의에서 `drive` 항목을 빼면 도구가 사라진다. 구글 계정의 '서드 파티 액세스'에서 앱의 접근을 끊으면 토큰이 무효가 된다.

## 재검토 조건

- 편의 기능을 붙일 때(§7).
- 노드가 둘 이상이 되거나 공유 볼륨의 접근 모드가 바뀔 때.
- 구글이 `drive.file` 범위로 PC 쪽 파일을 읽을 길을 주거나, 공식 MCP 가 바이트 전송과 폴더 제한을 지원할 때.
- 동기화 지연이 작업에 문제가 될 때.
