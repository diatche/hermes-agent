"""Behavior tests for the Python Hermes wrapper deployment orchestrator."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-hermes-wrapper.py"


def _module():
    spec = importlib.util.spec_from_file_location("deploy_hermes_wrapper", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _terminal_env(**updates: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_SESSION_")
        and key not in {"_HERMES_GATEWAY", "HERMES_UI_SESSION_ID"}
    }
    environment.update(updates)
    return environment


def test_agent_foreground_run_refuses_before_touching_launchd(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    launchctl = _executable(
        tmp_path / "launchctl",
        "#!/bin/sh\n" f"printf called >> {calls}\n" "exit 0\n",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--launchctl", str(launchctl)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "_HERMES_GATEWAY": "1"},
    )

    assert result.returncode == 2
    assert "circular wait" in result.stderr
    assert "--detach" in result.stderr
    assert not calls.exists()


def test_detach_starts_a_new_session_with_delayed_worker(
    monkeypatch, tmp_path: Path
) -> None:
    module = _module()
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 43210

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    result = module.main(
        ["--detach", "--detach-delay", "12", "--detach-log", str(tmp_path / "log")]
    )

    assert result == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert "--detached-worker" in command
    assert command[command.index("--detach-delay") + 1] == "12"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_effective_launchd_timeout_mismatch_is_a_warning_after_successful_restart(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls"
    controller = _executable(
        tmp_path / "controller",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$1\" >> {calls}\n"
        "exit 0\n",
    )
    launchctl = _executable(
        tmp_path / "launchctl",
        "#!/bin/sh\n"
        f"count_file={tmp_path / 'launchctl-count'}\n"
        "count=$(cat \"$count_file\" 2>/dev/null || printf 0)\n"
        "count=$((count + 1)); printf '%s' \"$count\" > \"$count_file\"\n"
        "pid=11111; [ \"$count\" -gt 1 ] && pid=12345\n"
        "printf '    state = running\\n    exit timeout = 60\\n    pid = %s\\n' \"$pid\"\n",
    )
    codesign_calls = tmp_path / "codesign-calls"
    codesign = _executable(
        tmp_path / "codesign",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {codesign_calls}\n"
        "exit 0\n",
    )
    state = tmp_path / "timeouts.json"
    state.write_text(
        json.dumps({"pid": 12345, "wrapper_grace": 20, "controller_wait": 30}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--controller",
            str(controller),
            "--launchctl",
            str(launchctl),
            "--timeout-state",
            str(state),
            "--expected-exit-timeout",
            "3700",
            "--codesign",
            str(codesign),
            "--app",
            str(tmp_path / "HermesGateway.app"),
            "--skip-health",
            "--telegram-chat-id",
            "",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_terminal_env(PYTHONDONTWRITEBYTECODE="1"),
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == ["--foreground", "--status"]
    assert "--verify --deep --strict" in codesign_calls.read_text(encoding="utf-8")
    assert "WARNING" in result.stderr
    assert "effective launchd ExitTimeOut is 60; configured checkpoint expected 3700" in result.stderr
    assert "Deployment verification completed with 1 warning" in result.stdout


def test_telegram_progress_uses_one_message_and_notification_failure_is_nonfatal(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, list[str]]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            requests.append((self.path, form))
            if self.path.endswith("/sendMessage"):
                payload = {"ok": True, "result": {"message_id": 777}}
                status = 200
            elif len(requests) == 3:
                payload = {"ok": False, "description": "temporary edit failure"}
                status = 500
            else:
                payload = {"ok": True, "result": {"message_id": 777}}
                status = 200
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    controller = _executable(tmp_path / "controller", "#!/bin/sh\nexit 0\n")
    launchctl = _executable(
        tmp_path / "launchctl",
        "#!/bin/sh\n"
        f"count_file={tmp_path / 'launchctl-count'}\n"
        "count=$(cat \"$count_file\" 2>/dev/null || printf 0)\n"
        "count=$((count + 1)); printf '%s' \"$count\" > \"$count_file\"\n"
        "pid=11111; [ \"$count\" -gt 1 ] && pid=12345\n"
        "printf '    state = running\\n    exit timeout = 60\\n    pid = %s\\n' \"$pid\"\n",
    )
    state = tmp_path / "timeouts.json"
    state.write_text(
        json.dumps({"pid": 12345, "wrapper_grace": 20, "controller_wait": 30}),
        encoding="utf-8",
    )
    telegram_state = tmp_path / "telegram-progress.json"
    telegram_env = tmp_path / ".env"
    telegram_env.write_text("TELEGRAM_BOT_TOKEN=test-token\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--controller",
                str(controller),
                "--launchctl",
                str(launchctl),
                "--timeout-state",
                str(state),
                "--expected-exit-timeout",
                "3700",
                "--skip-signature",
                "--skip-health",
                "--telegram-state",
                str(telegram_state),
                "--telegram-api-base",
                f"http://127.0.0.1:{server.server_port}",
                "--telegram-env-file",
                str(telegram_env),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=_terminal_env(
                PYTHONDONTWRITEBYTECODE="1", TELEGRAM_BOT_TOKEN=""
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.returncode == 0, result.stderr
    assert [path.rsplit("/", 1)[-1] for path, _form in requests].count("sendMessage") == 1
    assert [path.rsplit("/", 1)[-1] for path, _form in requests].count("editMessageText") >= 2
    assert all(form.get("message_id", ["777"])[0] == "777" for _path, form in requests[1:])
    assert "WARNING: Telegram progress update failed" in result.stderr
    saved = json.loads(telegram_state.read_text(encoding="utf-8"))
    assert saved == {"chat_id": "297138560", "message_id": 777}
