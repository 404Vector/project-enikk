# Isolated dev agent image (ADR-0021): the Python app plus the tools the agent works
# with - git, gh, uv and a Postgres server for the tests - so it can clone, run the
# project checks and open a PR.
# Build: docker build -f deploy/docker/dev-agent.Dockerfile .

ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package enikk-dev-agent

FROM ${PYTHON_IMAGE}
# PostgreSQL's apt repository (PGDG) key, pinned by checksum. Its server binaries let the
# persistence tests start a temporary Postgres without Docker (docs/development/testing.md).
ADD --chmod=644 \
    --checksum=sha256:0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76 \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc /usr/share/keyrings/pgdg.asc
# Same major version as the CloudNativePG cluster (ADR-0012 §1). No cluster is created
# at install time; the tests make their own under /tmp.
ARG POSTGRES_MAJOR=18
RUN echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && mkdir -p /etc/postgresql-common \
 && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf \
 && apt-get update \
 && apt-get install --yes --no-install-recommends git gh ca-certificates \
      "postgresql-${POSTGRES_MAJOR}" \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=build /app/.venv /app/.venv
# The definitions come with the release; the agent edits its own clone, not these.
COPY --from=build /src/agents /app/agents
COPY --from=build /src/config /app/config
# uv uses the image's Python for the project checks; it never downloads one.
ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    ENIKK_TEST_POSTGRES_BIN=/usr/lib/postgresql/${POSTGRES_MAJOR}/bin
USER 10001:10001
