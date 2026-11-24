# ADR-0021: 개발 에이전트는 작업마다 새 Pod(Job)로 실행한다

- 상태: 대체됨 (ADR-0037 4e: 개발 Job, `enikk-dev` 네임스페이스, dev-agent 앱을 없앰. 개발은 Slack 스레드의 세션이 worktree 에서 하고 push·PR 은 core 가 한다)
- 날짜: 2026-11-22
- 관련: ADR-0002, ADR-0007, ADR-0010, ADR-0011, ADR-0013, ADR-0017, ADR-0019, ADR-0020, ADR-0022, I-14, I-17, I-18, I-20, I-21, I-28

> §5·§7 에서 셸·파일 도구(`builtin`)를 격리 역할(developer)만 가질 수 있다고 한 부분은 ADR-0033 이 대체한다(부분 대체). 이제 모든 역할이 정의에 적은 기본 도구를 엔진의 Landlock 샌드박스 안에서 쓴다. 개발 Pod 의 구성과 감수한 GitHub 토큰 위험은 그대로다.

> §3·§7 에서 브랜치 push 와 PR 생성을 개발 Pod 만 한다고 한 부분은 ADR-0037(2b 구현 세부)이 부분 대체한다. 이제 공용 런타임의 스레드 세션도 core 를 거쳐 PR 을 낼 수 있다(core 가 push, 모델은 토큰 없음). 개발 Job 흐름 자체는 4단계에서 없앨 때까지 그대로 동작한다.

## 배경
ADR-0007 은 개발 에이전트를 작업마다 새로 만드는 격리된 Pod 에서 실행하도록 정했다.
개발 에이전트는 셸과 파일 편집 도구를 쓰고 저장소에 push 할 수 있으므로, 작업 사이에 상태가 남거나 다른 서비스의 자격증명에 닿으면 안 된다.

지금 Pod 를 만들 수 있는 주체가 없다. core 는 TokenReview 권한만 갖고(I-28), 다른 서비스는 Kubernetes API 토큰이 없다.

## 검토한 대안
- **전용 Deployment(복제본 1개), 작업마다 컨테이너 재시작**: Kubernetes 권한이 필요 없다. 그러나 emptyDir 가 컨테이너 재시작 사이에 남고, 격리 단위가 Pod 가 아니다. → 소유자 결정으로 기각.
- **KEDA ScaledJob**: core 에 권한을 주지 않아도 된다. 그러나 KEDA 가 core 의 대기 작업 수를 조회하려면 인증된 호출(I-28)이 필요해 구성이 늘어난다. → 기각.
- **core 가 전용 네임스페이스에만 Job 을 만든다**: 채택.

## 결정

### 1. 전용 네임스페이스 `enikk-dev`
- 개발 에이전트 Job 은 `enikk-dev` 네임스페이스에서만 실행한다.
- 이 네임스페이스에는 ServiceAccount `enikk-dev-agent` 하나만 두고, Secret 은 두지 않는다. 기존 `enikk` 네임스페이스의 같은 이름 SA 는 옮긴다.
- Pod Security Admission 을 `restricted` 로 강제한다.
- core 는 `enikk-dev` 안에서만 Job 을 만들고, 조회하고, 지울 수 있다(Role). Pod 는 조회만 한다.
  - 네임스페이스에 `enikk-dev-agent` 외의 SA 가 없으므로, core 코드가 잘못돼도 다른 서비스의 신원으로 Pod 를 띄울 수 없다.

### 2. Job 템플릿은 git 에 둔다
- Job 템플릿은 Helm 차트 `deploy/helm/dev-agent` 가 ConfigMap 으로 만들고, Argo CD 가 배포한다. 이미지 태그는 Image Updater 가 갱신한다.
- core 는 템플릿을 읽어 작업 ID 와 이름만 채운다. 이미지, SA, 보안 설정, 자원 한도, 실행 시간 한도는 바꾸지 않는다.
- Job 설정: `backoffLimit: 0`, `activeDeadlineSeconds` 는 에이전트 정의의 `limits.timeout_seconds` 에 여유를 더한 값, `ttlSecondsAfterFinished` 로 끝난 Job 을 정리한다.
- 컨테이너: 비루트, 루트 파일시스템 읽기 전용, 작업 폴더는 emptyDir. projected SA 토큰(audience `enikk-core`)만 마운트하고 Kubernetes API 토큰은 받지 않는다.

