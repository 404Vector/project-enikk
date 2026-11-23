# 테스트 실행

- 상태: 작성 중
- 최종 갱신: 2026-11-23

저장소의 검사 목록은 루트 `AGENTS.md` 에 있다. 이 문서는 그중 `uv run pytest` 를 어디서 어떻게 돌리는지 적는다.

## Postgres 가 필요한 테스트

`src/libs/adapters/persistence-sql/tests` 의 통합 테스트는 실제 Postgres 에 마이그레이션을 적용하고 실행한다.
픽스처(`conftest.py`)는 다음 순서로 Postgres 를 고른다(ADR-0030).

| 순서 | 조건 | 동작 | 쓰는 곳 |
|---|---|---|---|
| 1 | `ENIKK_TEST_POSTGRES_DSN` 이 있다 | 그 서버에 붙는다. 호스트가 `localhost`·`127.0.0.1`·`::1`·Unix 소켓이 아니면 접속 전에 오류 | 직접 띄운 일회용 Postgres |
| 2 | `ENIKK_TEST_POSTGRES_BIN` 이 있다 | 그 디렉터리의 `initdb`·`pg_ctl` 로 임시 서버를 띄운다(Unix 소켓만, `trust` 인증). 끝나면 지운다 | 개발 Pod (이미지가 설정) |
| 3 | 둘 다 없다 | testcontainers 로 `postgres:18-alpine` 컨테이너를 띄운다 | PC(Docker Desktop), CI |

- 어느 경우든 세션마다 `enikk_test_<임의값>` 데이터베이스를 새로 만들어 마이그레이션하고, 끝나면 지운다. 테스트마다 테이블을 비운다.
- Postgres 를 띄울 수 없으면 테스트는 건너뛰지 않고 오류로 끝난다(ADR-0012 §1).
- 운영 DB 의 주소나 접속 정보를 이 변수에 넣지 않는다. 루프백 검사가 막지만, 넣을 이유도 없다.

### PC 에서

Docker Desktop 이 켜져 있으면 `uv run pytest` 만 실행한다.

Docker 없이 로컬에 설치한 Postgres 를 쓰려면(예: Debian/Ubuntu 의 `postgresql-18`):

```bash
ENIKK_TEST_POSTGRES_BIN=/usr/lib/postgresql/18/bin uv run pytest
```

이미 띄운 일회용 서버를 쓰려면, 데이터베이스를 만들 수 있는 역할로 접속하는 DSN 을 준다.

```bash
ENIKK_TEST_POSTGRES_DSN=postgresql://enikk@127.0.0.1:5432/postgres uv run pytest
```

### 개발 Pod 에서 (에닉의 개발 역할)

개발 이미지(`deploy/docker/dev-agent.Dockerfile`)에 Postgres 18 서버가 들어 있고 `ENIKK_TEST_POSTGRES_BIN` 이 설정돼 있다.
개발 역할은 PC 와 같은 명령(`uv run pytest`)만 실행하면 된다. Docker 는 필요 없다.

## 비밀 파일 권한 테스트

`src/apps/ops-cli/tests/test_secrets.py` 는 비밀 파일이 정확히 0600, 그 디렉터리의 권한 비트가 0700 인지 확인한다.
디렉터리는 권한 비트(`rwx`)만 비교한다. Kubernetes 의 `fsGroup` 볼륨처럼 setgid 가 켜진 디렉터리 아래에서는 새 디렉터리가 setgid 비트(02000)를 물려받는데, 이 비트는 접근 권한을 주지 않는다.

## 도구 샌드박스 테스트

`src/libs/adapters/engine-claude/tests/test_sandbox.py` 는 역할의 기본 도구를 가두는 Landlock 샌드박스(ADR-0033)를 실제 커널과 실제 Claude Code CLI(SDK 에 들어 있는 것)로 확인한다.
CLI 는 테스트가 localhost 에 띄운 가짜 Anthropic API 와 통신하므로 자격증명이 필요 없고 외부로 나가지 않는다.
같은 방법으로 앞 작업의 세션을 샌드박스 안에서 이어받는지, 검색 요청이 거부된 WebSearch 가 작업 실패가 아니라 도구 오류로 끝나는지도 확인한다(ADR-0034).
Landlock 이 없는 커널에서는 이 테스트들이 그 이유와 함께 건너뛰어진다. Docker Desktop(WSL2)과 개발 Pod, GitHub 호스팅 runner 의 커널에는 Landlock 이 있다(GitHub runner 는 CI 결과로 확인한다).
