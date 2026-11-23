# ADR-0019: GitHub App 신원과 용도별로 좁힌 설치 토큰

- 상태: 승인 (§2 용도별 토큰은 ADR-0037 로 부분 대체: core 가 쓰는 `submit` 토큰 추가)
- 날짜: 2026-11-22
- 관련: ADR-0007, ADR-0008, ADR-0013, ADR-0020, ADR-0021, ADR-0037, I-07, I-17, I-19, I-20, I-21, I-30, I-32

> §2 의 용도별 토큰에 ADR-0037(2b 구현 세부)이 `submit` 용도를 더한다. core 가 세션의 커밋을 자기 clone 에서 `agent/<세션>` 에 push 하고 PR 을 열 때 쓰며(`contents`·`pull_requests` 쓰기, `metadata` 읽기, 워크플로·상태·관리 권한 없음), core 밖으로 나가지 않고 쓰고 나면 바로 폐기한다. 개발 Pod 에 주는 `develop` 토큰은 4단계까지 그대로다.

## 배경
ADR-0007 은 개발 에이전트가 소유자 계정이 아닌 별도 GitHub 신원(GitHub App)으로 브랜치를 push 하고 PR 을 만들도록 정했다.
소유자가 App 을 만들었다.

- 이름 `enikk-deployer`, App ID `5869950`, Installation ID `184545127`.
- 소유자가 다른 용도로도 쓸 계획이라 **모든 저장소에 설치**했고, 권한도 이 프로젝트에 필요한 것보다 넓다.
- 승인 기록(ADR-0020)을 위해 App 을 하나 더 만들지는 않는다(소유자 결정).

따라서 이 App 의 개인 키는 소유자의 모든 저장소에 넓은 권한으로 접근할 수 있다.
이 프로젝트가 키를 어디에 두고, 누구에게 어떤 권한을 넘길지 정해야 한다.

## 검토한 대안
- **개발 에이전트 Pod 에 개인 키를 준다**: 구현이 가장 단순하다. 그러나 에이전트가 App 의 모든 권한(모든 저장소, 워크플로 수정, 상태 기록)을 갖게 되어 I-19, I-20 을 우회할 수 있다. → 기각.
- **승인 전용 App 을 따로 만든다**: 상태 기록 권한을 다른 키로 분리할 수 있다. 그러나 소유자가 관리할 App 과 키가 둘이 되고, 아래 방식으로 같은 효과의 대부분을 얻는다. → 소유자 결정으로 기각.
- **App 하나, 키는 core 만, 토큰은 용도별로 좁혀 발급**: 채택.

## 결정

### 1. 키와 식별값
- 개인 키는 비밀값 `github.app-private-key` 로 등록한다(ADR-0013). core 에만 마운트되고, **core 밖으로 내주지 않는다**. 어떤 역할 바인딩의 `credentials` 에도 넣지 않는다.
- App ID 와 Installation ID 는 비밀값이 아니므로 core Helm values 에 둔다.
- 키를 읽어 JWT 를 서명하고 설치 토큰을 받는 코드는 GitHub 어댑터 패키지 한 곳에만 둔다. import-linter 로 core 외의 앱이 이 패키지를 import 하지 못하게 막는다.

### 2. 용도별 토큰
설치 토큰은 `POST /app/installations/{id}/access_tokens` 로 받으며, 저장소와 권한을 App 권한보다 좁게 지정한다. 토큰은 1시간 뒤 만료된다.

| 용도 | 받는 쪽 | 저장소 | 권한 |
|---|---|---|---|
| 개발 | 개발 에이전트 Pod (core API 로 전달) | 작업 대상 저장소 **하나** | `contents: write`, `pull_requests: write`, `checks: read`, `statuses: read`, `metadata: read`, 정책이 허용하면 `workflows: write` |
| 승인 | core 의 승인 모듈 (core 밖으로 나가지 않음) | 승인할 PR 의 저장소 하나 | `statuses: write`, `pull_requests: write`, `contents: write`, `metadata: read` |
| 감시 | core 의 PR 감시 (core 밖으로 나가지 않음) | 관리 대상 저장소 전체 | `pull_requests: read`, `checks: read`, `statuses: read`, `contents: read`, `metadata: read` |
| 설정 | core 의 저장소 등록 (core 밖으로 나가지 않음) | 새로 등록하는 저장소 하나 | `administration: write`, `metadata: read` |

- 개발 토큰에는 다음 권한이 **없다**. 이것이 이 결정의 핵심이다.
  - `statuses: write`, `checks: write`: 승인 상태(ADR-0020)를 스스로 기록할 수 없다.
  - `administration`: 브랜치 보호 규칙(ruleset)을 바꿀 수 없다.
  - `workflows` (`project-enikk` 에서는 항상): GitHub 가 `.github/workflows/` 를 바꾸는 push 를 거부한다(I-20 의 두 번째 방어선).
- 토큰의 범위는 항상 저장소 하나다. 한 작업이 여러 저장소에 동시에 쓰지 않는다.

### 3. 관리 대상 저장소
에이전트가 작업할 수 있는 저장소는 `config/github.yaml` 의 목록에 있는 것뿐이다. App 이 설치된 다른 저장소는 대상이 아니다.

