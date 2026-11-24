# ADR-0030: 개발 Pod 에서 Docker 없이 영속성 통합 테스트를 돌린다

- 상태: 대체됨 (ADR-0037 4a·4e: 개발 Pod 가 없어졌고, 영속성 통합 테스트는 CI 에서만 돌린다)
- 날짜: 2026-11-22
- 관련: ADR-0012, ADR-0021, I-07, I-21, I-26

## 배경
ADR-0012 §1 은 영속성 통합 테스트를 testcontainers(Docker)로만 돌리고, Docker 가 없으면 실패시키도록 정했다.
개발 Pod(ADR-0021)에는 Docker 가 없다. 그래서 개발 에이전트가 여는 PR 은 매번 Postgres 통합 테스트(약 15건)가 오류로 끝나고, 에이전트는 이를 "이번 변경과 관계없음"으로 적고 넘어갔다. 검증 결과를 믿기 어렵다.

제약 (소유자 요청, 2026-11-22):
- privileged 컨테이너, Docker-in-Docker, 호스트 Docker 소켓 마운트는 쓰지 않는다.
- 새 비밀값을 만들지 않는다.
- 운영 DB 에는 절대 붙지 않는다.
- PC 와 CI 의 기존 동작(testcontainers)은 유지한다.

## 검토한 대안
- **Job 템플릿에 Postgres 사이드카 추가, 테스트는 DSN 환경변수로 접속**: 테스트 코드는 단순하다. 그러나 Job 이 끝나려면 사이드카가 스스로 멈춰야 하므로 네이티브 사이드카(initContainer + `restartPolicy: Always`)가 필요하고, Postgres 이미지가 `restricted` PSA·읽기 전용 루트·uid 10001 에서 돌도록 볼륨과 설정을 더 얹어야 한다. 테스트를 돌리지 않는 작업에도 매번 DB 가 떠 자원을 쓰고, 이미지가 하나 더 늘어 Image Updater 대상도 는다. 배포 뒤에만 확인할 수 있다. → 기각.
- **개발 이미지에 Postgres 서버 바이너리를 넣고, 테스트 픽스처가 임시 인스턴스를 띄운다**: 채택.

## 결정
1. 개발 이미지(`deploy/docker/dev-agent.Dockerfile`)에 클러스터와 같은 메이저 버전(18)의 Postgres 서버를 PostgreSQL 공식 apt 저장소(PGDG)에서 설치한다.
   - 저장소 서명 키는 빌드 때 받아 **sha256 으로 고정**해 확인한다. 설치 때 기본 클러스터는 만들지 않는다.
   - 이미지는 `ENIKK_TEST_POSTGRES_BIN` 에 그 바이너리 경로를 둔다. Job 템플릿과 Helm values 는 바꾸지 않는다.
2. 영속성 테스트 픽스처는 다음 순서로 Postgres 를 고른다.
   1. `ENIKK_TEST_POSTGRES_DSN`: 이 컴퓨터에 띄운 일회용 서버. 호스트가 루프백(`localhost`, `127.0.0.1`, `::1`)이나 Unix 소켓이 아니면 접속하기 전에 오류로 끝낸다.
   2. `ENIKK_TEST_POSTGRES_BIN`: 그 디렉터리의 `initdb`·`pg_ctl` 로 임시 서버를 띄운다. TCP 는 열지 않고 권한 0700 인 임시 디렉터리의 Unix 소켓에서만 받으며, 인증은 `trust` 다. 비밀번호가 없으므로 새 비밀값도 없다. 테스트가 끝나면 서버를 멈추고 지운다.
   3. 둘 다 없으면 지금처럼 testcontainers 로 Docker 컨테이너를 띄운다(PC, CI).
3. 어느 경우든 세션마다 이름이 임의인 새 데이터베이스(`enikk_test_<임의값>`)를 만들어 마이그레이션하고, 세션이 끝나면 지운다. 기존 데이터베이스에는 쓰지 않는다. 테스트마다 테이블을 비우는 것은 그대로다.
4. 어떤 방법으로도 Postgres 를 띄울 수 없으면 ADR-0012 §1 대로 건너뛰지 않고 실패한다.

ADR-0012 §1 의 "testcontainers 로만 실행한다"를 이 결정으로 대체한다. 같은 메이저 버전, 실패시키기 원칙은 유지한다.

## 근거
- 개발 Pod 는 이미 비루트(uid 10001), 읽기 전용 루트, `/tmp` emptyDir 로 돈다. Postgres 는 데이터 디렉터리와 소켓만 쓰면 되므로 이 조건을 바꾸지 않고 돈다(2026-11-22, 개발 Pod 에서 확인).
- 보안 설정과 Job 템플릿(민감 경로)을 건드리지 않는다. 바뀌는 것은 이미지 내용뿐이다.
- 테스트를 돌리는 동안만 서버가 떠 있고, 작업 Pod 가 끝나면 모두 사라진다.
- 루프백 검사와 세션별 새 데이터베이스로, 잘못 설정해도 운영 DB 나 기존 데이터에 쓰지 않는다. 개발 Pod 에는 DB 접속 정보도 없다.
- PGDG 는 PostgreSQL 프로젝트가 운영하는 Debian 용 저장소다. Debian trixie 기본 저장소에는 17 만 있다(2026-11-22 확인, https://apt.postgresql.org/pub/repos/apt/dists/trixie-pgdg/).

## 결과
- 개발 이미지가 약 130MB 커진다(Postgres 18 과 의존 패키지의 설치 크기 합).
- 개발 Pod 의 자원 요청은 바꾸지 않는다. 테스트 중 Postgres 는 수십 MB 의 메모리와 `/tmp` 에 수십 MB 를 쓴다(WAL 상한 64MB).
- 이미지 빌드가 PGDG 와 postgresql.org 에 의존한다. 서명 키가 바뀌면 고정한 체크섬과 맞지 않아 빌드가 실패하므로, 키를 확인한 뒤 체크섬을 고친다.
- `ENIKK_TEST_POSTGRES_*` 는 테스트 전용 설정이다. 서비스 코드는 읽지 않는다.

## 재검토 조건
- 운영 Postgres 의 메이저 버전을 올릴 때(이미지의 `POSTGRES_MAJOR` 와 testcontainers 이미지를 함께 올린다).
- 개발 Pod 의 이미지 크기나 빌드 시간이 문제가 될 때.
