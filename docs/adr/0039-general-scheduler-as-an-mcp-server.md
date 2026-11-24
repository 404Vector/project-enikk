# ADR-0039: 일반 스케줄러 — MCP 서버, 에이전트가 대화로 관리하고 자기 범위 밖은 소유자 승인

- 상태: 승인 (S1·S2 구현. 남은 단위 S3~S7 은 아래 "이행 순서")
- 날짜: 2026-11-24
- 관련: ADR-0004, ADR-0023, ADR-0028, ADR-0032, ADR-0035, ADR-0036, ADR-0037, I-01, I-05, I-16, I-23, I-24, I-26, I-35, I-38

> 결정은 2026-11-24 소유자가 Slack 스레드에서 확정했다. 바뀌는 ADR·불변식의 대체 표시는 실제로 바뀌는 단계에서 단다(ADR-0037 의 관례).

## 배경
- 날짜별 대화 요약(ADR-0035)은 core 안의 전용 루프(`_digest_forever`)와 전용 정의(`agents/summarizer.yaml`)로 돈다. 예약 작업을 하나 더 넣으려면(예: 세계·한국 경제 이슈 요약) 같은 것을 또 만들어야 한다.
- 소유자는 스케줄을 대화로 만들고 고친다. 대화로 자주 바뀌므로 변경마다 PR 을 내는 것은 맞지 않다(소유자).
- 스케줄은 에이전트가 직접 쓰는 기능이어야 하고, MCP 사양으로 만든다(소유자).
- ADR-0004·I-05 는 MCP 를 agentgateway 로만 쓰기로 정했다. 그러나 main `3c7a1e8` 기준 gateway 는 배포되지 않았고, 엔진은 정의의 `tools.mcp` 를 읽지 않는다(`enikk_engine_claude.engine` 은 runtime 안의 플랫폼 서버만 붙이고 `strict_mcp_config=True`).
- ADR-0037 §3: 스케줄 작업은 스킬을 미리 불러온 세션으로 연다. 5b 에서 기본 프롬프트는 `enikk`, 요약 스킬은 `skill-conversation-summary` 가 되었다.

## 검토한 대안
- **스케줄을 git(`config/schedules.yaml`)에 둔다**: 권한 변경이 PR 로만 일어나 안전하다. 그러나 변경마다 PR 이 필요하다. → 기각(소유자).
- **core 안 모듈**: workflow 변경이 없고 상주 자원도 늘지 않는다. 그러나 core 는 MCP 호출 경로에 있을 수 없다(I-23). → 기각.
- **Kubernetes CronJob**: 스케줄마다 매니페스트가 필요하고 대화로 바꿀 수 없다. → 기각.
- **runtime 이 MCP 서버에 직접 연결(ADR-0004 개정)**: gateway 가 필요 없다. 그러나 MCP 서버가 늘 때 인증과 허용 목록이 흩어진다. → 기각(소유자).
- **MCP 도구를 플랫폼 도구로 다시 감싼다**: MCP 도구는 에이전트가 그대로 쓰고 권한은 정의의 `tools.mcp` 로 준다. 감쌀 이유가 없다. → 기각.
- **대화 요약 스케줄만 도구를 넓히지 못하게 막거나 승인 대상으로 둔다**: 예약 세션은 스케줄을 바꿀 수 없으므로, 남는 위험은 도구가 있는 대화 세션이 속아 요약 스케줄에 도구를 붙이는 경우뿐이다. 변경 알림의 경고로 충분하다. → 기각(소유자: 모든 스케줄을 똑같이 다룬다).
- **SDK 서브에이전트로 원문을 격리**: 서브에이전트에 원문을 넘기는 본 세션이 먼저 읽게 되고, 부를지 말지를 모델이 정한다. 격리가 되지 않는다. → 기각.

## 결정