### 3. 실행 흐름
1. core 가 `developer` 에이전트의 작업을 접수하면 대기 상태로 둔다.
2. core 의 dispatcher 가 실행 제한(아래)을 확인하고 Job 을 만든다.
3. Pod 는 자기 작업 ID 로 core 에서 그 작업만 임대한다. 다른 작업은 임대할 수 없다.
4. core 에서 Claude 토큰, Langfuse 키, 개발용 GitHub 토큰(ADR-0019)을 받는다.
5. 저장소를 작업 폴더에 clone 하고 브랜치 `agent/<작업 ID>` 에서 작업한 뒤, 로컬 검사(ruff, mypy, import-linter, pytest)를 통과하면 push 하고 PR 을 만든다.
6. 결과(PR 번호 또는 실패 이유)를 core 에 보고하고 종료한다.

- Job 이 실패하거나 시간 한도를 넘으면 core 가 작업을 실패로 처리하고 원래 스레드에 알린다.

### 4. 실행 제한
- 동시에 실행되는 개발 Job 은 1개다.
- 하루 실행 횟수 상한을 둔다(기본 10). 넘으면 위임을 거부하고 그 사실을 스레드에 알린다.
- 두 값은 core Helm values 에 둔다.
- **변경 (소유자 결정, 2026-11-23)**: 동시 실행 상한을 10개로 올리고, 하루 상한은 기본으로 두지 않는다. 코드는 동시 실행을 10까지 받고, `devAgent.dailyLimit` 이 비어 있으면 하루 상한을 적용하지 않는다. 차트 값은 이 코드가 배포된 뒤 따로 바꾼다(I-34). Job 하나가 메모리 512Mi 를 요청하므로 10개는 노드에 들어가지만, 한도(3Gi)는 노드 메모리를 넘겨 잡혀 있다.

### 5. 이미지와 정의
- 이미지 `enikk-dev-agent` 는 runtime-claude 와 같은 엔진 코드에 git, uv 를 더한 것이다. CI 가 같은 방식으로 빌드한다.
- 에이전트 정의 `agents/developer.yaml` 이 셸·파일 편집 도구를 허용하는 유일한 정의다(I-14).

### 6. 공용 런타임과의 분리 (구현 세부, 2026-11-22)
- 에이전트 정의에 `execution: shared | isolated` 를 둔다(기본 `shared`). 작업은 접수될 때 자기 에이전트의 값을 기록한다.
- 공용 런타임(엔진별 Deployment)은 `shared` 작업만 임대한다. 임대 직전에 정의가 `isolated` 로 바뀌었으면 그 작업을 실패로 처리한다.
- 격리 Job 은 `POST /v1/tasks/{id}/claim` 으로 자기 작업 하나만 임대한다(`dev-agent` 역할). heartbeat, 결과, 대화 이력 API 는 두 역할이 함께 쓰되, 각자 자기 쪽 작업에만 접근한다.
- Job 템플릿은 ConfigMap `enikk-dev-agent-job` 의 `job.yaml` 이다. core 는 `${TASK_ID}` 만 바꾸고, 결과가 `enikk-dev` 네임스페이스의 Job 이며 `enikk.dev/role=dev-agent`, `enikk.dev/task=<작업>` 라벨을 가졌는지 확인한 뒤 만든다. 이미지 태그가 없으면 템플릿이 배포되지 않고, core 는 Job 을 만들지 않는다.
- 시작한 Job 은 DB 의 `dispatches` 에 기록한다. 하루 상한은 최근 24시간의 기록 수로 센다. 상한에 걸린 작업은 대기하고, 위임 단계(ADR-0022)에서 접수 전에 거부한다.
- 한 작업에 Job 을 `max_attempts` 번 시작했는데도 결과 보고가 없으면, 그 작업을 실패로 처리하고 스레드에 알린다.

