# ADR-0020: Slack 승인, 상태 검사 기반 머지, 배포 실패 대응

- 상태: 승인
- 날짜: 2026-11-22
- 관련: ADR-0007, ADR-0008, ADR-0018, ADR-0019, ADR-0021, ADR-0022, I-01, I-18, I-19, I-20, I-33

## 배경
ADR-0007 은 에이전트 PR 을 소유자가 승인해야 머지되도록 정했다. 소유자의 요구는 다음과 같다.

- 사람과의 상호작용은 Slack 에서 할 수 있는 것은 모두 Slack 에서 한다. 소유자는 GitHub 를 열지 않는다.
- 승인 요청은 소유자에게만 DM 으로 온다.
- 승인 전용 GitHub App 은 따로 만들지 않는다(ADR-0019).

GitHub 의 PR 리뷰 승인은 사람 계정으로만 할 수 있다. 소유자 계정의 토큰을 클러스터에 두면 에이전트가 자기 PR 을 승인할 통로가 생긴다(I-19).

## 검토한 대안
- **GitHub PR 리뷰로 승인**: GitHub 의 기본 기능이다. 그러나 소유자가 GitHub 를 열어야 한다. → 소유자 요구로 기각.
- **Slack 버튼 → core 가 소유자 토큰으로 GitHub 리뷰 승인**: 소유자 토큰이 클러스터에 들어온다. → 기각.
- **Slack 버튼 → core 가 필수 상태 검사 `enikk/owner-approval` 을 기록**: 채택.

배포 실패 대응은 다음을 비교했다.
- **RollingUpdate + 상태 확인**: 새 Pod 가 준비되기 전에는 이전 Pod 를 내리지 않는다. 추가 구성 요소가 없다. 실행은 되지만 동작이 틀린 경우는 잡지 못한다.
- **Argo Rollouts**: 분석 단계로 기능 이상까지 자동 판단해 되돌린다. 그러나 컨트롤러와 CRD 가 늘고, 모든 앱이 복제본 1개라 canary 가 의미 없다. 자동으로 되돌려도 git 은 여전히 새 이미지를 가리켜 revert 가 필요하다. → 기각.

## 결정

### 1. `main` 보호 규칙 (ruleset)
- PR 필수. GitHub 리뷰 승인 수는 0 이다.
- 필수 상태 검사:
  - 기존 CI 검사
  - 보호 경로 검사(I-20). 모든 PR 에서 실행되어야 한다(경로 필터를 걸지 않는다).
  - `enikk/owner-approval`. **출처를 App `enikk-deployer` 로 고정**한다. 같은 이름의 상태라도 다른 App 이나 GitHub Actions 가 기록하면 인정되지 않는다.
- 강제 push 와 브랜치 삭제를 막는다.
- **예외(bypass)는 두지 않는다.** 소유자 계정으로 만든 PR(소유자가 Claude Code 에 시킨 작업 포함)도 에이전트 PR 과 같은 Slack 승인을 거친다.
- 저장소 설정에서 auto-merge 를 허용한다.
- ruleset 설정값은 `deploy/github/` 에 JSON 으로 두고, 소유자 확인을 받아 `gh api` 로 적용한다. 저장소 설정은 GitOps 대상이 아니므로 I-18 의 부트스트랩 예외로 본다.
- `enikk/owner-approval` 은 승인 흐름이 동작하기 시작할 때 필수 검사에 추가한다. 그 전에는 PR 과 CI 통과만 요구한다.
- `main` 과 `agent/**` 밖의 브랜치는 저장소 관리자만 만들고 바꿀 수 있다(ruleset `agent-branches-only`). 그래서 App 은 `agent/**` 에만 push 할 수 있고, 소유자 브랜치에 커밋을 끼워 넣을 수 없다.
- 보호 경로 검사는 App 이 연 PR 과 `agent/` 브랜치의 PR 을 에이전트 변경으로 본다. 검사 목록은 워크플로 파일 안에 둔다. App 토큰에는 `workflows` 권한이 없으므로 에이전트가 목록을 바꿀 수 없다.

### 비상 시 수동 처리
- 승인 경로(core, connector-slack, Slack 앱, GitHub App 키)가 고장 나 Slack 으로 승인할 수 없으면, 소유자가 GitHub 설정에서 ruleset 을 직접 잠시 끄고 수정 PR 을 머지한 뒤 다시 켠다.
- 이 조작은 소유자만 한다. 에이전트와 Claude Code 는 ruleset 을 끄거나 바꾸지 않는다.
- 배포 실패 대응(아래 3)으로 이전 버전이 계속 서비스하므로, 승인 경로가 배포 때문에 끊기는 경우는 대부분 막힌다. 수동 처리는 그 밖의 경우(readiness 는 통과했지만 승인 처리가 틀린 배포, 토큰·키 만료 등)를 위한 것이다.

