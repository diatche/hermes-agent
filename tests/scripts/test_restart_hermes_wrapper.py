"""Behavior tests for the port-authoritative Hermes wrapper controller."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/restart-hermes-wrapper.sh"


def _command(directory: Path, name: str, content: str) -> Path:
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o755)
    return target


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    calls = tmp_path / "calls"
    loaded = tmp_path / "loaded"
    listener = tmp_path / "listener"
    listener.write_text("7711\n", encoding="utf-8")

    app = tmp_path / "HermesGateway.app" / "Contents" / "MacOS"
    _command(app, "HermesGateway", "#!/bin/sh\nexit 0\n")
    entrypoint = _command(tmp_path, "entrypoint", "#!/bin/sh\nexit 0\n")
    health = tmp_path / "health.py"
    health.write_text("# test health script\n", encoding="utf-8")
    python = _command(
        fake_bin,
        "health-python",
        f"#!/bin/sh\nprintf 'health %s\\n' \"$*\" >> '{calls}'\nexit 0\n",
    )
    plist = tmp_path / "wrapper.plist"
    plist.write_text("plist\n", encoding="utf-8")

    _command(
        fake_bin,
        "plutil",
        f"#!/bin/sh\nprintf 'plutil %s\\n' \"$*\" >> '{calls}'\nexit 0\n",
    )
    _command(
        fake_bin,
        "lsof",
        f"#!/bin/sh\nif [ -f '{listener}' ]; then cat '{listener}'; exit 0; fi\nexit 1\n",
    )
    _command(
        fake_bin,
        "launchctl",
        "#!/bin/sh\n"
        f"printf 'launchctl %s\\n' \"$*\" >> '{calls}'\n"
        f"if [ \"$1\" = print ]; then [ -f '{loaded}' ]; exit $?; fi\n"
        f"if [ \"$1\" = bootout ]; then rm -f '{loaded}'; exit 0; fi\n"
        f"if [ \"$1\" = bootstrap ]; then touch '{loaded}'; exit 0; fi\n"
        f"if [ \"$1\" = kickstart ]; then touch '{loaded}'; printf '8822\\n' > '{listener}'; exit 0; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "kill-command",
        f"#!/bin/sh\nprintf 'kill %s\\n' \"$*\" >> '{calls}'\nrm -f '{listener}'\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HOME": str(tmp_path),
            "HERMES_WRAPPER_DOMAIN": "gui/501",
            "HERMES_WRAPPER_APP": str(tmp_path / "HermesGateway.app"),
            "HERMES_WRAPPER_PLIST": str(plist),
            "HERMES_WRAPPER_ENTRYPOINT": str(entrypoint),
            "HERMES_WRAPPER_HEALTH_SCRIPT": str(health),
            "HERMES_WRAPPER_HEALTH_PYTHON": str(python),
            "HERMES_WRAPPER_KILL_BIN": str(fake_bin / "kill-command"),
            "HERMES_WRAPPER_LOG_DIR": str(tmp_path / "logs"),
            "HERMES_WRAPPER_START_WAIT": "2",
            "HERMES_WRAPPER_PORT_TERM_WAIT": "1",
            "HERMES_WRAPPER_PORT_KILL_WAIT": "1",
            "HERMES_MAINTENANCE_ACTIVE": str(tmp_path / "active.json"),
        }
    )
    return env, calls, listener


def _run(tmp_path: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    env, calls, listener = _environment(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, calls, listener


def test_stop_boots_out_wrapper_and_terminates_port_listener(tmp_path: Path) -> None:
    result, calls, listener = _run(tmp_path, "--stop")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not listener.exists()
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert "launchctl bootout gui/501/nz.diatche.hermes-gateway" in lines
    assert "kill -TERM 7711" in lines
    assert "port 9119 is free" in result.stdout


def test_force_stop_uses_same_port_authoritative_path(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, "--force-stop")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "kill -TERM 7711" in calls.read_text(encoding="utf-8")


def test_restart_bootstraps_only_after_port_is_free_and_runs_health(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, "--foreground")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    kill_index = lines.index("kill -TERM 7711")
    bootstrap_index = lines.index(
        f"launchctl bootstrap gui/501 {tmp_path / 'wrapper.plist'}"
    )
    health_index = next(i for i, line in enumerate(lines) if line.startswith("health "))
    assert kill_index < bootstrap_index < health_index
    assert any("--check-only --no-state --json" in line for line in lines)


def test_restart_is_blocked_by_active_maintenance(tmp_path: Path) -> None:
    env, calls, _ = _environment(tmp_path)
    Path(env["HERMES_MAINTENANCE_ACTIVE"]).write_text("{}\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--foreground"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "maintenance owns gateway restart" in result.stderr
    assert not calls.exists()


def test_status_requires_loaded_job_and_listening_port(tmp_path: Path) -> None:
    env, _, listener = _environment(tmp_path)
    loaded = tmp_path / "loaded"
    loaded.write_text("yes\n", encoding="utf-8")

    healthy = subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    listener.unlink()
    unhealthy = subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert "port 9119 is listening" in healthy.stdout
    assert unhealthy.returncode == 1
    assert "port 9119 is not listening" in unhealthy.stdout


def test_update_quiescence_only_requires_unloaded_job_and_free_port(tmp_path: Path) -> None:
    env, _, listener = _environment(tmp_path)
    listener.unlink()

    result = subprocess.run(
        ["bash", str(SCRIPT), "--assert-update-quiescence"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "wrapper unloaded and port 9119 is free" in result.stdout
