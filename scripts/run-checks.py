#!/usr/bin/env python3
"""Supervise POSIX automatic checks under one deadline, or run manual deep tests."""

from __future__ import annotations

import argparse
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUTOMATIC_BUDGET_SECONDS = 300.0
TERMINATION_RESERVE_SECONDS = 6.0
TERMINATION_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.05
TIMEOUT_EXIT_CODE = 124
OWNERSHIP_FAILED_EXIT_CODE = 125


@dataclass
class _PendingSignal:
    number: int | None = None


@contextmanager
def _termination_signals() -> Iterator[_PendingSignal]:
    pending = _PendingSignal()
    previous = {}

    def record_signal(number: int, _frame: FrameType | None) -> None:
        # Recording instead of raising keeps Popen ownership and cleanup atomic
        # with respect to asynchronous delivery, including repeated signals.
        if pending.number is None:
            pending.number = number

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            previous[number] = signal.signal(number, record_signal)
        yield pending
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _group_exists(process: subprocess.Popen[bytes]) -> bool:
    process.poll()  # Reap the leader before probing the whole owned group.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_empty(process: subprocess.Popen[bytes], deadline: float) -> bool:
    while _group_exists(process):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(POLL_SECONDS, remaining))
    return process.poll() is not None


def _signal_group(process: subprocess.Popen[bytes], number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


def _terminate_group(process: subprocess.Popen[bytes], deadline: float) -> bool:
    remaining = max(0.0, deadline - time.monotonic())
    _signal_group(process, signal.SIGTERM)
    graceful_deadline = min(
        deadline,
        time.monotonic() + min(TERMINATION_GRACE_SECONDS, remaining / 2),
    )
    if _wait_empty(process, graceful_deadline):
        return True
    _signal_group(process, signal.SIGKILL)
    return _wait_empty(process, deadline)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    budget_seconds: float | None,
) -> int:
    """Own one session, including descendants after its original leader exits.

    Deliberately detached process groups/sessions and uncatchable supervisor
    termination are outside this POSIX ownership guarantee.
    """

    if os.name != "posix":
        print("bounded OPDS checks require POSIX process groups", file=sys.stderr)
        return OWNERSHIP_FAILED_EXIT_CODE
    if budget_seconds is not None and (
        not math.isfinite(budget_seconds)
        or not 0 < budget_seconds <= AUTOMATIC_BUDGET_SECONDS
    ):
        raise ValueError("automatic budget must be positive and at most 300 seconds")
    started = time.monotonic()
    deadline = None if budget_seconds is None else started + budget_seconds
    reserve = (
        TERMINATION_RESERVE_SECONDS
        if budget_seconds is None
        else min(TERMINATION_RESERVE_SECONDS, budget_seconds / 4)
    )
    execution_deadline = None if deadline is None else deadline - reserve
    print("+", shlex.join(command), flush=True)
    process: subprocess.Popen[bytes] | None = None
    tree_empty = False
    with _termination_signals() as pending:
        try:
            process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
            timed_out = False
            while process.poll() is None and pending.number is None:
                now = time.monotonic()
                if execution_deadline is not None and now >= execution_deadline:
                    timed_out = True
                    break
                wait = POLL_SECONDS
                if execution_deadline is not None:
                    wait = min(wait, max(0.0, execution_deadline - now))
                try:
                    process.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    pass

            cleanup_deadline = (
                time.monotonic() + reserve if deadline is None else deadline
            )
            if pending.number is not None or timed_out:
                reason = "interrupted" if pending.number is not None else "timed out"
                print(f"checks {reason}; terminating owned process group", flush=True)
                tree_empty = _terminate_group(process, cleanup_deadline)
                status = (
                    128 + pending.number
                    if pending.number is not None
                    else TIMEOUT_EXIT_CODE
                )
            else:
                # Normal leader exit is not evidence that descendants exited.
                tree_empty = _wait_empty(
                    process, min(cleanup_deadline, time.monotonic() + 0.25)
                )
                if tree_empty:
                    status = process.returncode
                    assert status is not None
                else:
                    print(
                        "check leader exited with surviving descendants",
                        file=sys.stderr,
                        flush=True,
                    )
                    tree_empty = _terminate_group(process, cleanup_deadline)
                    status = OWNERSHIP_FAILED_EXIT_CODE
            if not tree_empty:
                print(
                    "could not verify an empty owned process group before deadline",
                    file=sys.stderr,
                    flush=True,
                )
                status = OWNERSHIP_FAILED_EXIT_CODE
            elapsed = time.monotonic() - started
            if pending.number is not None and status == 0:
                status = 128 + pending.number
            if deadline is not None and time.monotonic() > deadline and status == 0:
                status = TIMEOUT_EXIT_CODE
            print(
                f"checks finished: {elapsed:.2f}s, exit={status}, "
                f"owned_group_empty={tree_empty}",
                flush=True,
            )
            return status
        finally:
            if process is not None and not tree_empty:
                cleanup_deadline = time.monotonic() + reserve
                if deadline is not None:
                    cleanup_deadline = min(cleanup_deadline, deadline)
                _terminate_group(process, cleanup_deadline)


def _command(profile: str) -> tuple[str, ...]:
    if profile == "full":
        return ("bash", str(REPOSITORY_ROOT / "scripts/check-full-steps.sh"))
    selection = "deep" if profile == "deep" else "not deep"
    return (sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-m", selection)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("full", "pytest", "deep"))
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=None,
        help="reduce the automatic 300-second budget (never increase it)",
    )
    args = parser.parse_args(arguments)
    if args.profile == "deep" and args.budget_seconds is not None:
        parser.error(
            "deep is explicitly manual and does not accept an automatic budget"
        )
    budget = (
        None
        if args.profile == "deep"
        else (
            AUTOMATIC_BUDGET_SECONDS
            if args.budget_seconds is None
            else args.budget_seconds
        )
    )
    print(
        "Manual profile: only deep-marked tests; no execution deadline."
        if budget is None
        else f"Automatic {args.profile} profile: {budget:g}s aggregate deadline.",
        flush=True,
    )
    try:
        return run_command(
            _command(args.profile), cwd=REPOSITORY_ROOT, budget_seconds=budget
        )
    except (OSError, ValueError) as error:
        print(f"check supervisor failed: {error}", file=sys.stderr, flush=True)
        return OWNERSHIP_FAILED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