### 7. 개발 Pod 안 (구현 세부, 2026-11-22)
- 앱 `enikk-dev-agent`(이미지 `deploy/docker/dev-agent.Dockerfile`: 공용 Python 이미지 + git, gh, uv)는 작업 하나를 처리하고 종료한다.
  1. `claim` 으로 자기 작업을 임대한다. 없으면 아무것도 하지 않고 끝낸다.
  2. core 에서 Claude 토큰과 Langfuse 키를 받고, `POST /v1/github/token` 으로 개발용 GitHub 토큰을 받는다. core 는 그 작업의 활성 임대를 확인한 뒤에만 발급한다.
  3. 토큰은 gh 의 설정 파일(권한 0600)에만 쓰고, git 은 `gh auth git-credential` 로 읽는다. 환경변수와 명령 인자에는 넣지 않는다.
  4. 저장소를 clone 하고 `agent/<작업 ID>` 브랜치를 만든 뒤, 저장소·브랜치·워크플로 수정 가능 여부를 요청 앞에 붙여 에이전트를 실행한다.
  5. 결과를 보고하고, 토큰을 폐기(`DELETE /installation/token`)하고 설정 파일을 지운다.
- Claude 엔진은 정의의 `tools.builtin` 만 열고, CLI 가 그 밖의 도구를 열면 실행을 거부한다. 계약 검증으로 `builtin` 도구는 `isolated` 에이전트만 가질 수 있다(I-14).
- 프롬프트 `developer` 는 Langfuse 에 있다(I-13). `production` 승격은 소유자가 한다(I-19).
- `enikk-ops smoke dev` 는 개발 에이전트에게 파일 하나를 추가하는 PR 을 요청하고, PR 링크가 돌아오는지 확인한다. PR 은 열린 채로 남으므로 확인 뒤 닫는다.

## 근거
- 격리 단위가 Pod 이면 작업 사이에 파일, 프로세스, 자격증명이 남지 않는다.
- core 의 Kubernetes 권한을 `enikk-dev` 한 네임스페이스의 Job 으로 한정하면, 권한이 늘어나는 범위가 개발 에이전트 실행에 그친다.
- 템플릿을 git 에 두면 Pod 의 보안 설정이 코드 리뷰와 GitOps(I-18)를 거친다.

## 결과
- I-28 을 고친다: core 는 TokenReview 와 `enikk-dev` 네임스페이스의 Job 관리 권한만 갖는다.
- Docker Desktop 의 기본 네트워크는 NetworkPolicy 를 적용하지 않는다. 개발 Pod 와 다른 Pod 사이의 네트워크 격리는 이 환경에서 보장되지 않는다. core API 는 모든 요청을 인증하므로(I-28) 개발 Pod 는 자기 역할의 API 만 쓸 수 있다.
- Job 시작마다 이미지를 띄우고 저장소를 clone 하므로 작업 시작이 수십 초 늦다.
- 개발 Pod 는 인터넷(GitHub, Anthropic, PyPI)에 나갈 수 있어야 한다.
- **감수하는 위험 (소유자 결정, 2026-11-22)**: push 와 PR 생성을 위해 개발 Pod 안의 git·gh 가 개발용 GitHub 토큰을 쓰므로, 셸 도구를 가진 모델도 이 토큰을 읽을 수 있다. 모델이 출력하면 대화 이력이나 트레이스에 남을 수 있다.
  - 제한 장치: 저장소 하나, 승인·설정 권한 없음, 1시간 만료, 작업이 끝나면 폐기, `agent/**` 브랜치에만 push 가능, 머지는 Slack 승인 필요. 토큰은 git credential helper 로만 전달하고 환경변수 전체에 넣지 않는다.
  - 완전히 막으려면 push·PR 생성을 모델 밖의 플랫폼 도구로 빼야 한다. 재검토 조건에 둔다.

## 재검토 조건
- NetworkPolicy 를 적용하는 클러스터로 옮길 때 → 개발 Pod 의 나가는 연결을 허용 목록으로 제한.
- 동시 실행 1개가 병목이 될 때. → 2026-11-23 10개로 올린다(§4).
- 동시에 도는 개발 Job 들이 노드 메모리를 다 쓸 때.