### 1. 스케줄러 서비스
- `enikk-scheduler` 는 시계(언제 무엇을 열지)와 MCP 도구를 가진 별도 서비스다.
- 스케줄과 실행 기록의 원본은 core DB 다(I-26). 스케줄러는 core API(새 서비스 역할 `scheduler`)로만 읽고 쓰며, 작업도 core 큐에 넣는다.
- core 는 MCP 호출 경로에 없다(I-23). 방향은 runtime → gateway → 스케줄러 → core 다.

### 2. MCP 경로 (ADR-0004 의 실현)
- agentgateway 를 `enikk` 네임스페이스에 Argo 앱으로 배포한다(업스트림 이미지).
- 엔진은 에이전트 정의의 `tools.mcp` 에 적힌 서버와 허용 도구만 gateway 로 붙인다. 정의에 없는 서버·도구는 보이지 않는다(I-14).
- **호출자 신원**: runtime 이 실행 중인 작업 ID 와 임대를 담은 토큰을 붙인다. 스케줄러는 core 에 그 임대가 살아 있는지, 어느 에이전트·세션인지 확인한 뒤에만 동작한다(지금 플랫폼 도구가 하는 확인과 같다).

### 3. 스케줄 하나에 담는 것
- 이름, 설명, 시각(cron 5필드 + 시간대, 또는 한 번만)
- 에이전트, 미리 불러올 스킬(`skill-*`)
- 도구: 그 에이전트 정의의 부분집합
- 입력: 코드에 등록된 종류만. `text`(고정 문장), `day-conversations`(그날 대화, 비밀값 가림, 신뢰하지 않는 입력)
- 출력: 코드에 등록된 종류만. 모두 소유자에게만 간다. `owner-dm`, `thread`(만든 스레드), `vault-note`(`Private/enikk/` 아래 정해진 폴더), `discard`
- 놓친 회차 채우기, 재시도 횟수, 켜짐·꺼짐

### 4. MCP 도구
- 조회: `list_schedules`, `get_schedule`, `list_runs`
- 변경: `create_schedule`, `update_schedule`, `pause_schedule`, `resume_schedule`, `delete_schedule`
- 실행: `run_now`. 결과를 원래 출력 대신 이 스레드에 올리는 선택이 있다(소유자가 대화에서 시험).
- 플랫폼 도구 `summarize_day` 는 `run_now` 로 대체한다.

### 5. 권한
- **바로 되는 것(자기 범위)**: 대상 에이전트가 호출한 에이전트 자신이고, 주기가 10분 이상인 변경. 스케줄 수에는 제한이 없다.
- **소유자 승인**: 다른 에이전트를 대상으로 하거나 주기가 10분보다 잦은 변경. 승인 채널에 카드와 [승인]·[반려] 가 가고(ADR-0036), 소유자만 누른다(I-01). 승인 전에는 `pending` 이고 돌지 않는다.
- **언제나 거부**: 에이전트 정의를 넘는 도구, 등록되지 않은 입력·출력. 승인으로도 넓힐 수 없다(I-16).
- **누가 바꾸나**: 소유자가 Slack 에서 시작한 세션만 바꾼다(기억 쓰기와 같은 기준, I-35). 예약으로 열린 세션은 조회만 한다. 신뢰하지 않는 입력에 속은 예약 세션이 스스로 스케줄을 늘리는 길을 막는다.
- **알림**: 변경마다 그 스레드에 한 줄 알림이 간다. 신뢰하지 않는 입력을 쓰는 스케줄에 도구가 더해지면 그 줄에 경고를 붙인다. 막지는 않는다.

### 6. 실행
- 시각이 되면 스케줄러가 core 에 작업을 넣는다. 커넥터 종류는 `schedule`, 실행마다 새 세션이다.
- core 는 에이전트 정의를 스케줄의 도구로 좁혀 runtime 에 보낸다.
- runtime 은 기본 프롬프트 `enikk` 와 미리 불러온 스킬 본문을 시스템 프롬프트로 쓴다. 불러온 스킬과 버전은 트레이스에 남는다(ADR-0037 2a).
- 출력은 core 가 쓴다. 볼트 쓰기는 기존 기억 쓰기 경로(경로 제한, `draft: true`, 비밀값 확인)를 지난다. frontmatter 의 `by` 는 그 에이전트의 이름이다(대화 요약은 `enikk`).
- 실행 기록은 (스케줄, 회차)마다 상태 하나다. 중복이 없고, 실패하면 재시도하며, 끝내 실패하면 소유자 DM 에 한 줄을 보낸다(ADR-0035 §6 을 일반화).

