from __future__ import annotations

import sys

import pytest

from enikk_core.errors import ExternalCommandError, ToolNotFoundError
from enikk_core.process import SubprocessRunner
from enikk_core.testing import RecordingRunner


def test_captures_stdout() -> None:
    result = SubprocessRunner().run([sys.executable, "-c", "print('hi')"], mutating=False)
    assert result.ok
    assert result.stdout.strip() == "hi"


def test_passes_input_via_stdin() -> None:
    code = "import sys; print(sys.stdin.read().upper())"
    result = SubprocessRunner().run([sys.executable, "-c", code], input="secret", mutating=False)
    assert result.stdout.strip() == "SECRET"


def test_raises_on_failure_when_checked() -> None:
    with pytest.raises(ExternalCommandError) as exc:
        SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(3)"])
    assert exc.value.exit_code == 3


def test_returns_failure_when_unchecked() -> None:
    result = SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(3)"], check=False)
    assert result.returncode == 3


def test_dry_run_skips_mutating_but_runs_queries() -> None:
    runner = SubprocessRunner(dry_run=True)
    skipped = runner.run([sys.executable, "-c", "raise SystemExit(1)"])
    assert skipped.ok
    query = runner.run([sys.executable, "-c", "print('q')"], mutating=False)
    assert query.stdout.strip() == "q"


def test_missing_tool() -> None:
    with pytest.raises(ToolNotFoundError):
        SubprocessRunner().run(["definitely-not-a-real-binary-xyz"])


def test_recording_runner_longest_prefix_wins() -> None:
    runner = RecordingRunner()
    runner.when("helm", stdout="generic")
    runner.when("helm", "status", stdout="specific")
    assert runner.run(["helm", "status", "x"]).stdout == "specific"
    assert runner.run(["helm", "lint"]).stdout == "generic"
    assert runner.argvs == [("helm", "status", "x"), ("helm", "lint")]
