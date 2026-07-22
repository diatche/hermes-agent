#!/usr/bin/env python3
"""Restart and verify Pavel's signed Hermes gateway wrapper.

Usage from Terminal: ``python3 scripts/deploy-hermes-wrapper.py``.
Usage from a Hermes agent session: add ``--detach``. Never run the foreground
mode from the gateway turn it is stopping: the turn can wait for the restart
while graceful shutdown waits for the turn, creating a circular wait.
The script invokes the established wrapper controller, verifies runtime state,
and reports informational checkpoint mismatches as warnings. It does not edit
configuration, install files, or replace the signed app bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import error, parse, request

from dotenv import dotenv_values


DEFAULT_HOME = Path.home() / ".hermes"
DEFAULT_LABEL = "nz.diatche.hermes-gateway"
DEFAULT_TELEGRAM_CHAT_ID = "297138560"
DEFAULT_DETACH_LOG = DEFAULT_HOME / "logs/hermes-wrapper-deployment.log"


class TelegramProgress:
    """Best-effort single-message progress reporting through Telegram Bot API."""

    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        state_path: Path,
        api_base: str,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.state_path = state_path
        self.api_base = api_base.rstrip("/")
        self.message_id: int | None = None

    def _call(self, method: str, fields: dict[str, str]) -> dict[str, object]:
        body = parse.urlencode(fields).encode("utf-8")
        endpoint = f"{self.api_base}/bot{self.token}/{method}"
        try:
            with request.urlopen(endpoint, data=body, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Telegram Bot API request failed") from exc
        if not payload.get("ok"):
            raise RuntimeError("Telegram Bot API rejected progress update")
        return payload

    def _save_state(self) -> None:
        assert self.message_id is not None
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=f".{self.state_path.name}."
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"chat_id": self.chat_id, "message_id": self.message_id}, handle
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def update(self, text: str) -> None:
        if self.message_id is None:
            payload = self._call(
                "sendMessage", {"chat_id": self.chat_id, "text": text}
            )
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(
                result.get("message_id"), int
            ):
                raise RuntimeError("Telegram response omitted message_id")
            self.message_id = result["message_id"]
            self._save_state()
            return
        self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": str(self.message_id),
                "text": text,
            },
        )


def _progress(message: str) -> None:
    print(f"==> {message}", file=sys.stderr, flush=True)


def _run(command: list[str], *, name: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{name} failed: {detail}")
    return result


def _parse_launchd(output: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for key, pattern in {
        "pid": r"^\s*pid = (\d+)\s*$",
        "exit_timeout": r"^\s*exit timeout = (\d+)\s*$",
    }.items():
        match = re.search(pattern, output, re.MULTILINE)
        if not match:
            raise RuntimeError(f"launchd status omitted {key.replace('_', ' ')}")
        values[key] = int(match.group(1))
    return values


def _read_timeout_state(path: Path) -> dict[str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        state = {
            "pid": int(value["pid"]),
            "wrapper_grace": int(value["wrapper_grace"]),
            "controller_wait": int(value["controller_wait"]),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid wrapper timeout state: {exc}") from exc
    if any(number < 0 for number in state.values()):
        raise RuntimeError("invalid wrapper timeout state: negative value")
    return state


def _in_agent_session() -> bool:
    return bool(os.environ.get("_HERMES_GATEWAY") or os.environ.get("HERMES_SESSION_ID"))


def _schedule_detached(args: argparse.Namespace, argv: list[str]) -> int:
    args.detach_log.parent.mkdir(parents=True, exist_ok=True)
    worker_argv = [argument for argument in argv if argument != "--detach"]
    worker_argv.append("--detached-worker")
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("HERMES_SESSION_") or key in {
            "_HERMES_GATEWAY",
            "HERMES_UI_SESSION_ID",
        }:
            environment.pop(key, None)
    with args.detach_log.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *worker_argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=environment,
        )
    print(
        f"Scheduled detached Hermes gateway restart as PID {process.pid}; "
        f"starting after {args.detach_delay:g}s. Log: {args.detach_log}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Agent sessions must use --detach to avoid a circular wait. "
            "Foreground mode is intended only for an independent Terminal."
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="start an independent delayed worker and return before shutdown",
    )
    parser.add_argument(
        "--detach-delay",
        type=float,
        default=10.0,
        help="seconds the detached worker waits for the initiating turn to finish",
    )
    parser.add_argument("--detach-log", type=Path, default=DEFAULT_DETACH_LOG)
    parser.add_argument("--detached-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--controller",
        default=str(DEFAULT_HOME / "local/bin/hermes-gateway-wrapperctl"),
    )
    parser.add_argument("--launchctl", default="/bin/launchctl")
    parser.add_argument("--codesign", default="/usr/bin/codesign")
    parser.add_argument("--app", default="/Applications/HermesGateway.app")
    parser.add_argument(
        "--timeout-state",
        type=Path,
        default=DEFAULT_HOME / "local/run/hermes-gateway-wrapper-timeouts.json",
    )
    parser.add_argument("--expected-exit-timeout", type=int, default=3700)
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-signature", action="store_true")
    parser.add_argument("--telegram-chat-id", default=DEFAULT_TELEGRAM_CHAT_ID)
    parser.add_argument(
        "--telegram-state",
        type=Path,
        default=DEFAULT_HOME / "local/run/hermes-wrapper-telegram-progress.json",
    )
    parser.add_argument(
        "--telegram-api-base", default="https://api.telegram.org"
    )
    parser.add_argument(
        "--telegram-env-file", type=Path, default=DEFAULT_HOME / ".env"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    if args.detach:
        return _schedule_detached(args, raw_argv)
    if _in_agent_session() and not args.detached_worker:
        print(
            "ERROR: refusing foreground restart from a Hermes agent session: "
            "it can create a circular wait between this turn and gateway drain. "
            "Run this command with --detach, or run foreground mode from an "
            "independent Terminal.",
            file=sys.stderr,
        )
        return 2
    if args.detached_worker:
        time.sleep(args.detach_delay)
    warnings: list[str] = []
    domain = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"
    notifier: TelegramProgress | None = None
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token and args.telegram_env_file.is_file():
        loaded_token = dotenv_values(args.telegram_env_file).get("TELEGRAM_BOT_TOKEN")
        if isinstance(loaded_token, str):
            token = loaded_token
    if args.telegram_chat_id and token:
        notifier = TelegramProgress(
            token=token,
            chat_id=args.telegram_chat_id,
            state_path=args.telegram_state,
            api_base=args.telegram_api_base,
        )
    elif args.telegram_chat_id:
        warnings.append("Telegram progress disabled: TELEGRAM_BOT_TOKEN is unavailable")

    def notify(text: str) -> None:
        if notifier is None:
            return
        try:
            notifier.update(text)
        except RuntimeError as exc:
            warning = f"Telegram progress update failed: {exc}"
            if warning not in warnings:
                warnings.append(warning)
            print(f"WARNING: {warning}", file=sys.stderr)

    try:
        old_launchd_result = _run(
            [args.launchctl, "print", f"{domain}/{DEFAULT_LABEL}"],
            name="initial launchd status",
        )
        old_launchd = _parse_launchd(old_launchd_result.stdout)
        _progress("Restarting the custom Hermes gateway wrapper")
        notify("Hermes gateway restart\n\n⏳ Restarting wrapper")
        _run([args.controller, "--foreground"], name="wrapper restart")

        _progress("Checking wrapper-owned process and listener topology")
        notify("Hermes gateway restart\n\n✅ Wrapper restarted\n⏳ Verifying topology")
        _run([args.controller, "--status"], name="wrapper status")

        launchd_result = _run(
            [args.launchctl, "print", f"{domain}/{DEFAULT_LABEL}"],
            name="launchd status",
        )
        launchd = _parse_launchd(launchd_result.stdout)
        if launchd["pid"] == old_launchd["pid"]:
            raise RuntimeError("wrapper restart did not replace the launchd process")
        state = _read_timeout_state(args.timeout_state)
        if state["pid"] != launchd["pid"]:
            raise RuntimeError(
                "wrapper timeout state PID does not match the running launchd job"
            )

        if launchd["exit_timeout"] != args.expected_exit_timeout:
            warnings.append(
                "effective launchd ExitTimeOut is "
                f"{launchd['exit_timeout']}; configured checkpoint expected "
                f"{args.expected_exit_timeout}"
            )

        if not args.skip_signature:
            _progress("Verifying the signed HermesGateway app")
            _run(
                [args.codesign, "--verify", "--deep", "--strict", args.app],
                name="HermesGateway signature verification",
            )

        if not args.skip_health:
            _progress("Health checking the restarted Hermes gateway")
            notify(
                "Hermes gateway restart\n\n✅ Wrapper restarted\n"
                "✅ Topology verified\n⏳ Checking health"
            )
            health = DEFAULT_HOME / "local/health/hermes_core_health.py"
            python = DEFAULT_HOME / "hermes-agent/venv/bin/python"
            _run(
                [str(python), str(health), "--check-only", "--no-state", "--json"],
                name="Hermes core health check",
            )
    except RuntimeError as exc:
        notify(f"Hermes gateway restart\n\n❌ Failed: {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    notify(
        "Hermes gateway restart\n\n✅ Restarted and verified"
        + (f"\n⚠️ Completed with {len(warnings)} warning(s)" if warnings else "")
    )
    for warning in warnings:
        if not warning.startswith("Telegram progress update failed"):
            print(f"WARNING: {warning}", file=sys.stderr)
    suffix = "warning" if len(warnings) == 1 else "warnings"
    if warnings:
        print(f"Deployment verification completed with {len(warnings)} {suffix}")
    else:
        print("Deployment verification completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
