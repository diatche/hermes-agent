"""Behavior tests for the forced stop → merge → restart maintenance path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes-maintenance-update.py"


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=check
    )
    return result.stdout.strip()


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.name=Maintenance Test",
        "-c", "user.email=maintenance@example.test",
        "commit", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path, *, conflict: bool = False) -> tuple[Path, str]:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _write(seed, "base.txt", "base\n")
    _commit(seed, "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(repo))
    _git(repo, "switch", "-c", "diatche")
    _write(repo, "base.txt" if conflict else "local.txt", "local\n")
    _commit(repo, "local")
    _write(seed, "base.txt" if conflict else "upstream.txt", "upstream\n")
    upstream_sha = _commit(seed, "upstream")
    _git(seed, "push", "origin", "main")
    return repo, upstream_sha


def _fake_runtime(
    tmp_path: Path,
    *,
    fail_health_call: int | None = None,
    fail_stop: bool = False,
):
    calls = tmp_path / "wrapper-calls.log"
    health_count = tmp_path / "health-count"
    stop_clause = '[ "$1" != --force-stop ]\n' if fail_stop else "exit 0\n"
    wrapper = _write(
        tmp_path,
        "wrapperctl",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls!s}\n"
        f"{stop_clause}",
    )
    fail_test = (
        f"if n == {fail_health_call}:\n    raise SystemExit(1)\n"
        if fail_health_call is not None
        else ""
    )
    health = _write(
        tmp_path,
        "health.py",
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        f"p = Path({str(health_count)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        f"{fail_test}"
        "print(json.dumps({'healthy': True}))\n",
    )
    wrapper.chmod(0o755)
    health.chmod(0o755)
    return wrapper, health, calls


def _run(
    repo: Path,
    tmp_path: Path,
    *,
    fail_health_call: int | None = None,
    fail_stop: bool = False,
    env: dict[str, str] | None = None,
):
    wrapper, health, calls = _fake_runtime(
        tmp_path, fail_health_call=fail_health_call, fail_stop=fail_stop
    )
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run",
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    return result, calls, state_dir


def test_success_force_stops_merges_and_restarts(tmp_path: Path) -> None:
    repo, upstream_sha = _make_repo(tmp_path)

    result, calls, state_dir = _run(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "branch", "--show-current") == "diatche"
    assert _git(repo, "rev-parse", "main") == upstream_sha
    assert _git(repo, "merge-base", "--is-ancestor", upstream_sha, "diatche") == ""
    assert _git(repo, "status", "--porcelain") == ""
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--assert-update-quiescence",
        "--foreground",
        "--status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "complete"
    assert state["upstream_sha"] == upstream_sha
    assert "official updater" not in result.stdout.lower()


def test_conflict_fails_before_forced_stop(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path, conflict=True)
    old_main = _git(repo, "rev-parse", "main")
    old_diatche = _git(repo, "rev-parse", "diatche")

    result, calls, _ = _run(repo, tmp_path)

    assert result.returncode == 1
    assert "conflict" in result.stderr.lower()
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert calls.read_text(encoding="utf-8").splitlines() == ["--status"]


def test_dirty_checkout_fails_before_wrapper_call(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    _write(repo, "dirty.txt", "dirty\n")

    result, calls, _ = _run(repo, tmp_path)

    assert result.returncode == 1
    assert "dirty" in result.stderr.lower()
    assert not calls.exists()


def test_health_failure_rolls_back_refs_and_restarts_old_runtime(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    old_main = _git(repo, "rev-parse", "main")
    old_diatche = _git(repo, "rev-parse", "diatche")

    result, calls, state_dir = _run(repo, tmp_path, fail_health_call=1)

    assert result.returncode == 1
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert _git(repo, "rev-parse", "HEAD") == old_diatche
    assert _git(repo, "status", "--porcelain") == ""
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--assert-update-quiescence",
        "--foreground",
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "failed"
    assert state["recovered"] is True


def test_stop_failure_keeps_refs_and_restarts_old_runtime(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    old_main = _git(repo, "rev-parse", "main")
    old_diatche = _git(repo, "rev-parse", "diatche")

    result, calls, _ = _run(repo, tmp_path, fail_stop=True)

    assert result.returncode == 1
    assert "wrapper --stop failed" in result.stderr
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
    ]


def test_never_invokes_updater_package_or_build_commands(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    forbidden = tmp_path / "forbidden-bin"
    called = tmp_path / "forbidden-called"
    forbidden.mkdir()
    for name in ("hermes", "npm", "pip", "pip3", "uv", "yarn", "pnpm", "make"):
        command = forbidden / name
        command.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{name}' >> {called!s}\nexit 97\n",
            encoding="utf-8",
        )
        command.chmod(0o755)
    env = {**os.environ, "PATH": f"{forbidden}:{os.environ['PATH']}"}

    result, _, _ = _run(repo, tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert not called.exists()


def test_install_writes_command_and_one_shot_job_without_running_update(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    installed = tmp_path / "bin" / "hermes-maintenance-update"
    plist = tmp_path / "maintenance.plist"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--install",
            "--no-load",
            "--repo", str(repo),
            "--state-dir", str(tmp_path / "state"),
            "--installed-script", str(installed),
            "--maintenance-plist", str(plist),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert installed.read_bytes() == SCRIPT.read_bytes()
    assert installed.stat().st_mode & 0o111
    body = plist.read_text(encoding="utf-8")
    assert "nz.diatche.hermes-maintenance-update" in body
    assert "<false/>" in body
    assert "--run" in body


def test_detach_queues_installed_launchagent(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl-calls"
    launchctl = _write(
        fake_bin,
        "launchctl",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls!s}\n"
        "exit 0\n",
    )
    launchctl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    state_dir = tmp_path / "state"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--detach",
            "--repo", str(repo),
            "--state-dir", str(state_dir),
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    domain = f"gui/{os.getuid()}/nz.diatche.hermes-maintenance-update"
    assert calls.read_text().splitlines() == [
        f"print {domain}",
        f"kickstart {domain}",
    ]
    assert json.loads((state_dir / "state.json").read_text())["phase"] == "queued"
