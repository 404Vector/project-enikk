# ADR-0011: enikk-core 의 저장소·작업 큐·API·데이터 모델링

- 상태: 승인
- 날짜: 2026-11-21
- 관련: ADR-0010, P-02, P-04, P-13, I-21, I-26, I-27, I-28

## 배경
ADR-0010 의 미결정 사항(상태 저장소, 작업 큐, API 형식, 서비스 간 인증)을 정한다.
코드는 주로 에이전트가 작성하므로, 실수가 구조적으로 막히는 방식을 우선한다.

## 검토한 대안 (저장소·큐)
- **Postgres 하나로 상태와 큐**: 채택.
- Postgres(상태) + Redis(큐): 상태 저장과 큐 등록 사이 불일치를 별도로 처리해야 함.
- Postgres + NATS / RabbitMQ: 개인 규모에 과하고 메모리 부담.
- Redis 단독: 관계형 상태와 영속성에 약함.

## 결정

### 1. 저장소와 작업 큐
- **Postgres** 하나로 core 의 상태와 작업 큐를 모두 처리한다. 큐는 `SELECT ... FOR UPDATE SKIP LOCKED` 기반으로 구현한다.
- 클러스터 배포는 **CloudNativePG** 오퍼레이터를 쓴다 (도입 시 최신 상태 재확인).
- **DB 에는 core 만 접근한다.** 커넥터·런타임은 DB 주소도 스키마도 모른다 (I-26).

### 2. 영속성 계층: SQLAlchemy + 레포지토리 패턴 + Unit of Work
- **SQLAlchemy 2.x**(타입 있는 ORM, `Mapped[...]`)로 다루며, SQL 문자열을 직접 실행하지 않는다.
- 스키마 변경은 **Alembic** 마이그레이션으로만 한다.
- **레포지토리 포트**(예: `TaskRepository`, `SessionRepository`, `JobQueue`, `ApprovalRepository`)는 도메인 계층에 Protocol 로 정의하고,
  SQLAlchemy 구현은 영속성 어댑터 패키지에 둔다. 도메인과 유스케이스는 SQLAlchemy 를 모른다.
- **ORM 모델과 도메인 모델을 분리한다.** ORM 모델은 영속성 어댑터 안에서만 쓰고, 레포지토리는 도메인 객체를 받고 돌려준다.
- 여러 레포지토리에 걸친 변경은 **Unit of Work** 포트로 하나의 트랜잭션에 묶는다
  (예: 작업 상태를 `QUEUED` 로 바꾸고 + 큐에 등록 → 한 번에 커밋 또는 롤백).
- 구현체 교체(예: 큐를 Redis 로)는 해당 포트의 어댑터 추가와 조립 지점 변경만으로 끝나야 한다.

### 3. 데이터 모델링: 모든 경계에서 pydantic 객체
- 계약(API 요청·응답, 큐 메시지, 이벤트), 설정, 도메인 값 객체는 **pydantic 모델**로 정의한다.
  - 기본 설정: `strict=True`, `extra="forbid"`, 값 객체는 `frozen=True`.
- 설정은 **pydantic-settings** 로 로딩한다.
- 코드 안에서 **원시 JSON 문자열이나 `dict[str, Any]` 를 주고받지 않는다.** 외부 입력은 경계에서 즉시 pydantic 모델로 파싱하고, 내부에서는 객체만 다룬다 (I-27, P-13).
- 이 규칙은 정적 검사로 강제한다: `json` 모듈 직접 사용 금지(허용된 어댑터 제외), `Any` 사용 제한, mypy strict + pydantic 플러그인.

### 4. core API
- **HTTP + JSON**, 서버는 pydantic 기반 프레임워크(FastAPI)를 쓴다. OpenAPI 문서가 자동 생성된다.
- 요청·응답 모델은 **공용 계약 패키지** 하나에만 정의하고 core 와 클라이언트가 함께 쓴다.
- 클라이언트(커넥터·런타임)는 공용 계약 패키지의 **타입 있는 클라이언트**를 통해서만 core 를 호출한다. 원시 HTTP 호출을 흩뿌리지 않는다.
- 통신은 모두 **pull 방식**이다.
  - 커넥터 → core: 이벤트 전달, 보낼 메시지(outbox) 가져가기
  - 런타임 → core: 작업 가져가기(롱 폴링 + 임대 시간), 진행 상황·결과 보고
