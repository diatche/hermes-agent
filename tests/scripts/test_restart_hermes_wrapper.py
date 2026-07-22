"""Behavior tests for wrapper process-topology checks."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "restart-hermes-wrapper.sh"


def _command(path: Path, name: str, content: str) -> None:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o755)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    app = tmp_path / "HermesGateway.app"
    repo = tmp_path / "hermes-agent"
    timeout_state = tmp_path / "wrapper-timeouts.json"
    timeout_state.write_text(
        json.dumps({"pid": 99111, "wrapper_grace": 65, "controller_wait": 75}),
        encoding="utf-8",
    )
    _command(
        repo / "venv" / "bin",
        "hermes",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == config && $2 == get ]]; then printf '0\\n'; exit 0; fi\n"
        "exit 1\n",
    )
    _command(
        repo / "scripts",
        "hermes-wrapper-timeout-budget.py",
        "#!/usr/bin/env bash\nprintf '75\\n'\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HOME": str(tmp_path),
            "HERMES_WRAPPER_DOMAIN": "gui/501",
            "HERMES_WRAPPER_APP": str(app),
            "HERMES_WRAPPER_PLIST": str(tmp_path / "wrapper.plist"),
            "HERMES_WRAPPER_ENTRYPOINT": str(tmp_path / "entrypoint"),
            "HERMES_DASHBOARD_PORT": "9119",
            "HERMES_WRAPPER_PYTHON": sys.executable,
            "HERMES_WRAPPER_TIMEOUT_STATE": str(timeout_state),
            "HERMES_WRAPPER_TIMEOUT_BUDGET_SCRIPT": str(
                repo / "scripts" / "hermes-wrapper-timeout-budget.py"
            ),
        }
    )
    return env, fake_bin


def test_stop_waits_only_for_captured_wrapper_tree(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    loaded = tmp_path / "loaded"
    loaded.write_text("yes\n", encoding="utf-8")
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        f"state='{loaded}'\n"
        "if [[ $1 == print && $2 == gui/501/nz.diatche.hermes-gateway ]]; then\n"
        "  [[ -f $state ]] || exit 1\n"
        "  echo '    pid = 99111'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ $1 == bootout ]]; then rm -f $state; exit 0; fi\n"
        "exit 1\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        "if [[ $* == *'-axo pid=,ppid=,command='* ]]; then\n"
        "  echo '99222 99111 /fake/hermes gateway run --replace'\n"
        "  echo '99888 1 /other/profile/hermes gateway run --replace'\n"
        "fi\n"
        "exit 0\n",
    )
    _command(fake_bin, "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--stop"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Stopped and quiescent" in result.stdout


def test_stop_wait_budget_comes_from_active_wrapper_state(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    Path(env["HERMES_WRAPPER_TIMEOUT_STATE"]).write_text(
        json.dumps({"pid": 99111, "wrapper_grace": 1, "controller_wait": 1}),
        encoding="utf-8",
    )
    loaded = tmp_path / "loaded"
    loaded.write_text("yes\n", encoding="utf-8")
    sleeps = tmp_path / "sleeps"
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print ]]; then echo '    pid = 99111'; exit 0; fi\n"
        "if [[ $1 == bootout ]]; then exit 0; fi\n"
        "exit 1\n",
    )
    _command(fake_bin, "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "ps", "#!/usr/bin/env bash\nexit 0\n")
    _command(
        fake_bin,
        "sleep",
        "#!/usr/bin/env bash\n"
        f"printf 'poll\\n' >> {sleeps}\n"
        "/bin/sleep \"$1\"\n",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--stop"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert len(sleeps.read_text(encoding="utf-8").splitlines()) >= 1


def test_stop_rejects_timeout_state_for_a_different_wrapper_pid(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    Path(env["HERMES_WRAPPER_TIMEOUT_STATE"]).write_text(
        json.dumps({"pid": 12345, "wrapper_grace": 65, "controller_wait": 75}),
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        "if [[ $1 == print ]]; then echo '    pid = 99111'; exit 0; fi\n"
        "exit 0\n",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--stop"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "timeout state is invalid" in result.stderr
    assert "bootout" not in calls.read_text(encoding="utf-8")


def test_stop_bounds_a_hanging_launchctl_bootout(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    Path(env["HERMES_WRAPPER_TIMEOUT_STATE"]).write_text(
        json.dumps({"pid": 99111, "wrapper_grace": 1, "controller_wait": 1}),
        encoding="utf-8",
    )
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print ]]; then echo '    pid = 99111'; exit 0; fi\n"
        "if [[ $1 == bootout ]]; then trap '' TERM; while :; do /bin/sleep 0.1; done; fi\n"
        "exit 1\n",
    )
    _command(fake_bin, "ps", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--stop"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert "bootout exceeded coordinated stop deadline" in result.stderr


def test_status_rejects_listener_not_owned_by_wrapper_tree(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print-disabled ]]; then echo '\"ai.hermes.gateway\" => disabled'; exit 0; fi\n"
        "if [[ $1 == print && $2 == gui/501/nz.diatche.hermes-gateway ]]; then\n"
        "  echo '    pid = 99111'; exit 0\n"
        "fi\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        "if [[ $* == *'-p 99111 -o command='* ]]; then echo \"$HERMES_WRAPPER_APP/Contents/MacOS/HermesGateway\"; exit 0; fi\n"
        "if [[ $* == *'-p 99999 -o ppid='* ]]; then echo '1'; exit 0; fi\n"
        "if [[ $* == *'-axo pid=,ppid=,command='* ]]; then echo '99222 99111 hermes gateway run --replace'; exit 0; fi\n"
        "if [[ $* == *'-axo pid,ppid,stat,command'* ]]; then exit 0; fi\n"
        "exit 1\n",
    )
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\n[[ $* == *'-t'* ]] && echo 99999\nexit 0\n")
    _command(fake_bin, "pgrep", "#!/usr/bin/env bash\nexit 1\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "wrapper-owned gateway/listener is not ready" in result.stdout


@pytest.mark.parametrize(
    ("command", "should_block"),
    [
        ("/usr/local/bin/hermes --profile crmwebhook gateway run --replace", True),
        ("/usr/local/bin/hermes --profile crmwebhook gateway", True),
        ("/usr/local/bin/hermes-gateway --profile crmwebhook", True),
        ("python /repo/gateway/run.py --profile crmwebhook", True),
        ("python -m hermes_cli.main --profile crmwebhook dashboard", True),
        ("python /repo/hermes_cli/main.py --profile crmwebhook dashboard", True),
        ("python /repo/hermes_cli/main.py serve --profile crmwebhook", True),
        ("/usr/local/bin/hermes --profile crmwebhook dashboard --port 9000", True),
        ("/usr/local/bin/other gateway run --replace", False),
        ("python chat.py say hermes dashboard", False),
        ("python chat.py say hermes_cli.main dashboard", False),
        ("python chat.py say /repo/hermes_cli/main.py serve", False),
        ("python chat.py say web_server.start_server", False),
    ],
)
def test_update_quiescence_detects_hermes_runtime_command_shapes(
    tmp_path: Path, command: str, should_block: bool
) -> None:
    env, fake_bin = _environment(tmp_path)
    env["HERMES_REPO"] = str(SCRIPT.parents[1])
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print-disabled ]]; then echo '\"ai.hermes.gateway\" => disabled'; exit 0; fi\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        f"echo '4242 1 {command}'\n",
    )
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "sleep", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--assert-update-quiescence"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode != 0) is should_block, result.stdout + result.stderr
    if should_block:
        assert "update quiescence could not be established" in result.stderr
    else:
        assert "Update quiescence verified" in result.stdout


def test_update_quiescence_ignores_matcher_ancestry(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    env["HERMES_REPO"] = str(SCRIPT.parents[1])
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        "echo \"$PPID 1 /usr/local/bin/hermes dashboard\"\n",
    )
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "sleep", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--assert-update-quiescence"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Update quiescence verified" in result.stdout


def test_update_quiescence_fails_closed_on_malformed_process_row(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    env["HERMES_REPO"] = str(SCRIPT.parents[1])
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(fake_bin, "ps", "#!/usr/bin/env bash\necho 'not-a-valid-process-row'\n")
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "sleep", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--assert-update-quiescence"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "update quiescence could not be established" in result.stderr
    assert "matcher-unavailable" in result.stderr


def test_update_quiescence_rejects_dashboard_that_starts_after_clean_samples(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    ps_calls = tmp_path / "ps-calls"
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print-disabled ]]; then echo '\"ai.hermes.gateway\" => disabled'; exit 0; fi\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        f"count=$(cat '{ps_calls}' 2>/dev/null || echo 0)\n"
        f"echo $((count + 1)) > '{ps_calls}'\n"
        "if (( count >= 9 )); then echo '4343 1 /usr/local/bin/hermes dashboard --port 9119'; fi\n",
    )
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\nexit 1\n")
    _command(fake_bin, "sleep", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--assert-update-quiescence"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "update quiescence could not be established" in result.stderr


def test_status_allows_named_profile_gateway(tmp_path: Path) -> None:
    env, fake_bin = _environment(tmp_path)
    env["HERMES_REPO"] = "/repo"
    extra_gateway = tmp_path / "extra-gateway"
    env["EXTRA_GATEWAY"] = str(extra_gateway)
    _command(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ $1 == print-disabled ]]; then echo '\"ai.hermes.gateway\" => disabled'; exit 0; fi\n"
        "if [[ $1 == print && $2 == gui/501/nz.diatche.hermes-gateway ]]; then\n"
        "  echo '    pid = 99111'; exit 0\n"
        "fi\n"
        "if [[ $1 == print ]]; then exit 1; fi\n"
        "exit 0\n",
    )
    _command(
        fake_bin,
        "ps",
        "#!/usr/bin/env bash\n"
        "if [[ $* == *'-p 99111 -o command='* ]]; then echo \"$HERMES_WRAPPER_APP/Contents/MacOS/HermesGateway\"; exit 0; fi\n"
        "if [[ $* == *'-p 99333 -o command='* ]]; then echo '/repo/venv/bin/hermes gateway run --replace'; exit 0; fi\n"
        "if [[ $* == *'-axo pid=,ppid=,command='* ]]; then\n"
        "  echo '99333 99111 /repo/venv/bin/hermes gateway run --replace'\n"
        "  echo '99444 99111 /repo/venv/bin/hermes dashboard --port 9119'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ $* == *'-axo pid=,command='* ]]; then\n"
        "  echo \"99111 $HERMES_WRAPPER_APP/Contents/MacOS/HermesGateway\"\n"
        "  echo '99333 /repo/venv/bin/hermes gateway run --replace'\n"
        "  echo '99444 /repo/venv/bin/hermes dashboard --port 9119'\n"
        "  [[ -e $EXTRA_GATEWAY ]] && echo '99555 /other/venv/bin/hermes --profile crmwebhook gateway run --replace'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    _command(fake_bin, "lsof", "#!/usr/bin/env bash\n[[ $* == *'-t'* ]] && echo 99444\nexit 0\n")

    healthy = subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    extra_gateway.touch()
    result = subprocess.run(
        ["bash", str(SCRIPT), "--status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: Hermes gateway is wrapper-owned and :9119 is listening" in result.stdout


def test_detached_restart_is_blocked_by_active_maintenance(tmp_path: Path) -> None:
    env, _ = _environment(tmp_path)
    marker = tmp_path / ".hermes" / "local" / "update" / "active.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"run_id":"test"}\n', encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), "--detach"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "maintenance owns gateway restart" in result.stderr
