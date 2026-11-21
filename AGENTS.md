# AGENTS.md

코딩 에이전트를 위한 작업 지침. 설계 결정·원칙·불변식은 이 파일에 적지 않는다. 모두 `docs/` 에 있다.

## 작업 전에 읽을 것

- `docs/README.md` — 문서 구성과 작성 규칙
- `docs/architecture/invariants.md` — 어떤 변경에서도 깨면 안 되는 조건
- `docs/architecture/principles.md` — 코드 작성 원칙
- `docs/adr/` — 관련 결정. `제안` 상태 ADR 은 구현 근거로 쓰지 않는다.

구조를 바꾸거나 새 결정이 필요하면 코드보다 문서(ADR)를 먼저 제안하고 소유자의 리뷰를 받는다.

## 개발 환경

- Python 은 uv 로 관리한다 (`.python-version` 참조). pip / venv 를 직접 쓰지 않는다.
- 의존성 추가: 해당 패키지 디렉토리에서 `uv add <패키지>`.

```bash
uv sync --all-packages           # 모든 workspace 멤버와 개발 도구 설치
```

- `main` 에는 직접 push 할 수 없다. 브랜치를 만들고 PR 로 반영한다.

## 검사 (변경 후 모두 통과해야 함)

```bash
uv run ruff format .
uv run ruff check .
uv run mypy
uv run lint-imports              # 계층 의존성 규칙
uv run pytest                    # 영속성 통합 테스트는 Docker 로 Postgres 를 띄운다
```

## 코드 규칙

- 테스트는 해당 패키지의 `tests/` 에 둔다.
- 값 객체와 계약은 `enikk_kernel.FrozenModel` / `enikk_contracts.Contract` 를, 설정은 `ServiceSettings` 와 `SettingsModel` 을 상속한다.
- `json`, `subprocess` 모듈은 직접 쓰지 않는다 (ruff 가 막는다). pydantic 과 `ProcessRunner` 를 쓴다.
- 코드 주석에는 "무엇을 하는지"만 쓴다. "왜 이렇게 설계했는지"는 `docs/` 에 쓴다.
- 비밀값을 코드, 테스트 픽스처, 명령 인자, 로그에 넣지 않는다.
- 서비스는 비밀값을 `CoreClient.fetch_credentials` 로만 받는다. 새 비밀값은 `CredentialName` 과 core values 의 `roleBindings[].credentials` 에 함께 추가한다. `~/.config/enikk/secrets.yaml` 은 읽거나 쓰지 않는다.
- DB 스키마는 `enikk_persistence_sql/tables.py` 와 Alembic 리비전(`migrations/versions/`)을 함께 바꾼다. 둘이 어긋나면 테스트가 실패한다.
- 새 core API 엔드포인트는 `@requires(<역할>)` 로 허용 역할을 선언한다. 선언이 없으면 앱이 시작되지 않는다.
- 에이전트 정의(`agents/*.yaml`)는 core 테스트가 검증한다. 프롬프트 본문은 넣지 않는다.

## 배포 자산

- Dockerfile: `deploy/docker/python-app.Dockerfile` (빌드 인자 `PACKAGE`)
- Helm 차트: `deploy/helm/`, 그 외 매니페스트: `deploy/k8s/`
- Argo CD Application: `deploy/argocd/` (클러스터는 `main` 의 이 디렉토리를 따른다)
- 대상 클러스터: kube context `docker-desktop`
- 운영 리소스를 `helm`/`kubectl` 로 직접 바꾸지 않는다. 최초 설치만 `uv run enikk-ops argocd bootstrap` 으로 한다.
- 배포 후 확인: `uv run enikk-ops smoke core` (리소스를 만들지 않는다). Claude 런타임은 `smoke claude` 로 확인한다 (모델을 실제로 호출한다).
- Claude Agent SDK 는 `engine-claude` 패키지에서만 import 한다 (import-linter 가 막는다).