- 런타임이 임대 시간 안에 결과를 보고하지 않으면 작업은 다시 배정된다. 따라서 작업 처리와 결과 게시는 **재시도해도 안전**해야 한다 (멱등 키로 중복 방지).

### 5. 서비스 간 인증·인가

#### 배경
core API 는 클러스터 내부 모든 Pod 가 접근할 수 있다. Docker Desktop 기본 네트워크는 NetworkPolicy 를 강제하지 않으므로
네트워크 차단 대신 **호출자 인증과 역할별 인가**가 실제 방어선이다.
막아야 할 예: 침해된 MCP 서버가 위조 결과를 보내 Slack 에 게시, 개발 에이전트가 런타임을 사칭하거나 승인을 조작.

#### 검토한 대안
- 인증 없음: 위 위협에 무방비. → 기각.
- 서비스별 고정 토큰(Secret): 사람이 발급·교체해야 하고, 유출 시 계속 유효. → 기각.
- 서비스 메시 mTLS(Linkerd, Istio): 가장 강력하지만 설치·운영·메모리 부담. → 현재 기각, 향후 추가 가능.
- **ServiceAccount projected token + TokenReview**: 채택.

#### 결정
1. 서비스마다 전용 ServiceAccount 를 둔다 (`enikk-connector-slack`, `enikk-runtime-claude`, `enikk-runtime-gemini`, `enikk-dev-agent` 등).
2. core 를 제외한 자체 서비스 Pod 는 `automountServiceAccountToken: false` 로 Kubernetes API 용 기본 토큰을 받지 않는다.
   core 는 TokenReview 호출을 위해 Kubernetes API 토큰을 받되, 권한은 4번의 `system:auth-delegator` 로 한정한다.
   core 를 호출하는 Pod 는 **audience=`enikk-core`, 만료 600초**인 projected token 만 받는다. 교체는 kubelet 이 자동으로 한다.
3. 공용 core 클라이언트가 매 요청마다 토큰 파일을 읽어 `Authorization: Bearer` 로 보낸다. 서비스 코드는 이를 직접 다루지 않는다.
4. core 는 Kubernetes **TokenReview**(audience=`enikk-core`)로 토큰을 검증하고 결과를 짧게(1~2분) 캐시한다.
   core 의 ServiceAccount 에는 `system:auth-delegator` 권한만 준다.
5. 검증된 ServiceAccount 를 역할로 매핑하고, API 마다 허용 역할을 선언한다. 매핑은 core 설정(pydantic 모델)으로 관리한다.

   | 역할 | 허용 |
   |---|---|
   | connector | 이벤트 전달, 자기 outbox 가져가기 |
   | runtime (엔진별) | 자기 엔진의 작업만 가져가기, 자기가 임대한 작업에만 진행·결과 보고 |
   | dev-agent | 자기에게 배정된 개선 작업의 결과 보고. 승인 관련 API 불가 |
   | 그 외 | 모두 거부 |

#### 근거
- 관리할 비밀값이 생기지 않는다 (I-21). 토큰은 짧은 주기로 자동 교체된다.
- audience 한정으로, 탈취된 토큰을 Kubernetes API 등 다른 곳에 쓸 수 없다.

#### 한계
- 클러스터 내부 통신은 암호화되지 않는다 (단일 노드 로컬 환경이라 위험이 낮다고 판단).
- Pod 가 통째로 장악되면 토큰 유효 시간 동안 그 Pod 의 권한 범위로 호출할 수 있다.
- 암호화가 필요해지면 서비스 메시(mTLS)를 이 방식과 함께 추가한다.

## 결과
- 상태 변경과 작업 등록이 한 트랜잭션이라 둘 중 하나만 반영되는 불일치가 생기지 않는다.
- 단, 큐를 다른 저장소(Redis 등)로 바꾸면 이 트랜잭션 보장이 사라진다. 그때는 **아웃박스 패턴**(DB 에 이벤트를 먼저 기록하고 별도로 큐에 전달)을 함께 도입해야 한다.
- ORM 모델과 도메인 모델 사이 변환 코드가 생긴다. 이 비용은 도메인이 영속성 기술에서 독립하는 대가로 받아들인다.
- 마이그레이션 없는 스키마 변경, 원시 SQL, 원시 JSON 처리는 검사 단계에서 실패한다.

## 재검토 조건
- 작업 처리량이 Postgres 큐의 한계에 가까워질 때.
