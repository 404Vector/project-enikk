"""Composition root: the only place adapters are constructed."""

from __future__ import annotations

from functools import cached_property

from enikk_kernel.clock import SystemClock
from enikk_kernel.process import SubprocessRunner
from enikk_ops.bootstrap import ArgoCdService
from enikk_ops.secrets import CredentialSync, SecretFile
from enikk_ops.settings import OpsSettings
from enikk_ops.smoke import CoreSmokeTest
from enikk_platform.adapters import HelmCli, KubectlCli


class Container:
    def __init__(self, *, dry_run: bool = False, settings: OpsSettings | None = None) -> None:
        self._dry_run = dry_run
        self._settings = settings

    @cached_property
    def settings(self) -> OpsSettings:
        return self._settings or OpsSettings()

    @cached_property
    def runner(self) -> SubprocessRunner:
        return SubprocessRunner(dry_run=self._dry_run)

    @cached_property
    def argocd(self) -> ArgoCdService:
        return ArgoCdService(self.settings, HelmCli(self.runner), KubectlCli(self.runner))

    @cached_property
    def secret_file(self) -> SecretFile:
        return SecretFile(self.settings.secrets.file, SystemClock())

    @cached_property
    def credential_sync(self) -> CredentialSync:
        return CredentialSync(
            self.settings.secrets,
            self.settings.kube_context,
            self.secret_file,
            KubectlCli(self.runner),
        )

    @cached_property
    def core_smoke(self) -> CoreSmokeTest:
        return CoreSmokeTest(
            self.settings.smoke, self.settings.kube_context, KubectlCli(self.runner)
        )
