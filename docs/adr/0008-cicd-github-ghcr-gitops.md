# ADR-0008: CI/CD — GitHub Actions 빌드, GHCR 레지스트리, GitOps 배포

- 상태: 승인
- 날짜: 2026-11-21
- 관련: ADR-0007, I-07, I-18

## 배경
자기 개선 구조(ADR-0007)는 머지된 변경이 사람 손을 거치지 않고 클러스터에 반영되는 경로를 필요로 한다.
대상 클러스터는 로컬 Docker Desktop Kubernetes 로, 외부(GitHub)에서 직접 접근할 수 없다.

## 결정
1. **원격 저장소**: GitHub.
2. **빌드**: GitHub Actions 가 이미지를 빌드해 **GHCR**(GitHub Container Registry)에 올린다.
   - 이미지 태그는 커밋 SHA 등 **변경 불가능한 값**을 쓴다. `latest` 같은 이동 태그로 배포하지 않는다.
3. **배포**: 클러스터 안의 **GitOps 컨트롤러**가 git 저장소를 주기적으로 읽어(pull) 원하는 상태로 맞춘다.
   - 외부에서 클러스터로 밀어 넣는(push) 방식은 로컬 클러스터에 접근할 수 없으므로 쓰지 않는다.
4. 운영 배포는 GitOps 컨트롤러만 수행한다. 에이전트와 사람 모두 `helm`/`kubectl` 로 운영 리소스를 직접 바꾸지 않는다 (I-18).
   - 예외: GitOps 컨트롤러 자체의 최초 설치 같은 **부트스트랩 작업**은 운영 CLI 로 수행한다.

## 근거
- 풀(pull) 방식 GitOps 는 클러스터가 외부로 나가는 연결만 필요하므로 로컬 클러스터에서도 동작한다.
- 배포 이력과 롤백이 git 이력과 일치한다.

## 결과
- 저장소와 이미지가 비공개라면 클러스터에 다음 자격증명이 필요하며, 소유자가 직접 Secret 으로 만든다 (I-07, I-21).
  - GitOps 컨트롤러용 저장소 읽기 권한 (배포 키 등)
  - GHCR 이미지 pull 권한 (`read:packages` 범위 토큰)
- 새 이미지 태그를 배포 설정에 반영하는 방법(Actions 가 태그를 커밋 / 컨트롤러의 이미지 자동 갱신 기능)을 정해야 한다.

## 추가 결정 (2026-11-21)
- **저장소와 이미지는 공개한다.**
  - GHCR 이미지 pull 에 자격증명이 필요 없다.
  - 저장소의 모든 내용(에이전트 정의, 배포 설정, 이력)이 공개되므로 비밀값 관리가 더 엄격해야 한다 (I-07).
    GitHub secret scanning 과 push protection 을 켠다.
  - 외부인이 fork 와 PR 을 만들 수 있으므로, 외부 PR 이 실행하는 워크플로가 클러스터나 비밀값에 닿지 않아야 한다.

## 배포 방식 재검토: GitOps 컨트롤러 vs self-hosted runner (2026-11-21)
- self-hosted runner 로 클러스터에 직접 배포하는 안을 검토했으나 GitOps 컨트롤러를 유지한다.
- 근거:
  - GitHub 문서는 self-hosted runner 를 공개 저장소에 쓰지 않도록 권고한다. fork PR 이 runner 에서 코드를 실행할 수 있다.
  - PR 워크플로는 PR 브랜치의 워크플로 파일로 실행되므로, 개발 에이전트가 자기 브랜치에서 워크플로를 바꿔 승인 전에 runner(클러스터 권한)를 쓸 수 있다. I-20 의 CI 검사는 머지만 막고 이 실행은 막지 못한다.
  - 안전하게 구성하려면 클러스터 안 임시 runner Pod(Actions Runner Controller)와 `main` 한정 자격증명이 필요해, 구성 부담이 GitOps 컨트롤러와 비슷해진다.
  - GitOps 컨트롤러는 클러스터가 git 을 읽기만 하므로 GitHub 쪽에서 클러스터로 들어오는 경로가 없다.

