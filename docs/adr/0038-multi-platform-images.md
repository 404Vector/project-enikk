# ADR-0038: 이미지를 amd64 와 arm64 로 함께 빌드한다

- 상태: 승인
- 날짜: 2026-11-24
- 관련: ADR-0008, ADR-0028, ADR-0033, I-18

## 배경
지금까지 `images` 워크플로는 `ubuntu-latest`(x86_64) runner 에서 플랫폼 지정 없이 빌드했다. 그래서 GHCR 의 `enikk-*` 이미지는 모두 linux/amd64 전용이었다.
소유자는 배포 대상을 Windows(WSL2) 노트북에서 Apple Silicon Mac 의 Docker Desktop Kubernetes 로 옮기려 한다. 이 클러스터의 노드는 linux/arm64 다.
amd64 이미지를 arm64 노드에서 돌리면 에뮬레이션(Rosetta·QEMU)을 거친다. 느릴 뿐 아니라, Landlock 샌드박스(ADR-0033)가 ctypes 로 직접 부르는 `landlock_*` 시스템 호출을 에뮬레이터가 지원한다는 보장이 없다. 샌드박스는 실패하면 실행을 막으므로, 그러면 역할의 기본 도구가 모두 쓸 수 없게 된다.

## 검토한 대안
- A. 한 runner 에서 QEMU 로 두 플랫폼을 빌드(`platforms: linux/amd64,linux/arm64`): 워크플로가 가장 단순하다 / arm64 쪽 `uv sync` 와 apt 설치가 에뮬레이션으로 돌아 빌드가 몇 배 느려진다.
- B. 플랫폼마다 그 아키텍처의 GitHub 호스팅 runner(`ubuntu-latest`, `ubuntu-24.04-arm`)에서 빌드하고, digest 로 올린 뒤 하나의 태그로 합친다: 에뮬레이션이 없다 / 잡이 하나 더 생긴다.
- C. arm64 만 빌드: Mac 에서는 충분하다 / WSL2·x86 클러스터와 x86 개발 PC 로 되돌아갈 수 없다.

## 결정
B안.
- `verify`(PR): 앱 × {amd64, arm64} 를 각 아키텍처 runner 에서 빌드만 한다. 권한은 `contents: read` 그대로다.
- `build`(`main`): 앱 × 아키텍처마다 이미지를 태그 없이 digest 로만 GHCR 에 올리고, digest 를 아티팩트로 넘긴다.
- `publish`(`main`): 앱마다 두 digest 를 `docker buildx imagetools create` 로 하나의 멀티 플랫폼 인덱스로 묶어 `sha-<커밋 SHA>` 태그를 붙인다. 인덱스에 `org.opencontainers.image.source`·`revision` 주석을 단다.
- 태그 규칙(`^sha-[0-9a-f]{40}$`), 불변 태그, Image Updater 설정은 바꾸지 않는다.

## 근거
- 공개 저장소는 arm64 GitHub 호스팅 runner(`ubuntu-24.04-arm`)를 무료로 쓸 수 있다(GitHub Changelog 2025-01-16 공지).
- 의존성은 두 플랫폼 모두 준비되어 있다. `uv.lock` 의 `claude-agent-sdk` 에 `manylinux_2_17_aarch64` wheel 이 있고, `python:3.13-slim-trixie`·`ghcr.io/astral-sh/uv`·PGDG 의 PostgreSQL 18 도 arm64 를 제공한다.
- 태그는 두 아키텍처가 모두 올라간 뒤에만 생긴다. 한쪽만 실패하면 태그가 없으므로 Image Updater 가 한 플랫폼만 있는 이미지를 배포하지 않는다.

## 결과
- `main` 의 이미지 게시가 `build` → `publish` 두 단계가 되고, runner 사용량이 약 두 배가 된다.
- 이 결정 이전의 `sha-*` 태그는 amd64 전용으로 남는다. arm64 클러스터에서는 이 PR 이후의 태그부터 쓴다.
- Image Updater 는 자신이 도는 노드의 플랫폼에 맞는 매니페스트로 `newest-build` 를 판단한다. 두 플랫폼이 모두 있으므로 어느 클러스터에서도 같은 태그를 고른다.
- Docker Desktop for Mac 의 VM 커널에 Landlock 이 켜져 있는지는 이 결정과 별개로 이전 전에 확인한다(`/sys/kernel/security/lsm` 에 `landlock`). 없으면 샌드박스가 실행을 막는다.

## 재검토 조건
- arm64 호스팅 runner 가 공개 저장소에 유료가 되거나 사라질 때.
- 배포 대상이 다시 한 아키텍처로 고정되어 다른 쪽 빌드가 필요 없어질 때.
