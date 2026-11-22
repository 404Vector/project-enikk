# Isolated dev agent image (ADR-0021): the Python app plus the tools the agent works
# with - git, gh and uv - so it can clone, run the project checks and open a PR.
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
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git gh ca-certificates \
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
    UV_PYTHON_DOWNLOADS=never
USER 10001:10001
