"""Behavior tests for the forced stop → merge → restart maintenance path."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes-maintenance-update.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("hermes_maintenance_update", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    python = _write(repo, "venv/bin/python", "#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    updater_calls = tmp_path / "official-updater-calls.log"
    updater = _write(
        repo,
        "venv/bin/hermes",
        "#!/bin/sh\nset -eu\n"
        f"printf '%s\\n' \"$*\" >> {updater_calls!s}\n"
        "git fetch origin main\n"
        "git update-ref refs/heads/main refs/remotes/origin/main\n"
        "git switch main\n"
        "git reset --hard refs/remotes/origin/main\n",
    )
    updater.chmod(0o755)
    _write(repo, ".git/info/exclude", "venv/\n")
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
    health.chmod(0o644)
    return wrapper, health, calls


def _fake_official_updater(tmp_path: Path) -> tuple[Path, Path]:
    """Emulate only the updater's documented Git result inside the fixture repo."""
    calls = tmp_path / "official-updater-calls.log"
    updater = _write(
        tmp_path,
        "fake-hermes",
        "#!/bin/sh\nset -eu\n"
        f"printf '%s\\n' \"$*\" >> {calls!s}\n"
        "git fetch origin main\n"
        "git update-ref refs/heads/main refs/remotes/origin/main\n"
        "git switch main\n"
        "git reset --hard refs/remotes/origin/main\n",
    )
    updater.chmod(0o755)
    return updater, calls


def _emulate_official_update(repo: Path) -> None:
    _git(repo, "fetch", "origin", "main")
    _git(repo, "update-ref", "refs/heads/main", "refs/remotes/origin/main")
    _git(repo, "switch", "main")
    _git(repo, "reset", "--hard", "refs/remotes/origin/main")


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
    updater, _ = _fake_official_updater(tmp_path)
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
            "--updater-executable", str(updater),
        ],
        text=True,
        capture_output=True,
        env=env,
    )
    return result, calls, state_dir


def _args(repo: Path, state_dir: Path, wrapper: Path, health: Path) -> Namespace:
    updater, _ = _fake_official_updater(repo.parent)
    return Namespace(
        repo=repo,
        state_dir=state_dir,
        wrapperctl=wrapper,
        health_script=health,
        updater_executable=updater,
        remote="origin",
        upstream_branch="main",
        wrapper_timeout=30,
        health_timeout=30,
        updater_timeout=30,
    )


def test_public_help_exposes_pre_post_check_and_help() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "--pre" in result.stdout
    assert "--post" in result.stdout
    assert "--check" in result.stdout
    assert "--run" not in result.stdout


def test_pre_stops_runtime_prints_handoff_and_never_runs_updater(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pre",
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
    ]
    assert not (tmp_path / "official-updater-calls.log").exists()
    assert f"cd {repo}" in result.stdout
    assert (
        f"{repo}/venv/bin/hermes update --branch main --backup --yes "
        "--no-gateway-restart"
    ) in result.stdout
    assert f"{SCRIPT} --post" in result.stdout
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "awaiting-official-update"


def test_post_finishes_prepared_handoff_after_direct_official_update(
    tmp_path: Path,
) -> None:
    repo, upstream_sha = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    common = [
        "--repo", str(repo),
        "--state-dir", str(state_dir),
        "--wrapperctl", str(wrapper),
        "--health-script", str(health),
    ]
    prepared = subprocess.run(
        [sys.executable, str(SCRIPT), "--pre", *common],
        text=True,
        capture_output=True,
    )
    assert prepared.returncode == 0, prepared.stderr

    _emulate_official_update(repo)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--post", *common],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert _git(repo, "branch", "--show-current") == "diatche"
    assert _git(repo, "rev-parse", "main") == upstream_sha
    assert _git(repo, "merge-base", "--is-ancestor", upstream_sha, "diatche") == ""
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "complete"