### 2. 승인 흐름
1. 개발 에이전트(ADR-0021) 또는 소유자 계정이 PR 을 만든다.
2. core 가 PR 을 감시한다. 클러스터는 외부에서 들어오는 연결을 받을 수 없으므로 webhook 대신 30초 간격으로 조회한다. 감시 대상은 `main` 으로 가는 모든 열린 PR 이다.
3. CI 가 끝나면 core 가 소유자에게 Slack DM 으로 승인 요청을 보낸다.
   - 내용: PR 링크, 제목과 요약(에이전트 PR 이면 원래 요청 포함), 바뀐 파일과 줄 수, CI 결과, diff 파일 첨부, [승인] [반려] 버튼.
   - CI 가 실패하면 승인 버튼 없이 실패를 알린다. 에이전트 PR 이면 원래 스레드로 개발 에이전트에 결과를 돌려준다.
4. [승인] 을 누르면:
   - connector 가 누른 사람이 소유자인지 확인한다(I-01). 버튼 값에는 PR 번호와 **요청 당시의 head 커밋 SHA** 가 들어 있다.
   - core 는 PR 의 현재 head 가 그 SHA 와 같을 때만, 그 커밋에 `enikk/owner-approval = success` 를 기록하고 auto-merge 를 켠다. 다르면 승인을 거부하고 새 승인 요청을 보낸다.
   - 상태는 커밋에 붙으므로, 승인 뒤 새 커밋이 push 되면 그 커밋에는 승인이 없어 머지되지 않는다.
5. [반려] 를 누르면 PR 을 닫고 브랜치를 지운다. 수정이 필요하면 소유자는 원래 스레드에서 assistant 에게 말한다(ADR-0022).
6. 머지되면 원래 스레드와 DM 에 알린다.

- 승인·반려 처리는 core 의 `approval` 모듈만 한다. 이 모듈은 Slack 의 소유자 동작으로만 호출되며, `dev-agent`·`runtime` 역할은 호출할 수 없다.
- Slack 앱에 Interactivity(Socket Mode 로 수신)와 `files:write` 권한을 추가한다.

### 3. 배포 실패 대응
- 모든 앱 Deployment 를 RollingUpdate(`maxUnavailable: 0`)로 둔다. 새 Pod 가 readiness 를 통과하지 못하면 이전 Pod 가 계속 서비스한다.
  - connector-slack 도 Recreate 에서 바꾼다. 잠깐 Socket Mode 연결이 둘이 되어도 이벤트 ID 기반 멱등 처리(ADR-0018)로 작업은 하나만 생긴다.
- Argo CD PostSync Job 이 `enikk-ops smoke core` 를 실행하고 결과를 core 에 보고한다. core 는 결과를 소유자 DM 으로 알린다.
- 실패하면 DM 에 [되돌리기] 버튼을 붙인다. 누르면 core 가 개발 에이전트에 "해당 머지를 revert 하는 PR" 작업을 만든다. 이 PR 도 같은 승인 흐름을 거친다.
- DB 마이그레이션은 이전 버전 코드와 호환되게만 한다(expand/contract). 이미지를 되돌려도 스키마는 되돌리지 않기 때문이다.

## 근거
- 상태 검사를 출처 App 으로 고정하면, 승인 기록 권한(`statuses: write`)이 없는 개발 토큰(ADR-0019)으로는 승인을 위조할 수 없다. GitHub Actions 가 기록한 상태도 인정되지 않는다.
- 승인을 커밋 SHA 에 묶으면, 소유자가 본 내용과 머지되는 내용이 같다.
- 소유자는 Slack 에서 승인부터 배포 결과 확인, 되돌리기까지 모두 할 수 있다.
- 예외를 두지 않으면 소유자 계정 토큰(Claude Code 포함)으로도 승인 없이 머지할 수 없다. 모든 변경이 같은 경로를 거친다.
- ruleset 의 상태 검사 출처 지정(`integration_id`), auto-merge 동작은 GitHub 문서에 근거한다. 이 ADR 을 쓰면서 문서를 다시 열어 보지는 않았고, ruleset 을 적용할 때 확인한다.

## 결과
- I-19 를 고치고 I-33 을 추가한다.
- 소유자가 Claude Code 로 진행하는 작업도 머지는 Slack 승인 뒤에 일어난다. Claude Code 는 PR 을 만든 뒤 승인을 기다린다.
- 승인 경로가 고장 나면 소유자가 GitHub 에서 수동으로 처리해야 한다.
- core 가 GitHub API 를 주기적으로 호출한다. 설치 토큰의 시간당 호출 한도 안에서 동작해야 한다.
- 실행은 되지만 동작이 틀린 배포는 스모크 검사가 잡는 범위까지만 잡힌다.

## 재검토 조건
- 스모크 검사를 통과한 뒤 발견되는 장애가 잦을 때 → Argo Rollouts 의 분석 단계 재검토.
- 클러스터가 외부 연결을 받을 수 있게 되면 → 조회 대신 webhook.