### 7. 대화 요약
- 이 스케줄러의 작업 하나다. 에이전트 `enikk`, 스킬 `skill-conversation-summary`, 도구 없음, 입력 `day-conversations`, 출력 `vault-note`(`대화/YYYY-MM-DD.md`), 매일 04:00 Asia/Seoul.
- 다른 스케줄과 똑같이 다룬다. 처음 등록은 배포 뒤 core 가 한 번만 넣는다. 그 뒤로는 대화로 관리한다.
- 요약 스킬은 다른 스킬처럼 [스킬] 목록에 보이고 대화에서 불러 시험할 수 있다.

## 근거
- 스케줄을 원본으로 core DB 에 두면 대화로 바꿀 수 있고, 권한은 "에이전트 정의를 넘지 못한다"는 규칙으로 git 에 묶인 채 남는다. 스케줄이 할 수 있는 일의 상한은 언제나 git 의 정의다.
- 되돌릴 수 없는 결과(볼트 쓰기, Slack 게시)는 모델이 아니라 core 의 출력 처리기가 정해진 곳에만 한다. 그래서 신뢰하지 않는 입력이 바꿀 수 있는 것은 결과 글뿐이다.
- 예약 세션이 스케줄을 바꿀 수 없으면 웹 글이나 대화 원문 속 지시가 스스로를 반복 실행하게 만드는 길이 없다.
- MCP 는 gateway 하나로 모아야 서버가 늘어도 인증과 허용 목록을 한곳에서 본다(ADR-0004).