def test_check_is_read_only_and_never_calls_wrapper(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    refs_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--check",
            "--repo", str(repo),
            "--state-dir", str(tmp_path / "state"),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "mergeable" in result.stdout
    assert _git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert not calls.exists()


def test_single_interrupted_journal_is_recovered_before_update(tmp_path: Path) -> None:
    repo, upstream_sha = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    run_id = "interrupted"
    main_old = _git(repo, "rev-parse", "main")
    integration_old = _git(repo, "rev-parse", "diatche")
    common_git_dir = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not common_git_dir.is_absolute():
        common_git_dir = repo / common_git_dir
    payload = {
        "version": 2,
        "run_id": run_id,
        "repo": str(repo.resolve()),
        "common_git_dir": str(common_git_dir.resolve()),
        "integration_branch": "diatche",
        "main_ref": "refs/heads/main",
        "integration_ref": "refs/heads/diatche",
        "main_old": main_old,
        "integration_old": integration_old,
        "main_new": upstream_sha,
        "integration_new": integration_old,
        "phase": "stopped",
        "owner_pid": 99_999_999,
    }
    _write(state_dir, f"runs/{run_id}.json", json.dumps(payload))
    _write(state_dir, "state.json", json.dumps(payload))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    recovered = json.loads((state_dir / "runs" / f"{run_id}.json").read_text())
    assert recovered["phase"] == "recovered"
    assert calls.read_text().splitlines()[:2] == ["--status", "--status"]


def test_crash_recovery_stops_candidate_runtime_before_restoring_refs(
    tmp_path: Path,
) -> None:
    repo, upstream_sha = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    run_id = "published-crash"
    main_old = _git(repo, "rev-parse", "main")
    integration_old = _git(repo, "rev-parse", "diatche")
    common_git_dir = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not common_git_dir.is_absolute():
        common_git_dir = repo / common_git_dir
    _git(repo, "fetch", "origin", "main")
    _git(repo, "update-ref", "refs/heads/main", upstream_sha, main_old)
    payload = {
        "version": 2,
        "run_id": run_id,
        "repo": str(repo.resolve()),
        "common_git_dir": str(common_git_dir.resolve()),
        "integration_branch": "diatche",
        "main_ref": "refs/heads/main",
        "integration_ref": "refs/heads/diatche",
        "main_old": main_old,
        "integration_old": integration_old,
        "main_new": upstream_sha,
        "integration_new": integration_old,
        "phase": "published",
        "owner_pid": 99_999_999,
    }
    _write(state_dir, f"runs/{run_id}.json", json.dumps(payload))
    _write(state_dir, "state.json", json.dumps(payload))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines()[0] == "--force-stop"


def test_crash_recovery_preserves_nontransactional_dirty_checkout(
    tmp_path: Path,
) -> None:
    repo, upstream_sha = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    run_id = "stopped-dirty"
    main_old = _git(repo, "rev-parse", "main")
    integration_old = _git(repo, "rev-parse", "diatche")
    common_git_dir = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    payload = {
        "version": 2,
        "run_id": run_id,
        "repo": str(repo.resolve()),
        "common_git_dir": str(common_git_dir.resolve()),
        "integration_branch": "diatche",
        "main_ref": "refs/heads/main",
        "integration_ref": "refs/heads/diatche",
        "main_old": main_old,
        "integration_old": integration_old,
        "main_new": upstream_sha,
        "integration_new": integration_old,
        "phase": "stopped",
        "owner_pid": 99_999_999,
    }
    _write(state_dir, f"runs/{run_id}.json", json.dumps(payload))
    _write(state_dir, "state.json", json.dumps(payload))
    _write(repo, "base.txt", "concurrent dirty content\n")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert (repo / "base.txt").read_text() == "concurrent dirty content\n"
    assert calls.read_text().splitlines() == ["--force-stop"]


def test_completed_current_state_ignores_historical_failed_runs(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    wrapper, health, _ = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    _write(
        state_dir,
        "runs/historical.json",
        json.dumps({"phase": "failed", "owner_pid": 99_999_999}),
    )
    _write(state_dir, "state.json", json.dumps({"phase": "complete"}))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo),
            "--state-dir", str(state_dir),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


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


def test_missing_updater_fails_before_forced_stop(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    missing = tmp_path / "missing-hermes"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pre",
            "--repo", str(repo),
            "--state-dir", str(tmp_path / "state"),
            "--wrapperctl", str(wrapper),
            "--health-script", str(health),
            "--updater-executable", str(missing),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "official Hermes updater is missing" in result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == ["--status"]


def test_dirty_checkout_fails_before_wrapper_call(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    _write(repo, "dirty.txt", "dirty\n")

    result, calls, _ = _run(repo, tmp_path)

    assert result.returncode == 1
    assert "dirty" in result.stderr.lower()
    assert not calls.exists()


def test_checkout_is_rechecked_immediately_before_stop(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo, _ = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    original_build = module._build_candidate

    def build_then_dirty(*args, **kwargs):
        result = original_build(*args, **kwargs)
        _write(repo, "concurrent.txt", "do not delete\n")
        return result

    monkeypatch.setattr(module, "_build_candidate", build_then_dirty)

    result = module._run_pre(_args(repo, state_dir, wrapper, health))

    assert result == 1
    assert calls.read_text().splitlines() == ["--status"]
    assert (repo / "concurrent.txt").read_text() == "do not delete\n"


def test_cas_failure_does_not_reset_concurrent_ref_or_checkout_changes(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo, _ = _make_repo(tmp_path)
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"
    moved_to = ""

    def concurrent_cas_failure(_repo, **kwargs):
        nonlocal moved_to
        moved_to = kwargs["integration_new"]
        _write(repo, "base.txt", "concurrent tracked edit\n")
        _git(repo, "update-ref", "refs/heads/diatche", moved_to)
        raise RuntimeError("ref compare-and-swap transaction failed")

    prepared = module._run_pre(_args(repo, state_dir, wrapper, health))
    assert prepared == 0
    _emulate_official_update(repo)
    monkeypatch.setattr(module, "_publish_integration", concurrent_cas_failure)

    result = module._run_post(_args(repo, state_dir, wrapper, health))

    assert result == 1
    assert _git(repo, "rev-parse", "diatche") == moved_to
    assert (repo / "base.txt").read_text() == "concurrent tracked edit\n"
    assert calls.read_text().splitlines() == ["--status", "--force-stop"]
    state = json.loads((state_dir / "state.json").read_text())
    assert "concurrent" in state["recovery_error"].lower()


def test_health_failure_rolls_back_refs_and_restarts_old_runtime(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    old_main = _git(repo, "rev-parse", "main")
    old_diatche = _git(repo, "rev-parse", "diatche")

    wrapper, health, calls = _fake_runtime(tmp_path, fail_health_call=1)
    state_dir = tmp_path / "state"
    common = [
        "--repo", str(repo),
        "--state-dir", str(state_dir),
        "--wrapperctl", str(wrapper),
        "--health-script", str(health),
    ]
    prepared = subprocess.run(
        [sys.executable, str(SCRIPT), "--pre", *common],
        text=True,
        capture_output=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    _emulate_official_update(repo)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--post", *common],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert _git(repo, "rev-parse", "HEAD") == old_diatche
    assert _git(repo, "status", "--porcelain") == ""
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "failed"
    assert state["recovered"] is True


def test_failure_recovery_retains_original_lock_and_signal_guard(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo, _ = _make_repo(tmp_path)
    wrapper, health, _ = _fake_runtime(tmp_path, fail_health_call=1)
    state_dir = tmp_path / "state"
    observed: dict[str, object] = {}
    original_recover = module._recover_owned_git_state

    def probe_guard(*args, **kwargs):
        lock_file = state_dir / "maintenance.lock"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1], 'a+'); "
                "\ntry: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)"
                "\nexcept BlockingIOError: raise SystemExit(23)",
                str(lock_file),
            ],
            check=False,
        )
        observed["lock_returncode"] = probe.returncode
        observed["signal_handler"] = signal.getsignal(signal.SIGTERM)
        return original_recover(*args, **kwargs)

    prepared = module._run_pre(_args(repo, state_dir, wrapper, health))
    assert prepared == 0
    _emulate_official_update(repo)
    monkeypatch.setattr(module, "_recover_owned_git_state", probe_guard)

    result = module._run_post(_args(repo, state_dir, wrapper, health))

    assert result == 1
    assert observed["lock_returncode"] == 23
    assert callable(observed["signal_handler"]) or observed["signal_handler"] is signal.SIG_IGN


def test_official_updater_failure_restores_owned_git_state_and_old_runtime(
    tmp_path: Path,
) -> None:
    repo, _ = _make_repo(tmp_path)
    old_main = _git(repo, "rev-parse", "main")
    old_origin_main = _git(repo, "rev-parse", "origin/main")
    old_diatche = _git(repo, "rev-parse", "diatche")
    wrapper, health, calls = _fake_runtime(tmp_path)
    state_dir = tmp_path / "state"

    common = [
        "--repo", str(repo),
        "--state-dir", str(state_dir),
        "--wrapperctl", str(wrapper),
        "--health-script", str(health),
    ]
    prepared = subprocess.run(
        [sys.executable, str(SCRIPT), "--pre", *common],
        text=True,
        capture_output=True,
    )
    assert prepared.returncode == 0, prepared.stderr
    # Model an interrupted updater that switched to main before publishing the
    # pinned upstream commit. The post phase must reject and recover this state.
    _git(repo, "switch", "main")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--post", *common],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "official updater" in result.stderr.lower()
    assert _git(repo, "branch", "--show-current") == "diatche"
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "origin/main") == old_origin_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert _git(repo, "rev-parse", "HEAD") == old_diatche
    assert calls.read_text().splitlines() == [
        "--status", "--force-stop", "--foreground", "--status"
    ]
    state = json.loads((state_dir / "state.json").read_text())
    assert state["recovered"] is True
    assert "not rolled back" in state["rollback_scope"]


def test_stop_failure_keeps_refs_and_restarts_old_runtime(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path)
    old_main = _git(repo, "rev-parse", "main")
    old_diatche = _git(repo, "rev-parse", "diatche")

    result, calls, _ = _run(repo, tmp_path, fail_stop=True)

    assert result.returncode == 1
    assert "wrapper --force-stop failed" in result.stderr
    assert _git(repo, "rev-parse", "main") == old_main
    assert _git(repo, "rev-parse", "diatche") == old_diatche
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--status",
        "--force-stop",
        "--foreground",
        "--status",
    ]


def test_hindsight_embedding_guard_repairs_only_incompatible_hub(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo = tmp_path / "repo"
    python = repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    commands: list[tuple[str, ...]] = []
    outcomes = iter([11, 0, 0, 0])

    def fake_run(command, *, cwd, timeout=60, check=False, env=None):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, next(outcomes), "", "")

    monkeypatch.setattr(module, "_run", fake_run)

    module._ensure_hindsight_embeddings(repo, timeout=30)

    assert commands[0][0] == str(python)
    assert commands[0][1] == "-c"
    assert commands[1] == (
        str(python), "-m", "pip", "install", "--no-deps",
        "huggingface-hub>=1.5.0,<2.0",
    )
    assert commands[2] == commands[0]
    assert commands[3][1] == "-c"
    assert commands[3] != commands[0]


def test_hindsight_embedding_guard_is_noop_when_version_and_imports_work(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo = tmp_path / "repo"
    python = repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, *, cwd, timeout=60, check=False, env=None):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "_run", fake_run)

    module._ensure_hindsight_embeddings(repo, timeout=30)

    assert len(commands) == 2
    assert commands[0][1] == "-c"
    assert commands[1][1] == "-c"
    assert commands[1] != commands[0]


def test_hindsight_embedding_guard_does_not_repair_unrelated_import_failure(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo = tmp_path / "repo"
    python = repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    commands: list[tuple[str, ...]] = []
    outcomes = iter([0, 1])

    def fake_run(command, *, cwd, timeout=60, check=False, env=None):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, next(outcomes), "", "broken import")

    monkeypatch.setattr(module, "_run", fake_run)

    with pytest.raises(RuntimeError, match="embedding imports fail"):
        module._ensure_hindsight_embeddings(repo, timeout=30)

    assert len(commands) == 2
    assert all("pip" not in command for command in commands)


def test_hindsight_embedding_guard_does_not_repair_version_probe_crash(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script_module()
    repo = tmp_path / "repo"
    python = repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, *, cwd, timeout=60, check=False, env=None):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, "", "packaging probe crashed")

    monkeypatch.setattr(module, "_run", fake_run)

    with pytest.raises(RuntimeError, match="could not inspect huggingface-hub"):
        module._ensure_hindsight_embeddings(repo, timeout=30)

    assert len(commands) == 1
    assert "pip" not in commands[0]