```yaml
repositories:
  - name: project-enikk
    self: true          # 이 플랫폼 자신. workflows 권한을 절대 주지 않고, 보호 경로 검사를 적용한다
  - name: some-new-app
    workflows: true     # 에이전트가 이 저장소의 CI 워크플로를 만들고 고칠 수 있다
```

- 목록에 저장소를 추가하는 것도 PR 이므로 소유자 Slack 승인을 거친다(ADR-0020).
- `self: true` 인 저장소는 하나뿐이며 `workflows` 를 켤 수 없다. core 가 설정을 읽을 때 검증한다.
- `self` 인 저장소의 이름은 core Helm values 의 `github.selfRepository` 와 같아야 한다. 목록 파일은 에이전트도 바꿀 수 있지만 core values 는 보호 경로이므로, 목록을 고쳐 이 플랫폼의 보호를 다른 저장소로 옮길 수 없다. (ADR-0024 이후: 두 파일 모두 민감 경로이며, 바꾸면 강화된 승인을 거친다. `self` 저장소의 `workflows` 는 켠다.)
- 다른 저장소의 `workflows` 는 기본값이 꺼짐이다. 켜면 에이전트가 그 저장소에서 승인 전에 Actions 를 실행할 수 있다. 영향은 그 저장소와 그 저장소의 비밀값에 한정된다.
- 목록에 추가된 저장소는 core 가 설정 토큰으로 `project-enikk` 과 같은 기본 ruleset(PR 필수, 강제 push·삭제 금지, 필수 검사 `enikk/owner-approval`, 예외 없음)을 적용한다. 그래서 모든 관리 대상 저장소의 `main` 머지가 같은 Slack 승인을 거친다.

### 4. 새 저장소 만들기
- 저장소는 소유자가 만든다. 에이전트는 이름과 설정을 미리 채운 생성 링크(`https://github.com/new?name=...`)를 Slack 으로 보내고, 소유자가 만들면 목록 추가 PR 부터 이어서 진행한다.
- App 이 모든 저장소에 설치되어 있으므로 새 저장소에도 바로 쓸 수 있다.
- 검토한 대안:
  - GitHub 조직에 App 을 설치해 App 이 직접 만들기: Slack 만으로 끝나지만 새 프로젝트가 조직 아래에 생긴다. → 소유자 결정으로 보류.
  - 소유자 계정 권한의 토큰을 클러스터에 두기: 에이전트가 소유자로 승인·머지할 길이 생긴다(I-19). → 기각.
- 근거: 개인 계정 아래 저장소 생성(`POST /user/repos`)에 대해 공식 문서는 OAuth 토큰과 classic PAT 만 언급하고, 설치 토큰 지원을 확인하지 못했다(2026-11-22).
- 승인 토큰의 `contents: write` 와 `pull_requests: write` 는 auto-merge 를 켜고, 반려된 PR 을 닫고 브랜치를 지우는 데 쓴다. `main` 에 직접 쓰는 것은 브랜치 보호 규칙이 막는다(ADR-0020).
- 개발 토큰은 core 의 `POST /v1/github/token` 으로 `dev-agent` 역할에만, 활성 임대를 가진 작업이 있을 때만 발급한다. 토큰 값은 로그와 트레이스에 남기지 않는다(I-07).
- 커밋 작성자는 App 의 봇 계정(`enikk-deployer[bot]`)이다.

## 근거
- 설치 토큰의 저장소·권한 축소와 1시간 만료는 GitHub REST 문서 "Create an installation access token for an app" 에서 확인했다(2026-11-22): 지정하지 않으면 설치된 모든 저장소와 App 의 모든 권한을 갖고, App 보다 넓게는 줄 수 없다.
- 토큰 범위는 GitHub 가 서버에서 강제한다. 개발 에이전트 코드나 프롬프트가 잘못돼도 토큰 밖의 권한은 쓸 수 없다.
- 키가 core 를 떠나지 않으면, 키 유출 경로는 core 손상과 소유자 PC 의 원본 파일뿐이다. 둘은 이미 모든 비밀값의 유출 경로다(ADR-0013).
- App 토큰으로 워크플로 파일을 push 하려면 `workflows` 권한이 필요하다는 점은 GitHub 문서에 근거한다. 이 부분은 문서를 다시 열어 보지 않았고, M7 구현에서 실제 push 로 확인한다.

## 결과
- 새 불변식 I-32 를 추가한다.
- **감수하는 위험**: App 이 모든 저장소에 넓은 권한으로 설치되어 있으므로, core 가 손상되면 소유자의 다른 저장소까지 영향을 받는다. 승인 기록과 개발 토큰이 같은 App 에서 나오므로, 토큰 범위를 정하는 core 코드가 틀리면 에이전트가 승인을 위조할 수 있다. 이 코드는 보호 경로(I-20)에 넣고 테스트로 범위를 고정한다.
- 소유자 준비물: `enikk-ops secret set github.app-private-key` 로 키를 등록한다. 값은 파일에서 읽어 들이는 방식을 추가한다.

## 재검토 조건
- App 의 용도가 늘어 이 프로젝트와 무관한 권한이 더 넓어질 때 → 이 프로젝트 전용 App 분리.
- 토큰 범위 코드에서 결함이 발견될 때 → 승인 전용 App 분리.