### 외부 사실 (확인 2026-11-24, 구현 전에 다시 확인한다)
- agentgateway 는 JWKS 로 JWT 를 검증하고 클레임으로 접근을 허용·거부할 수 있다. https://agentgateway.dev/docs/kubernetes/1.0.x/security/jwt/mcp/ , https://agentgateway.dev/docs/kubernetes/latest/mcp/mcp-access/
- 호출자 토큰을 백엔드로 그대로 넘기는 방식은 제3자 예제에서만 확인했다(https://github.com/rvennam/agentgateway-auth-patterns). Kubernetes ServiceAccount 토큰으로 runtime 을 인증하고 작업 토큰을 스케줄러까지 넘길 수 있는지는 공식 문서로 확인하지 못했다.

## 결과
- 상주 서비스가 둘(gateway, 스케줄러) 는다. 노드 메모리 여유는 확인하지 못했다.
- 새 이미지(`enikk-scheduler`)는 `.github/workflows/images.yml` 의 matrix 에 더해야 한다. 스레드 세션은 workflow 를 바꿀 수 없으므로(ADR-0037 2b) 소유자가 직접 고친다.
- 구현 때 바꾸는 것(대체 표시는 그 단계에서):
  - I-05: 근거에 이 ADR 을 더한다.
  - I-24: 스케줄은 core DB 가 원본이다(설정이 아니라 소유자의 데이터). 예외로 적는다.
  - I-38·ADR-0035: "대화 요약은 도구 없는 스케줄로 실행한다. 신뢰하지 않는 입력을 쓰는 스케줄에 도구가 더해지면 변경 알림에 경고가 붙는다. core 는 입력과 결과 모두에서 비밀값을 가린다."
  - 새 불변식: 스케줄의 도구는 에이전트 정의를 넘지 못한다. 스케줄은 소유자가 Slack 에서 시작한 세션만 바꾼다. 자기 범위 밖의 스케줄은 소유자 승인 없이 돌지 않는다.
  - 민감 경로: 스케줄러의 권한 검사 코드와 gateway 매니페스트.

### 이행 순서 (각 단위 한 시간 안)
1. **S1**: core 의 스케줄·실행 기록 테이블(expand 마이그레이션), API, 서비스 역할 `scheduler`.
   - 구현: 마이그레이션 0013(`schedules`, `schedule_runs`), `enikk_contracts.schedules`, `enikk_core_domain.schedules`, `enikk_core.schedules`, `/v1/schedules*` 엔드포인트 7개(`@requires("scheduler")`). I-24 에 예외를 적고 I-39 를 더했다.
   - 요청마다 호출한 작업 ID 와 임대를 받아 core 가 확인한다(§2 의 호출자 신원 중 core 쪽). 바꾸는 요청은 Slack 세션만 된다.
   - 범위 밖 변경은 `pending` 으로만 저장한다. 승인 카드와 변경 알림은 S4 에서 붙인다. `enikk-scheduler` 의 역할 바인딩(core values `roleBindings`)은 서비스가 생기는 S2 에서 더한다.
   - 최소 주기는 cron 의 하루 안 실행 시각 간격과 자정을 넘는 간격 중 가장 짧은 것으로 본다. 다음 날이 허용되지 않아도 허용된다고 보므로 실제보다 짧게 나올 수는 있어도 길게 나오지는 않는다.
2. **S2**: 스케줄러 서비스 골격(조회 도구), 이미지. 소유자가 workflow matrix 에 한 줄을 더한다.
   - 구현: 앱 `src/apps/scheduler`(`enikk_scheduler`). 시계도 이 서비스에 들어가므로 `mcp-<name>` 이 아닌 `scheduler` 로 이름 지었다. MCP 공식 Python SDK 의 streamable HTTP(상태 없음, JSON 응답)로 `/mcp` 에 조회 도구 3개를 둔다. `/healthz` 는 프로브용이다.
   - 호출자 신원은 요청 헤더 `x-enikk-task-id`·`x-enikk-lease-id` 로 받아 core 에 그대로 넘긴다. 확인은 core 가 한다(S1). 헤더를 누가 붙이는지(runtime·gateway)는 S3 에서 정한다.
   - Host 헤더는 서비스 이름만 받는다(DNS rebinding 방어).
   - 배포: 차트 `deploy/helm/scheduler`(Deployment, Service), Argo 앱, ServiceAccount `enikk-scheduler`, Image Updater 항목, core `roleBindings` 의 `role: scheduler`. 이미지 태그가 없거나 `enabled: false` 이면 복제본 0 이다.
3. **S3**: agentgateway 배포, 엔진의 `tools.mcp` 연결, 호출자 신원. `agents/enikk.yaml` 에 스케줄러 조회 도구를 허용한다.
4. **S4**: 변경 도구, 권한 검사, 승인 카드(connector), 변경 알림과 경고.
5. **S5**: 시계와 실행(스킬 미리 불러오기, 도구 좁히기, 출력 처리기, 재시도), `run_now`.
6. **S6**: 경제 이슈 요약. 소유자가 대화로 직접 만들어 첫 실사용 시험으로 삼는다.
7. **S7**: 대화 요약 이전. `agents/summarizer.yaml`·`_digest_forever`·`summarize_day` 를 없애고 I-38·ADR-0035 에 대체 표시를 단다. 옛 `conversation_digests` 테이블은 마이그레이션 0004·0005 정리 때 지운다(I-34).

### 되돌리기
- S1~S5: 기능 플래그(스케줄러 `enabled`, 엔진의 MCP 연결)로 끈다. 그동안 대화 요약은 옛 경로로 돈다.
- S7: 이전 이미지와 매니페스트로 되돌린다(ADR-0023). 옛 루프가 같은 날을 다시 만들어도 같은 파일을 덮어쓰므로 중복은 없다.

## 재검토 조건
- 스케줄이 많아져 시계 하나로 부족할 때.
- MCP 서버가 늘어 gateway 정책이 복잡해질 때.
- agentgateway 가 호출자 신원 전달을 지원하지 않는 것으로 확인될 때 → 신원 전달 방식 또는 §2 를 다시 정한다.
