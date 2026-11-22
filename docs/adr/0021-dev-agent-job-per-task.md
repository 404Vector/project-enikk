# ADR-0021: 개발 에이전트는 작업마다 새 Pod(Job)로 실행한다

- 상태: 승인
- 날짜: 2026-11-22
- 관련: ADR-0002, ADR-0007, ADR-0010, ADR-0011, ADR-0013, ADR-0017, ADR-0019, ADR-0020, ADR-0022, I-14, I-17, I-18, I-20, I-21, I-28

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

### 5. 이미지와 정의
- 이미지 `enikk-dev-agent` 는 runtime-claude 와 같은 엔진 코드에 git, uv 를 더한 것이다. CI 가 같은 방식으로 빌드한다.
- 에이전트 정의 `agents/developer.yaml` 이 셸·파일 편집 도구를 허용하는 유일한 정의다(I-14).

## 근거
- 격리 단위가 Pod 이면 작업 사이에 파일, 프로세스, 자격증명이 남지 않는다.
- core 의 Kubernetes 권한을 `enikk-dev` 한 네임스페이스의 Job 으로 한정하면, 권한이 늘어나는 범위가 개발 에이전트 실행에 그친다.
- 템플릿을 git 에 두면 Pod 의 보안 설정이 코드 리뷰와 GitOps(I-18)를 거친다.

## 결과
- I-28 을 고친다: core 는 TokenReview 와 `enikk-dev` 네임스페이스의 Job 관리 권한만 갖는다.
- Docker Desktop 의 기본 네트워크는 NetworkPolicy 를 적용하지 않는다. 개발 Pod 와 다른 Pod 사이의 네트워크 격리는 이 환경에서 보장되지 않는다. core API 는 모든 요청을 인증하므로(I-28) 개발 Pod 는 자기 역할의 API 만 쓸 수 있다.
- Job 시작마다 이미지를 띄우고 저장소를 clone 하므로 작업 시작이 수십 초 늦다.
- 개발 Pod 는 인터넷(GitHub, Anthropic, PyPI)에 나갈 수 있어야 한다.

## 재검토 조건
- NetworkPolicy 를 적용하는 클러스터로 옮길 때 → 개발 Pod 의 나가는 연결을 허용 목록으로 제한.
- 동시 실행 1개가 병목이 될 때.
