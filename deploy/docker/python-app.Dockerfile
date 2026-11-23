# Shared image for every Python app in the uv workspace.
# Build: docker build -f deploy/docker/python-app.Dockerfile --build-arg PACKAGE=enikk-core .
# The container command is set by each app's Helm chart.

ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
RUN test -n "${PACKAGE}" \
 && uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM ${PYTHON_IMAGE}
ARG PACKAGE
# The Claude runtime reads and works on the repository in the shared volume with git
# (ADR-0037 §7); core checks and pushes a session's bundle from its own clone (§6b).
# Other apps get no extra packages.
RUN if [ "${PACKAGE}" = "enikk-runtime-claude" ] || [ "${PACKAGE}" = "enikk-core" ]; then \
      apt-get update \
      && apt-get install --yes --no-install-recommends git ca-certificates \
      && rm -rf /var/lib/apt/lists/*; \
    fi
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build /app/.venv /app/.venv
# Agent definitions and interface routing are part of the release (ADR-0012 §6, ADR-0018).
COPY --from=build /src/agents /app/agents
COPY --from=build /src/config /app/config
ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
