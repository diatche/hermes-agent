"""Behavior tests for the macOS HermesGateway Swift supervisor."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
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
        "if [[ $1 == --json ]]; then printf '{\"wrapper-grace\":1.1,\"controller-wait\":1.1}\\n'; exit 0; fi\n"
        "if [[ $1 == config && $2 == get ]]; then printf '0.4\\n'; exit 0; fi\n"
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
    ).replace(
        'let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"',
        f"let timeoutBudgetScript = {json.dumps(str(fake_hermes))}",
    )
    assert str(fake_hermes) in source
    source = source.replace(
        'let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"',
        f"let timeoutStatePath = {json.dumps(str(tmp_path / 'timeout-state.json'))}",
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
        timeout_state = json.loads(
            (tmp_path / "timeout-state.json").read_text(encoding="utf-8")
        )
        assert timeout_state == {
            "controller_wait": 1.1,
            "pid": supervisor.pid,
            "wrapper_grace": 1.1,
        }
        commands = subprocess.run(
            ["ps", "-o", "command=", "-p", ",".join(map(str, child_pids))],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # macOS may canonicalize /private/var to /var when directly executing
        # a script from pytest's temporary directory.
        expected_command_path = str(fake_hermes).replace("/private/var/", "/var/")
        assert expected_command_path in commands
        assert "gateway run --replace --external-supervisor" in commands
        assert "import os, sys; os.execve" not in commands

        supervisor.send_signal(signal.SIGTERM)
        time.sleep(0.6)

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
def test_supervisor_derives_force_kill_deadline_from_gateway_config(
    tmp_path: Path,
) -> None:
    fake_hermes = tmp_path / "fake-hermes.sh"
    ready = tmp_path / "children-ready"
    fake_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == --json ]]; then printf '{\"wrapper-grace\":0.5,\"controller-wait\":0.5}\\n'; exit 0; fi\n"
        "if [[ $1 == config && $2 == get ]]; then printf '0.2\\n'; exit 0; fi\n"
        "trap '' TERM INT\n"
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
        'let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"',
        f"let timeoutBudgetScript = {json.dumps(str(fake_hermes))}",
    )
    source = source.replace(
        'let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"',
        f"let timeoutStatePath = {json.dumps(str(tmp_path / 'timeout-state.json'))}",
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
        while time.monotonic() < deadline:
            child_pids = _children(supervisor.pid)
            if len(child_pids) == 2 and ready.exists() and len(
                ready.read_text(encoding="utf-8").splitlines()
            ) == 2:
                break
            time.sleep(0.05)
        assert len(child_pids) == 2

        started = time.monotonic()
        supervisor.send_signal(signal.SIGTERM)
        _, stderr = supervisor.communicate(timeout=3)

        assert supervisor.returncode == 0
        assert 0.4 <= time.monotonic() - started < 2
        assert stderr.count("force-killing child group pid=") == 2
        assert all(not _running(pid) for pid in child_pids)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)
        for pid in child_pids:
            if _running(pid):
                subprocess.run(
                    ["/bin/kill", "-KILL", f"-{pid}"],
                    check=False,
                    capture_output=True,
                    text=True,
                )


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS Swift wrapper")
def test_supervisor_kills_and_reaps_timed_out_budget_resolver(tmp_path: Path) -> None:
    fixture_id = uuid.uuid4().hex
    resolver = Path("/tmp") / f"hermes-timeout-resolver-{fixture_id}.sh"
    resolver_pid = Path("/tmp") / f"hermes-timeout-resolver-{fixture_id}.pid"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
        f"echo $$ > {json.dumps(str(resolver_pid))}\n"
        "trap '' TERM INT\n"
        "while :; do /bin/sleep 0.1; done\n",
        encoding="utf-8",
    )
    resolver.chmod(0o755)
    source = SOURCE.read_text(encoding="utf-8")
    source = source.replace(
        'let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"',
        f"let timeoutBudgetScript = {json.dumps(str(resolver))}",
    ).replace(
        "let configReadTimeout: TimeInterval = 5",
        "let configReadTimeout: TimeInterval = 0.2",
    ).replace(
        'let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"',
        f"let timeoutStatePath = {json.dumps(str(tmp_path / 'timeout-state.json'))}",
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

    try:
        result = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )

        assert result.returncode == 78
        match = re.search(r"timed out resolving wrapper shutdown budget pid=(\d+)", result.stderr)
        assert match, result.stderr
        pid = int(match.group(1))
        assert not _running(pid)
        assert "timed out resolving wrapper shutdown budget" in result.stderr
    finally:
        resolver.unlink(missing_ok=True)
        resolver_pid.unlink(missing_ok=True)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS Swift wrapper")
def test_launch_registration_race_does_not_orphan_started_child(tmp_path: Path) -> None:
    fake_hermes = tmp_path / "fake-hermes.sh"
    dashboard_pid_file = tmp_path / "dashboard-pid"
    fake_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == --json ]]; then printf '{\"wrapper-grace\":0.3,\"controller-wait\":0.3}\\n'; exit 0; fi\n"
        "if [[ $1 == config && $2 == get ]]; then printf '0\\n'; exit 0; fi\n"
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
        'let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"',
        f"let timeoutBudgetScript = {json.dumps(str(fake_hermes))}",
    ).replace(
        launch_boundary,
        '            try process.run()\n'
        '            if name == "dashboard" { Thread.sleep(forTimeInterval: 0.4) }\n'
        "            let groupDeadline =",
    )
    source = source.replace(
        'let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"',
        f"let timeoutStatePath = {json.dumps(str(tmp_path / 'timeout-state.json'))}",
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
        "if [[ $1 == --json ]]; then printf '{\"wrapper-grace\":0.3,\"controller-wait\":0.3}\\n'; exit 0; fi\n"
        "if [[ $1 == config && $2 == get ]]; then printf '0\\n'; exit 0; fi\n"
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
        'let timeoutBudgetScript = "/Users/diatche/.hermes/hermes-agent/scripts/hermes-wrapper-timeout-budget.py"',
        f"let timeoutBudgetScript = {json.dumps(str(fake_hermes))}",
    )
    source = source.replace(
        'let timeoutStatePath = "/Users/diatche/.hermes/local/run/hermes-gateway-wrapper-timeouts.json"',
        f"let timeoutStatePath = {json.dumps(str(tmp_path / 'timeout-state.json'))}",
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
