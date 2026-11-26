# Shared image for every Python app in the uv workspace.
# Build: docker build -f deploy/docker/python-app.Dockerfile --build-arg PACKAGE=enikk-core .
# The container command is set by each app's Helm chart.
# Third-party dependencies and the workspace's own packages are separate layers (ADR-0048).

ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

# Only the workspace members' pyproject.toml files.
FROM ${PYTHON_IMAGE} AS manifests
WORKDIR /src
COPY src src
RUN find src -type f ! -name pyproject.toml -delete \
 && find src -depth -type d -empty -delete

# The app's third-party dependencies, without any workspace package.
FROM ${PYTHON_IMAGE} AS deps
COPY --from=uv /uv /bin/uv
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
COPY --from=manifests /src/src src
RUN test -n "${PACKAGE}" \
 && uv sync --frozen --no-dev --no-editable --no-install-workspace --package "${PACKAGE}"

# The workspace packages, moved out of the environment into /out: each enikk_* entry of
# site-packages and the commands its RECORD lists under bin/.
FROM deps AS build
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=0
COPY src src
RUN uv sync --frozen --no-dev --no-editable --package "${PACKAGE}" \
 && site="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
 && /app/.venv/bin/python -m compileall -q "${site}"/enikk_* \
 && mkdir -p "/out${site}" /out/app/.venv/bin \
 && for record in "${site}"/enikk_*.dist-info/RECORD; do \
      for script in $(sed -n 's#^\.\./\.\./\.\./bin/\([^,]*\),.*#\1#p' "${record}"); do \
        mv "/app/.venv/bin/${script}" /out/app/.venv/bin/; \
      done; \
    done \
 && mv "${site}"/enikk_* "/out${site}/"

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
# The Claude runtime also gets uv, so a session can run the project checks in its
# worktree (ADR-0037 4a). The check tools come from uv.lock there, not from this image.
RUN --mount=type=bind,from=uv,source=/uv,target=/mnt/uv \
    if [ "${PACKAGE}" = "enikk-runtime-claude" ]; then \
      install -m 0755 /mnt/uv /usr/local/bin/uv; \
    fi
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=deps /app/.venv /app/.venv
COPY --from=build /out/ /
# Agent definitions and interface routing are part of the release (ADR-0012 §6, ADR-0018).
COPY agents /app/agents
COPY config /app/config
ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
