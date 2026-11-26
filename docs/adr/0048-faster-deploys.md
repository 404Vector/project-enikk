# ADR-0048: 배포 시간을 줄인다

- 상태: 승인 (§1 구현. §2~§4 는 뒤따르는 PR 에서 이 문서에 더한다)
- 날짜: 2026-11-26
- 관련: ADR-0008, ADR-0038, ADR-0040

## 배경
`main` 머지부터 모든 Pod 가 Ready 가 되기까지 약 6분이 걸린다(#129 기준, 2026-11-26 소유자 측정).
- `images` 워크플로 3분 24초. runtime-claude amd64 빌드가 2분 47초로 가장 길다.
- Image Updater 가 새 태그를 보기까지 최대 2분(이번에는 46초).
- runtime-claude 이미지(291MB) 받기 72초, kubelet 이 이미지를 하나씩 받아 33초 더 대기.
- core 의 migrate → 새 core → selfcheck 1분 41초. 이 순서는 유지한다.

원인의 첫째는 `python-app.Dockerfile` 이 `COPY . .` 뒤에 `uv sync` 를 하는 것이다. 코드만 바뀌어도 종속성 설치 레이어의 캐시가 깨져 GHA 캐시가 소용없고, 바이트코드 컴파일도 매번 다시 한다. 종속성과 앱 코드가 한 `.venv` 레이어에 있어서 노드는 배포마다 그 레이어를 통째로 다시 받는다. 지금은 코드만 자주 바뀌고 종속성은 거의 바뀌지 않는다.

## 검토한 대안
- §1 레이어 나누기
  - A. 종속성 설치 단계에 `uv.lock` 과 `pyproject.toml` 만 넣고, 최종 이미지는 지금처럼 `.venv` 하나를 복사: 빌드 캐시는 맞는다 / 최종 이미지의 `.venv` 레이어가 매번 바뀌어 받는 양은 그대로다.
  - B. A 에 더해, 워크스페이스 패키지(`enikk_*`)를 `.venv` 에서 빼내 최종 이미지에 따로 복사: 코드만 바뀌면 1MB 안팎의 레이어 하나만 바뀐다 / 빌드 단계가 조금 복잡하다.
  - C. 워크스페이스를 editable 로 설치하고 소스를 따로 복사: 레이어는 나뉜다 / 이미지 안 경로가 소스 트리에 묶이고 지금의 `--no-editable` 배포 방식과 달라진다.

## 결정
### §1 이미지 레이어 (B안)
- `manifests` 단계: 소스에서 `pyproject.toml` 만 남긴다.
- `deps` 단계: 루트 `pyproject.toml`·`uv.lock`·`.python-version` 과 멤버의 `pyproject.toml` 만으로 `uv sync --frozen --no-dev --no-editable --no-install-workspace --package <앱>`. 바이트코드 컴파일은 여기서 한 번 한다.
- `build` 단계(`deps` 위): 소스를 복사하고 워크스페이스 패키지를 설치한다. 바이트코드 컴파일은 `enikk_*` 에만 한다. 그 뒤 `site-packages/enikk_*` 와, 그 패키지의 `RECORD` 가 가리키는 `bin/` 명령을 `/out` 으로 옮긴다.
- 최종 이미지: `deps` 의 `.venv` 와 `build` 의 `/out` 을 서로 다른 레이어로 복사한다. `agents/`·`config/` 는 빌드 문맥에서 바로 복사한다.
- 의존성(`uv.lock`, 어느 `pyproject.toml` 이든)이 바뀌면 `deps` 단계부터 다시 빌드한다. 루트 `pyproject.toml` 의 도구 설정만 바꿔도 마찬가지다.

## 근거
- BuildKit 의 `COPY` 캐시 키는 파일 내용과 권한으로 정해진다. `manifests` 단계는 코드가 바뀔 때마다 다시 돌지만 결과가 같으므로 `deps` 단계는 캐시를 쓴다.
- uv 0.13.9 로 확인(2026-11-26): 멤버의 `pyproject.toml` 만 있는 트리에서 `--no-install-workspace` 가 종속성만 설치한다. 그 위에서 워크스페이스를 설치하면 `.venv` 에 새로 생기는 것은 `enikk_*` 항목과 그 `RECORD` 의 `bin/` 명령뿐이고, 이것을 옮기면 남은 `.venv` 는 종속성만 설치한 것과 파일 목록이 같다. 다시 합친 환경에서 `enikk-core` 가 실행된다.
- runtime-claude 의 워크스페이스 패키지는 약 1MB, `.venv` 는 약 400MB(대부분 `claude_agent_sdk`)다.

## 결과
- 코드만 바뀐 배포에서 노드가 새로 받는 것은 워크스페이스 레이어와 `agents/`·`config/` 레이어뿐이다. 종속성 레이어는 GHA 캐시에서 같은 내용으로 다시 만들어져 digest 가 같다.
- 워크스페이스 패키지 이름은 `enikk_` 로 시작해야 한다. 다른 이름의 최상위 모듈을 더하면 종속성 레이어에 남지 않고 이미지에서 빠진다.
- GHA 캐시(10GB, 7일 미사용 시 삭제)가 비면 첫 빌드는 예전처럼 오래 걸린다.

## 재검토 조건
- 워크스페이스가 `enikk_` 가 아닌 최상위 모듈이나 데이터 파일을 `site-packages` 밖에 설치하게 될 때.
- 종속성이 코드만큼 자주 바뀌게 될 때.