## GitOps 컨트롤러 선정 (2026-11-21)
- **Argo CD** 를 사용한다.
- 검토한 대안:
  - Flux: 가볍고, 새 이미지 감지 → git 반영 기능이 기본 제공. 공식 웹 UI 없음.
  - Argo CD: 웹 UI(리소스 트리, git 과 클러스터 차이 비교, 동기화·롤백)가 강력하고 실무에서 더 널리 쓰인다. 이미지 자동 반영은 애드온 필요.
- 근거: 실무 적용 가치와 배포 상태의 가시성을 우선했다. enikk-core(ADR-0010)가 애플리케이션 수준 관리를, Argo CD UI 가 배포·인프라 수준 관리를 맡는다.
- 이미지 태그 반영: **Argo CD Image Updater** 를 사용해 새 이미지 태그를 git 에 반영한다.

## 이미지 태그 반영 방식 (2026-11-21)

### 검토한 대안
- **A. Image Updater 가 `main` 에 직접 커밋**: 배포 키를 브랜치 보호 예외로 등록해야 한다. 그 키로는 어떤 파일이든 `main` 에 쓸 수 있어 I-19 에 예외 통로가 생긴다. → 기각.
- **B. Image Updater 가 Argo CD Application 의 helm 파라미터에 기록 (write-back method `argocd`)**: 채택.
- **C. CI 가 태그 변경 PR 을 만들고 자동 머지**: `GITHUB_TOKEN` 으로 만든 PR 에서는 CI 가 실행되지 않아 GitHub App 이 필요하다. → M7 에서 재검토.
  참고: Image Updater 1.5 는 GitHub PR 로 기록하는 방식(`writeBackConfig.gitConfig.pullRequest.github`)을 지원한다.

### 결정
- **B안.** Argo CD Image Updater 1.5 의 `ImageUpdater` CR 로 설정한다 (`deploy/k8s/image-updates/`).
  - 전략 `newest-build`, 허용 태그 `^sha-[0-9a-f]{40}$`.
  - `forceUpdate: true`: Image Updater 는 기본적으로 Application 이 실제 사용 중인 이미지만 갱신한다.
    태그가 없는 차트는 0 복제본으로 시작하므로 이 옵션이 없으면 첫 배포가 시작되지 않는다.
- 최상위 app-of-apps(`deploy/argocd/root.yaml`)는 하위 Application 의 `/spec/source/helm/parameters` 차이를 무시한다(`RespectIgnoreDifferences=true`).
  그렇지 않으면 Image Updater 가 기록한 태그를 최상위 앱이 git 상태로 되돌린다.
- 이미지 태그가 아직 없으면 Deployment 는 0개 복제본으로 배포된다.

### 결과
- 새 자격증명도, 브랜치 보호 예외도 필요 없다.
- 이미지 태그를 제외한 모든 배포 설정의 원본은 git 이다. 현재 배포된 이미지와 그 이력은 Argo CD 에서 확인한다.

### 알려진 위험 (M7 전에 해결)
- 같은 저장소의 브랜치에서 열린 PR 워크플로는 PR 브랜치의 워크플로 파일로 실행되므로, 워크플로를 수정해 `packages: write` 권한으로 GHCR 에 `sha-*` 이미지를 올릴 수 있다.
  Image Updater 는 이를 승인되지 않은 이미지와 구별하지 못한다.
- 현재는 브랜치를 만들 수 있는 주체가 소유자뿐이라 실제 위험이 없다. 개발 에이전트가 브랜치를 만들기 시작하는 M7 전에 이미지 출처 검증(서명 검증 등) 또는 C안 전환을 결정한다.

### 해결 (2026-11-22, M7)
서명 검증이나 C안 대신 다음 두 가지로 막는다.
- `images` 워크플로를 PR 검증(`verify`, `contents: read` 만)과 `main` 게시(`publish`, `packages: write`)로 나눈다. PR 에서 실행되는 작업은 패키지 쓰기 권한을 받지 않는다.
- 워크플로 파일을 바꿀 수 있는 주체는 소유자뿐이다. 개발 에이전트의 토큰에는 `workflows` 권한이 없어 GitHub 가 워크플로 파일을 바꾸는 push 를 거부한다(ADR-0019). 보호 경로 검사(I-20)도 `.github/` 변경을 막는다.
