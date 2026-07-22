"""Behavior tests for the macOS HermesGateway Swift supervisor."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "HermesGateway.swift"


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _children(parent: int) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-P", str(parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(line) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS Swift wrapper")
def test_supervisor_waits_for_children_before_exiting_on_sigterm(tmp_path: Path) -> None:
    fake_hermes = tmp_path / "fake-hermes.sh"
    ready = tmp_path / "children-ready"
    fake_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "trap 'sleep 1; exit 0' TERM INT\n"
        f"printf 'ready\\n' >> {json.dumps(str(ready))}\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)

    source = SOURCE.read_text(encoding="utf-8")
    source = source.replace(
        'let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"',
        f"let hermes = {json.dumps(str(fake_hermes))}",
    )
    assert str(fake_hermes) in source
    staged_source = tmp_path / "HermesGateway.swift"
    staged_source.write_text(source, encoding="utf-8")
    executable = tmp_path / "HermesGateway"
    subprocess.run(
        ["xcrun", "swiftc", str(staged_source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )

    supervisor = subprocess.Popen(
        [str(executable)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pids: list[int] = []
    try:
        deadline = time.monotonic() + 5
        ready_count = 0
        while time.monotonic() < deadline:
            child_pids = _children(supervisor.pid)
            ready_count = (
                len(ready.read_text(encoding="utf-8").splitlines())
                if ready.exists()
                else 0
            )
            if len(child_pids) == 2 and ready_count == 2:
                break
            time.sleep(0.05)
        assert len(child_pids) == 2
        assert ready_count == 2
        assert all(os.getpgid(pid) == pid for pid in child_pids)
        commands = subprocess.run(
            ["ps", "-o", "command=", "-p", ",".join(map(str, child_pids))],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert str(fake_hermes) in commands
        assert "import os, sys; os.execve" not in commands

        supervisor.send_signal(signal.SIGTERM)
        time.sleep(0.2)

        assert supervisor.poll() is None, "supervisor exited before its children"
        supervisor.wait(timeout=5)
        assert supervisor.returncode == 0
        assert all(not _running(pid) for pid in child_pids)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)
        for pid in child_pids:
            if _running(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS Swift wrapper")
def test_launch_registration_race_does_not_orphan_started_child(tmp_path: Path) -> None:
    fake_hermes = tmp_path / "fake-hermes.sh"
    dashboard_pid_file = tmp_path / "dashboard-pid"
    fake_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == gateway ]]; then sleep 0.1; exit 7; fi\n"
        f"printf '%s\\n' \"$$\" > {json.dumps(str(dashboard_pid_file))}.tmp\n"
        f"mv {json.dumps(str(dashboard_pid_file))}.tmp {json.dumps(str(dashboard_pid_file))}\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)

    source = SOURCE.read_text(encoding="utf-8")
    launch_boundary = "            try process.run()\n            let groupDeadline ="
    assert source.count(launch_boundary) == 1
    source = source.replace(
        'let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"',
        f"let hermes = {json.dumps(str(fake_hermes))}",
    ).replace(
        launch_boundary,
        '            try process.run()\n'
        '            if name == "dashboard" { Thread.sleep(forTimeInterval: 0.4) }\n'
        "            let groupDeadline =",
    ).replace(
        "func shutdownAndWait(timeout: TimeInterval = 30)",
        "func shutdownAndWait(timeout: TimeInterval = 0.3)",
    )
    staged_source = tmp_path / "HermesGateway.swift"
    staged_source.write_text(source, encoding="utf-8")
    executable = tmp_path / "HermesGateway"
    subprocess.run(
        ["xcrun", "swiftc", str(staged_source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )

    supervisor = subprocess.Popen(
        [str(executable)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dashboard_pid: int | None = None
    try:
        supervisor.wait(timeout=5)
        assert dashboard_pid_file.exists(), "race proof never started dashboard"
        dashboard_pid = int(dashboard_pid_file.read_text(encoding="utf-8"))
        assert not _running(dashboard_pid), "started dashboard escaped registration"
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)
        if dashboard_pid is not None and _running(dashboard_pid):
            os.kill(dashboard_pid, signal.SIGKILL)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS Swift wrapper")
def test_supervisor_force_kills_child_after_shutdown_timeout(tmp_path: Path) -> None:
    fake_hermes = tmp_path / "fake-hermes.sh"
    ready = tmp_path / "children-ready"
    descendants = tmp_path / "descendant-pids"
    fake_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "trap '' TERM INT\n"
        "(trap '' TERM INT; while :; do sleep 0.1; done) &\n"
        f"echo $! >> {json.dumps(str(descendants))}\n"
        f"printf 'ready\\n' >> {json.dumps(str(ready))}\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)

    source = SOURCE.read_text(encoding="utf-8")
    source = source.replace(
        'let hermes = "/Users/diatche/.hermes/hermes-agent/venv/bin/hermes"',
        f"let hermes = {json.dumps(str(fake_hermes))}",
    ).replace(
        "func shutdownAndWait(timeout: TimeInterval = 30)",
        "func shutdownAndWait(timeout: TimeInterval = 0.3)",
    )
    staged_source = tmp_path / "HermesGateway.swift"
    staged_source.write_text(source, encoding="utf-8")
    executable = tmp_path / "HermesGateway"
    subprocess.run(
        ["xcrun", "swiftc", str(staged_source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )

    supervisor = subprocess.Popen(
        [str(executable)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pids: list[int] = []
    try:
        deadline = time.monotonic() + 5
        ready_count = 0
        while time.monotonic() < deadline:
            child_pids = _children(supervisor.pid)
            ready_count = (
                len(ready.read_text(encoding="utf-8").splitlines())
                if ready.exists()
                else 0
            )
            if len(child_pids) == 2 and ready_count == 2:
                break
            time.sleep(0.05)
        assert len(child_pids) == 2
        assert ready_count == 2
        descendant_pids = [
            int(value)
            for value in descendants.read_text(encoding="utf-8").splitlines()
        ]
        assert len(descendant_pids) == 2

        started = time.monotonic()
        supervisor.send_signal(signal.SIGTERM)
        _, stderr = supervisor.communicate(timeout=5)

        assert supervisor.returncode == 0
        assert time.monotonic() - started >= 0.25
        assert stderr.count("force-killing child group pid=") == 2
        assert all(not _running(pid) for pid in child_pids)
        assert all(not _running(pid) for pid in descendant_pids)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)
        for pid in child_pids:
            if _running(pid):
                os.kill(pid, signal.SIGKILL)
