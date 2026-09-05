import importlib.util
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/run-checks.py"
    specification = importlib.util.spec_from_file_location("opds_check_runner", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_supervisor_preserves_command_failure(
    runner: ModuleType, tmp_path: Path
) -> None:
    assert (
        runner.run_command(
            (sys.executable, "-c", "raise SystemExit(17)"),
            cwd=tmp_path,
            budget_seconds=2,
        )
        == 17
    )


def test_deadline_is_shared_across_sequential_work_and_cleanup(
    runner: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    completed_stages: list[str] = []

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    class Process:
        returncode: int | None = None
        termination_at: float | None = None

        def poll(self) -> int | None:
            if self.termination_at is not None and elapsed >= self.termination_at:
                self.returncode = -signal.SIGTERM
            return self.returncode

        def wait(self, timeout: float) -> None:
            advance(timeout)
            for name, finished_at in (("first", 0.0), ("second", 0.3), ("last", 0.6)):
                if elapsed >= finished_at and name not in completed_stages:
                    completed_stages.append(name)
            raise subprocess.TimeoutExpired("sequential stages", timeout)

    process = Process()

    def spawn(
        _command: Sequence[str], *, cwd: Path, start_new_session: bool
    ) -> Process:
        assert cwd == tmp_path and start_new_session
        return process

    def terminate(_process: Process, number: int) -> None:
        assert _process is process and number == signal.SIGTERM
        process.termination_at = elapsed + 0.05

    monkeypatch.setattr(
        runner, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)
    )
    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(runner, "_group_exists", lambda child: child.poll() is None)
    monkeypatch.setattr(runner, "_signal_group", terminate)
    status = runner.run_command(
        ("sequential stages",), cwd=tmp_path, budget_seconds=0.7
    )
    assert status == runner.TIMEOUT_EXIT_CODE
    assert completed_stages == ["first", "second"]
    assert elapsed == pytest.approx(0.575)
    assert "owned_group_empty=True" in capsys.readouterr().out


def test_timeout_force_kills_a_term_resistant_leader(
    runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = """
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(30)
"""
    status = runner.run_command(
        (sys.executable, "-c", command), cwd=tmp_path, budget_seconds=0.6
    )
    assert status == runner.TIMEOUT_EXIT_CODE
    assert "owned_group_empty=True" in capsys.readouterr().out


def test_timeout_terminates_and_reaps_a_real_descendant(
    runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = """
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path("child.pid").write_text(str(child.pid))
def terminate(number, frame):
    child.wait(timeout=2)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
time.sleep(30)
"""
    status = runner.run_command(
        (sys.executable, "-c", command), cwd=tmp_path, budget_seconds=2
    )
    assert status == runner.TIMEOUT_EXIT_CODE
    child_pid = int((tmp_path / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert "owned_group_empty=True" in capsys.readouterr().out


def test_successful_leader_cannot_hide_a_surviving_descendant(
    runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = """
import subprocess
import sys
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path("child.pid").write_text(str(child.pid))
"""
    status = runner.run_command(
        (sys.executable, "-c", command), cwd=tmp_path, budget_seconds=2
    )
    assert status == runner.OWNERSHIP_FAILED_EXIT_CODE
    output = capsys.readouterr()
    assert "leader exited with surviving descendants" in output.err
    assert "owned_group_empty=True" in output.out


@pytest.mark.parametrize(
    "termination_signal", (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
)
def test_signal_at_spawn_boundary_cannot_escape_process_ownership(
    runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination_signal: signal.Signals,
) -> None:
    original = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def spawn_then_signal(
        command: tuple[str, ...], *, cwd: Path, start_new_session: bool
    ) -> subprocess.Popen[bytes]:
        process = original(command, cwd=cwd, start_new_session=start_new_session)
        processes.append(process)
        os.kill(os.getpid(), termination_signal)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", spawn_then_signal)
    status = runner.run_command(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        budget_seconds=2,
    )
    assert status == 128 + termination_signal
    assert len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.parametrize("budget", (0, -1, 301, float("inf"), float("nan")))
def test_automatic_deadline_cannot_be_disabled_or_extended(
    runner: ModuleType, tmp_path: Path, budget: float
) -> None:
    with pytest.raises(ValueError, match="at most 300"):
        runner.run_command(
            (sys.executable, "-c", "pass"), cwd=tmp_path, budget_seconds=budget
        )


def test_full_profile_supervises_all_stages_and_deep_is_manual(
    runner: ModuleType,
) -> None:
    assert runner._command("full") == (
        "bash",
        str(runner.REPOSITORY_ROOT / "scripts/check-full-steps.sh"),
    )
    assert runner._command("pytest")[-2:] == ("-m", "not deep")
    assert runner._command("deep")[-2:] == ("-m", "deep")
    with pytest.raises(SystemExit) as error:
        runner.main(["deep", "--budget-seconds", "300"])
    assert error.value.code == 2
