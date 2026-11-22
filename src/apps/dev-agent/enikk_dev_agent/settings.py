"""Settings, read from ``ENIKK_DEV_*`` environment variables set by the Job template."""

from __future__ import annotations

import uuid
from pathlib import Path

from enikk_core_client.tokens import PROJECTED_TOKEN_FILE
from enikk_kernel.settings import ServiceSettings


class DevAgentSettings(ServiceSettings, env_prefix="ENIKK_DEV_"):
    task_id: uuid.UUID
    core_url: str = "http://enikk-core.enikk.svc"
    token_file: Path = PROJECTED_TOKEN_FILE
    langfuse_url: str = "https://jp.cloud.langfuse.com"
    workspace: Path = Path("/workspace")
    """Where the repository is cloned. The engine's working directory is inside it."""
    scratch_dir: Path = Path("/tmp/enikk")  # noqa: S108 - the Pod's private emptyDir
    tool_home: Path = Path("/tmp/tools")  # noqa: S108 - the Pod's private emptyDir
    """git and gh configuration and tool caches; removed when the task ends."""
    tracing: bool = True
    verbose: bool = False
