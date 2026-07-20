#!/usr/bin/env python3
"""Guarded maintenance updater for Pavel's Hermes integration checkout.

Entrypoints:
  scripts/hermes-maintenance-update.py --check
  scripts/hermes-maintenance-update.py --install
  ~/.hermes/local/bin/hermes-maintenance-update --detach

The updater preserves ``main`` as an upstream mirror and ``diatche`` as the
validated local integration branch. It never resolves merge conflicts. The
installed one-shot LaunchAgent runs independently of HermesGateway.app so it
can stop the wrapper before the official updater and start it once afterward.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


DEFAULT_REPO = Path.home() / ".hermes" / "hermes-agent"
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "local" / "update"
DEFAULT_WRAPPERCTL = Path.home() / ".hermes" / "local" / "bin" / "hermes-gateway-wrapperctl"
DEFAULT_HEALTH_SCRIPT = Path.home() / ".hermes" / "local" / "health" / "hermes_core_health.py"
MAINTENANCE_LABEL = "nz.diatche.hermes-maintenance-update"
DEFAULT_INSTALLED_SCRIPT = Path.home() / ".hermes" / "local" / "bin" / "hermes-maintenance-update"
DEFAULT_MAINTENANCE_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MAINTENANCE_LABEL}.plist"


class MaintenanceBusyError(RuntimeError):
    """Raised when another maintenance worker owns the update lock."""


class MaintenanceInterruptedError(RuntimeError):
    """Raised so SIGINT/SIGTERM enter the same fail-closed recovery path."""


@contextmanager
def _maintenance_signal_guard() -> Iterator[None]:
    previous: dict[int, Any] = {}

    def _raise_interrupted(signum: int, _frame: Any) -> None:
        # Recovery must be allowed to finish after the first interruption.
        for guarded_signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(guarded_signum, signal.SIG_IGN)
        raise MaintenanceInterruptedError(
            f"maintenance interrupted by {signal.Signals(signum).name}"
        )

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _raise_interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = False,
    timeout: float | None = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = list(args)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        raise
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    return completed


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(("git", *args), cwd=repo, check=check)


def _rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", ref).stdout.strip()


def check_mergeability(
    repo: Path,
    *,
    integration_branch: str,
    upstream_ref: str,
) -> dict[str, Any]:
    integration_sha = _rev_parse(repo, integration_branch)
    upstream_sha = _rev_parse(repo, upstream_ref)
    merge = _git(
        repo,
        "merge-tree",
        "--write-tree",
        integration_sha,
        upstream_sha,
        check=False,
    )
    return {
        "ok": merge.returncode == 0,
        "mergeable": merge.returncode == 0,
        "integration_branch": integration_branch,
        "integration_sha": integration_sha,
        "upstream_ref": upstream_ref,
        "upstream_sha": upstream_sha,
        "merge_tree": merge.stdout.strip(),
        "error": merge.stderr.strip() if merge.returncode else "",
    }


def _write_state(state_dir: Path, payload: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "state.json"
    temporary = state_dir / f".{target.name}.tmp"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


@contextmanager
def _exclusive_lock(state_dir: Path, *, wait_seconds: float = 0) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "maintenance.lock"
    deadline = time.monotonic() + wait_seconds
    with lock_path.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise MaintenanceBusyError("another Hermes maintenance update is already running") from exc
                time.sleep(0.1)
        yield


def _active_marker(state_dir: Path) -> Path:
    return state_dir / "active.json"


def _claim_active_lifecycle(state_dir: Path, run_id: str) -> Path:
    marker = _active_marker(state_dir)
    payload = json.dumps({"run_id": run_id, "pid": os.getpid()}) + "\n"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise MaintenanceBusyError(
            f"maintenance lifecycle marker exists; inspect {marker} before retrying"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return marker


def _assert_no_active_lifecycle(state_dir: Path) -> None:
    marker = _active_marker(state_dir)
    if marker.exists():
        raise MaintenanceBusyError(
            f"maintenance lifecycle is still active or needs recovery: {marker}"
        )


def _require_clean_integration_checkout(repo: Path, integration_branch: str) -> None:
    current = _git(repo, "branch", "--show-current").stdout.strip()
    if current != integration_branch:
        raise RuntimeError(
            f"checkout must be on '{integration_branch}', currently on '{current or 'detached HEAD'}'"
        )
    dirty = _git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        raise RuntimeError("checkout is dirty; commit, stash, or remove local changes first")
    _assert_no_git_operation(repo)

    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
    current_path: Path | None = None
    for line in worktrees:
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch refs/heads/") and current_path != repo:
            branch = line.removeprefix("branch refs/heads/")
            if branch in {integration_branch, "main"}:
                raise RuntimeError(f"relevant branch '{branch}' is checked out in {current_path}")


def _assert_no_git_operation(repo: Path) -> None:
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
    ):
        git_path_text = _git(repo, "rev-parse", "--git-path", name).stdout.strip()
        git_path = Path(git_path_text)
        if not git_path.is_absolute():
            git_path = repo / git_path
        if git_path_text and git_path.exists():
            raise RuntimeError(f"unfinished Git operation detected: {name}")

def _create_backup_refs(repo: Path, integration_branch: str, upstream_branch: str) -> dict[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    branches = [integration_branch, upstream_branch]
    feature_lines = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/feature/",
    ).stdout.splitlines()
    branches.extend(branch for branch in feature_lines if branch)
    backups: dict[str, str] = {}
    for branch in dict.fromkeys(branches):
        sha = _rev_parse(repo, branch)
        backup = f"backup/maintenance-{stamp}/{branch}"
        created = _git(
            repo,
            "update-ref",
            f"refs/heads/{backup}",
            sha,
            "0" * 40,
            check=False,
        )
        if created.returncode:
            raise RuntimeError(f"backup ref already exists: {backup}")
        backups[branch] = backup
    return backups


def _find_pytest_python(source_repo: Path) -> Path | None:
    candidates = (
        source_repo / "venv" / "bin" / "python",
        Path.home() / "miniconda3" / "bin" / "python",
        Path(sys.executable),
    )
    for python in candidates:
        if not python.is_file():
            continue
        probe = _run((str(python), "-c", "import pytest"), cwd=source_repo)
        if probe.returncode == 0:
            return python
    return None


def _validate_candidate(
    candidate: Path,
    old_sha: str,
    candidate_sha: str,
    *,
    source_repo: Path,
) -> None:
    diff_check = _git(candidate, "diff", "--check", f"{old_sha}..{candidate_sha}", check=False)
    if diff_check.returncode:
        raise RuntimeError(diff_check.stdout.strip() or diff_check.stderr.strip() or "git diff --check failed")

    compile_roots = [
        candidate / name
        for name in ("agent", "gateway", "hermes_cli", "tools", "scripts")
        if (candidate / name).is_dir()
    ]
    if compile_roots:
        compiled = _run(
            (sys.executable, "-m", "compileall", "-q", *(str(path) for path in compile_roots)),
            cwd=candidate,
        )
        if compiled.returncode:
            raise RuntimeError(compiled.stderr.strip() or "Python compile validation failed")

    focused_paths = [
        relative
        for relative in (
            "tests/gateway/test_restart_resume_pending.py",
            "tests/gateway/test_todo_progress.py",
            "tests/gateway/test_run_progress_topics.py",
            "tests/gateway/test_display_config.py",
        )
        if (candidate / relative).is_file()
    ]
    if focused_paths:
        python = _find_pytest_python(source_repo)
        if python is None:
            raise RuntimeError("focused patch tests exist but no Python environment can import pytest")
        tested = _run(
            (str(python), "-m", "pytest", *focused_paths, "-q", "--tb=short"),
            cwd=candidate,
        )
        if tested.returncode:
            detail = tested.stdout[-8000:] + tested.stderr[-4000:]
            raise RuntimeError(f"focused patch tests failed:\n{detail.strip()}")


def _build_candidate(
    repo: Path,
    *,
    integration_sha: str,
    upstream_sha: str,
    state_dir: Path,
    candidate_ref: str,
) -> str:
    """Build and retain the exact tested merge without moving maintained branches."""
    temp_root = Path(tempfile.mkdtemp(prefix="hermes-maintenance-candidate-", dir=state_dir))
    candidate = temp_root / "worktree"
    added = False
    try:
        _git(repo, "worktree", "add", "--detach", str(candidate), integration_sha)
        added = True
        merged = _git(
            candidate,
            "-c",
            "user.name=Hermes Maintenance",
            "-c",
            "user.email=maintenance@localhost",
            "merge",
            "--no-ff",
            "--no-edit",
            upstream_sha,
            check=False,
        )
        if merged.returncode:
            raise RuntimeError(merged.stdout.strip() or merged.stderr.strip() or "candidate merge failed")
        candidate_sha = _rev_parse(candidate, "HEAD")
        _validate_candidate(candidate, integration_sha, candidate_sha, source_repo=repo)
        published = _git(
            repo,
            "update-ref",
            candidate_ref,
            candidate_sha,
            "0" * 40,
            check=False,
        )
        if published.returncode:
            raise RuntimeError(f"candidate ref already exists or moved: {candidate_ref}")
    finally:
        if added:
            _git(repo, "worktree", "remove", "--force", str(candidate))
        shutil.rmtree(temp_root)
    if _rev_parse(repo, candidate_ref) != candidate_sha:
        raise RuntimeError("candidate ref does not resolve to the tested commit")
    return candidate_sha


def _promote_candidate(
    repo: Path,
    *,
    integration_branch: str,
    integration_before: str,
    upstream_branch: str,
    upstream_ref: str,
    upstream_sha: str,
    candidate_ref: str,
    candidate_sha: str,
) -> None:
    commands = "\n".join(
        (
            "start",
            f"verify refs/heads/{upstream_branch} {upstream_sha}",
            f"verify refs/remotes/{upstream_ref} {upstream_sha}",
            f"verify {candidate_ref} {candidate_sha}",
            f"update refs/heads/{integration_branch} {candidate_sha} {integration_before}",
            "prepare",
            "commit",
            "",
        )
    )
    promoted = subprocess.run(
        ("git", "update-ref", "--stdin"),
        cwd=repo,
        input=commands,
        capture_output=True,
        text=True,
        check=False,
    )
    if promoted.returncode:
        detail = promoted.stderr.strip() or promoted.stdout.strip()
        raise RuntimeError(
            f"could not atomically verify refs and promote tested candidate to {integration_branch}: {detail}"
        )


def _restore_checkout_exact(repo: Path, branch: str, expected_sha: str) -> None:
    _assert_no_git_operation(repo)
    switched = _git(repo, "switch", branch, check=False)
    if switched.returncode:
        raise RuntimeError(switched.stderr.strip() or f"could not switch to {branch}")
    current = _git(repo, "branch", "--show-current").stdout.strip()
    head = _rev_parse(repo, "HEAD")
    branch_sha = _rev_parse(repo, branch)
    if current != branch or head != expected_sha or branch_sha != expected_sha:
        raise RuntimeError(
            f"refusing runtime start: expected {branch}@{expected_sha}, got {current or 'detached'}@{head}"
        )
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("refusing runtime start from a dirty checkout")
    _assert_no_git_operation(repo)


def _validate_config(hermes: Path, repo: Path) -> None:
    checked = _run((str(hermes), "config", "check"), cwd=repo)
    if checked.returncode:
        detail = checked.stderr.strip() or checked.stdout.strip()
        raise RuntimeError(f"Hermes config validation failed: {detail}")


def _validate_runtime_dependencies(repo: Path) -> None:
    """Validate offline-safe runtime pieces while the gateway is stopped."""
    python = repo / "venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"gateway Python is missing after update: {python}")
    imported = _run(
        (
            str(python),
            "-c",
            "import gateway.run; import huggingface_hub, transformers, sentence_transformers",
        ),
        cwd=repo,
        timeout=60,
    )
    if imported.returncode:
        detail = imported.stderr.strip() or imported.stdout.strip()
        raise RuntimeError(f"post-update runtime imports failed: {detail}")

    hindsight = repo / "venv" / "bin" / "hindsight-embed"
    if not hindsight.is_file():
        raise RuntimeError(f"Hindsight recall executable is missing after update: {hindsight}")
    try:
        recalled = _run(
            (
                str(hindsight),
                "-p",
                "hermes",
                "memory",
                "recall",
                "hermes",
                "Hermes post-upgrade health probe",
            ),
            cwd=repo,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("real Hindsight recall timed out after 60 seconds") from exc
    if recalled.returncode:
        raise RuntimeError(f"real Hindsight recall failed (exit {recalled.returncode})")


def _validate_live_health(repo: Path, health_script: Path) -> None:
    """Run topology-aware health only after the custom wrapper is running."""
    if not health_script.is_file():
        raise RuntimeError(f"mandatory post-update health script is missing: {health_script}")
    python = repo / "venv" / "bin" / "python"
    checked = _run(
        (str(python), str(health_script), "--check-only", "--no-state", "--json"),
        cwd=repo,
        timeout=120,
    )
    if checked.returncode:
        detail = checked.stdout.strip() or checked.stderr.strip()
        raise RuntimeError(f"post-update live health check failed: {detail}")
    try:
        payload = json.loads(checked.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("post-update live health check did not return JSON") from exc
    if payload.get("healthy") is not True:
        raise RuntimeError(f"post-update live health check is unhealthy: {checked.stdout.strip()}")


def _start_and_validate_wrapper(
    repo: Path,
    wrapper: Path,
    health_script: Path,
) -> None:
    try:
        restart_env = {**os.environ, "HERMES_MAINTENANCE_START_ALLOWED": "1"}
        restarted = _run(
            (str(wrapper), "--foreground"),
            cwd=repo,
            timeout=60,
            env=restart_env,
        )
        if restarted.returncode:
            raise RuntimeError(restarted.stderr.strip() or "wrapper restart failed")
        verified = _run((str(wrapper), "--status"), cwd=repo, timeout=30)
        if verified.returncode:
            raise RuntimeError("wrapper status failed after restart")
        _validate_live_health(repo, health_script)
    except BaseException:
        _run((str(wrapper), "--stop"), cwd=repo, check=False, timeout=45)
        raise


def _run_maintenance(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    upstream_ref = f"{args.remote}/{args.upstream_branch}"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    preupdate_candidate_ref = f"refs/hermes-maintenance/candidates/{run_id}/pre-update"
    candidate_ref = f"refs/hermes-maintenance/candidates/{run_id}/post-update"
    state: dict[str, Any] = {
        "ok": False,
        "phase": "preflight",
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "candidate_ref": preupdate_candidate_ref,
    }
    wrapper_stopped = False
    integration_before = ""
    runtime_sha = ""
    queued_state = state_dir / "state.json"
    lock_wait = 0.0
    queued_token = ""
    lifecycle_marker: Path | None = None
    if queued_state.is_file():
        try:
            queued_payload = json.loads(queued_state.read_text(encoding="utf-8"))
            if queued_payload.get("phase") == "queued":
                lock_wait = 5.0
                queued_token = str(queued_payload.get("queue_token") or "")
        except (json.JSONDecodeError, OSError):
            pass
    try:
        with _maintenance_signal_guard(), _exclusive_lock(state_dir, wait_seconds=lock_wait):
            try:
                lifecycle_marker = _claim_active_lifecycle(state_dir, run_id)
                if queued_token:
                    claimed = json.loads(queued_state.read_text(encoding="utf-8"))
                    if claimed.get("phase") != "queued" or claimed.get("queue_token") != queued_token:
                        raise RuntimeError("queued maintenance token changed before worker claim")
                    state["queue_token"] = queued_token
                    state["claimed_at"] = datetime.now(timezone.utc).isoformat()
                    _write_state(state_dir, state)
                _require_clean_integration_checkout(repo, args.integration_branch)
                if not args.no_fetch:
                    fetched = _git(repo, "fetch", args.remote, args.upstream_branch, check=False)
                    if fetched.returncode:
                        raise RuntimeError(fetched.stderr.strip() or "fetch failed")

                integration_before = _rev_parse(repo, args.integration_branch)
                runtime_sha = integration_before
                main_before = _rev_parse(repo, args.upstream_branch)
                upstream_sha = _rev_parse(repo, upstream_ref)
                preflight = check_mergeability(
                    repo,
                    integration_branch=integration_before,
                    upstream_ref=upstream_sha,
                )
                if not preflight["mergeable"]:
                    raise RuntimeError(
                        f"{args.integration_branch} conflicts with {upstream_ref}; no maintained refs were changed"
                    )

                wrapper = args.wrapperctl.expanduser().resolve()
                hermes = args.hermes.expanduser().resolve()
                health_script = args.health_script.expanduser().resolve()
                if not health_script.is_file():
                    raise RuntimeError(f"mandatory post-update health script is missing: {health_script}")
                _validate_config(hermes, repo)
                status = _run((str(wrapper), "--status"), cwd=repo, timeout=30)
                if status.returncode:
                    raise RuntimeError("custom Hermes wrapper is not healthy before maintenance")

                state["phase"] = "validating-candidate"
                backups = _create_backup_refs(repo, args.integration_branch, args.upstream_branch)
                state.update(
                    backup_refs=backups,
                    integration_before=integration_before,
                    main_before=main_before,
                    upstream_sha=upstream_sha,
                )
                _write_state(state_dir, state)
                candidate_sha = _build_candidate(
                    repo,
                    integration_sha=integration_before,
                    upstream_sha=upstream_sha,
                    state_dir=state_dir,
                    candidate_ref=preupdate_candidate_ref,
                )
                state["candidate_sha"] = candidate_sha
                _write_state(state_dir, state)

                if _rev_parse(repo, args.integration_branch) != integration_before:
                    raise RuntimeError(f"{args.integration_branch} moved during candidate validation")
                if _rev_parse(repo, args.upstream_branch) != main_before:
                    raise RuntimeError(f"{args.upstream_branch} moved during candidate validation")
                if _rev_parse(repo, upstream_ref) != upstream_sha:
                    raise RuntimeError("upstream moved before quiesce; run maintenance again")
                if _rev_parse(repo, preupdate_candidate_ref) != candidate_sha:
                    raise RuntimeError("candidate ref moved before quiesce")

                state["phase"] = "updating"
                _write_state(state_dir, state)
                stopped = _run((str(wrapper), "--stop"), cwd=repo, timeout=45)
                if stopped.returncode:
                    raise RuntimeError(stopped.stderr.strip() or "could not quiesce Hermes wrapper")
                wrapper_stopped = True
                quiescent = _run(
                    (str(wrapper), "--assert-update-quiescence"),
                    cwd=repo,
                    timeout=30,
                )
                if quiescent.returncode:
                    raise RuntimeError(
                        quiescent.stderr.strip()
                        or "another Hermes gateway can interfere with the official updater"
                    )

                try:
                    updated = _run(
                        (
                            str(hermes),
                            "update",
                            "--branch",
                            args.upstream_branch,
                            "--backup",
                            "--yes",
                            "--no-gateway-restart",
                        ),
                        cwd=repo,
                        timeout=args.update_timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"hermes update timed out after {args.update_timeout} seconds") from exc
                if updated.returncode:
                    detail = updated.stderr.strip() or updated.stdout.strip()
                    suffix = f": {detail}" if detail else ""
                    raise RuntimeError(f"hermes update failed (exit {updated.returncode}){suffix}")
                quiescent = _run(
                    (str(wrapper), "--assert-update-quiescence"),
                    cwd=repo,
                    timeout=30,
                )
                if quiescent.returncode:
                    raise RuntimeError(
                        quiescent.stderr.strip()
                        or "a gateway or restart actor appeared during the official update"
                    )
                exclusive = _run((str(wrapper), "--enforce-exclusivity"), cwd=repo, timeout=30)
                if exclusive.returncode:
                    raise RuntimeError("could not enforce custom-gateway exclusivity after update")

                main_sha = _rev_parse(repo, args.upstream_branch)
                current_upstream_sha = _rev_parse(repo, upstream_ref)
                if main_sha != current_upstream_sha:
                    raise RuntimeError(
                        f"{args.upstream_branch} ({main_sha}) does not match {upstream_ref} ({current_upstream_sha})"
                    )
                candidate_sha = _build_candidate(
                    repo,
                    integration_sha=integration_before,
                    upstream_sha=current_upstream_sha,
                    state_dir=state_dir,
                    candidate_ref=candidate_ref,
                )
                upstream_sha = current_upstream_sha
                state.update(
                    candidate_sha=candidate_sha,
                    candidate_ref=candidate_ref,
                    upstream_sha=upstream_sha,
                )
                _write_state(state_dir, state)

                if _rev_parse(repo, args.upstream_branch) != upstream_sha:
                    raise RuntimeError("main moved after candidate validation")
                if _rev_parse(repo, upstream_ref) != upstream_sha:
                    raise RuntimeError("upstream moved after candidate validation; run maintenance again")
                if _rev_parse(repo, args.integration_branch) != integration_before:
                    raise RuntimeError(f"{args.integration_branch} moved before candidate promotion")
                if _rev_parse(repo, candidate_ref) != candidate_sha:
                    raise RuntimeError("candidate ref moved before promotion")
                _promote_candidate(
                    repo,
                    integration_branch=args.integration_branch,
                    integration_before=integration_before,
                    upstream_branch=args.upstream_branch,
                    upstream_ref=upstream_ref,
                    upstream_sha=upstream_sha,
                    candidate_ref=candidate_ref,
                    candidate_sha=candidate_sha,
                )
                runtime_sha = candidate_sha
                _restore_checkout_exact(repo, args.integration_branch, candidate_sha)
                if _git(
                    repo,
                    "merge-base",
                    "--is-ancestor",
                    upstream_sha,
                    candidate_sha,
                    check=False,
                ).returncode:
                    raise RuntimeError("tested candidate does not contain the installed upstream tip")
                if _rev_parse(repo, candidate_ref) != candidate_sha:
                    raise RuntimeError("promoted runtime no longer matches the retained tested candidate")
                _validate_config(hermes, repo)
                _validate_runtime_dependencies(repo)
                if _rev_parse(repo, args.upstream_branch) != upstream_sha:
                    raise RuntimeError("main moved before runtime restart")
                if _rev_parse(repo, upstream_ref) != upstream_sha:
                    raise RuntimeError("upstream moved before runtime restart; rerun maintenance")
                _restore_checkout_exact(repo, args.integration_branch, candidate_sha)

                state["phase"] = "restarting-wrapper"
                _write_state(state_dir, state)
                _start_and_validate_wrapper(repo, wrapper, health_script)
                _restore_checkout_exact(repo, args.integration_branch, candidate_sha)
                if (
                    _rev_parse(repo, args.upstream_branch) != upstream_sha
                    or _rev_parse(repo, upstream_ref) != upstream_sha
                    or _rev_parse(repo, args.integration_branch) != candidate_sha
                    or _rev_parse(repo, candidate_ref) != candidate_sha
                ):
                    _run((str(wrapper), "--stop"), cwd=repo, timeout=45)
                    raise RuntimeError("repository refs moved during runtime restart; maintenance not complete")
                wrapper_stopped = False

                state.update(
                    ok=True,
                    phase="complete",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    wrapper_restored=True,
                )
                _write_state(state_dir, state)
                lifecycle_marker.unlink(missing_ok=True)
                lifecycle_marker = None
                print(f"Hermes maintenance update complete on {args.integration_branch} at {candidate_sha[:12]}")
                return 0
            except Exception as exc:
                state.update(
                    ok=False,
                    phase="failed",
                    error=str(exc),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                try:
                    if wrapper_stopped:
                        if not runtime_sha:
                            raise RuntimeError("no validated runtime SHA is available for recovery")
                        hermes = args.hermes.expanduser().resolve()
                        wrapper = args.wrapperctl.expanduser().resolve()
                        health_script = args.health_script.expanduser().resolve()
                        _restore_checkout_exact(repo, args.integration_branch, runtime_sha)
                        _validate_config(hermes, repo)
                        _validate_runtime_dependencies(repo)
                        _start_and_validate_wrapper(repo, wrapper, health_script)
                        wrapper_stopped = False
                        state["wrapper_restored"] = True
                except Exception as recovery_exc:
                    state["wrapper_restored"] = False
                    state["recovery_error"] = str(recovery_exc)
                _write_state(state_dir, state)
                if lifecycle_marker is not None and not state.get("recovery_error"):
                    lifecycle_marker.unlink(missing_ok=True)
                    lifecycle_marker = None
                print(f"ERROR: {exc}", file=sys.stderr)
                if state.get("recovery_error"):
                    print(f"RECOVERY ERROR: {state['recovery_error']}", file=sys.stderr)
                return 1
    except MaintenanceBusyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _maintenance_plist_payload(args: argparse.Namespace) -> dict[str, Any]:
    installed = args.installed_script.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    logs = Path.home() / ".hermes" / "logs"
    return {
        "Label": MAINTENANCE_LABEL,
        "ProgramArguments": [
            str(installed),
            "--run",
            "--repo",
            str(repo),
            "--state-dir",
            str(state_dir),
        ],
        "WorkingDirectory": str(repo),
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "hermes-maintenance-update.log"),
        "StandardErrorPath": str(logs / "hermes-maintenance-update.error.log"),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "HERMES_HOME": str(Path.home() / ".hermes"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_copy(source: Path, destination: Path, mode: int = 0o755) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.chmod(mode)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _install_transaction_dir(args: argparse.Namespace) -> Path:
    return args.state_dir.expanduser().resolve() / "install-transaction"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _recover_install_transaction(args: argparse.Namespace) -> None:
    transaction = _install_transaction_dir(args)
    journal_path = transaction / "journal.json"
    if not journal_path.is_file():
        if transaction.exists():
            shutil.rmtree(transaction)
        return
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if payload.get("phase") == "complete":
        shutil.rmtree(transaction)
        return

    repo = args.repo.expanduser().resolve()
    service = str(payload["service"])
    domain = str(payload["domain"])
    errors: list[str] = []
    _run(("launchctl", "bootout", service), cwd=repo, check=False)
    for item in payload["items"]:
        target = Path(item["target"])
        try:
            if item["existed"]:
                backup = transaction / item["backup"]
                if not backup.is_file():
                    raise RuntimeError(f"backup is missing: {backup}")
                _atomic_copy(backup, target, mode=int(item["mode"]))
            else:
                target.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"could not restore {target}: {exc}")

    if payload.get("was_loaded"):
        plist = Path(payload["plist"])
        restored = _run(("launchctl", "bootstrap", domain, str(plist)), cwd=repo)
        verified = _run(("launchctl", "print", service), cwd=repo)
        if restored.returncode or verified.returncode:
            errors.append("previous maintenance LaunchAgent could not be restored")
    elif _run(("launchctl", "print", service), cwd=repo).returncode == 0:
        errors.append("replacement maintenance LaunchAgent remained loaded during rollback")
    if errors:
        raise RuntimeError("; ".join(errors))
    shutil.rmtree(transaction)


def _install_unlocked(args: argparse.Namespace) -> int:
    installed = args.installed_script.expanduser().resolve()
    plist = args.maintenance_plist.expanduser().resolve()
    wrapperctl = args.wrapperctl.expanduser().resolve()
    source = Path(__file__).resolve()
    wrapper_source = source.with_name("restart-hermes-wrapper.sh")
    args.state_dir.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{MAINTENANCE_LABEL}"
    try:
        _recover_install_transaction(args)
    except Exception as exc:
        print(f"ERROR: could not recover interrupted prior install: {exc}", file=sys.stderr)
        return 1
    if not wrapper_source.is_file():
        print(f"ERROR: wrapper controller source is missing: {wrapper_source}", file=sys.stderr)
        return 2

    installed.parent.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".hermes" / "logs").mkdir(parents=True, exist_ok=True)

    # Validate a complete replacement set before touching installed files.
    with tempfile.TemporaryDirectory(
        prefix="hermes-maintenance-install-",
        dir=args.state_dir.expanduser().resolve(),
    ) as staging_text:
        staging = Path(staging_text)
        staged_updater = staging / "hermes-maintenance-update"
        staged_wrapper = staging / "hermes-gateway-wrapperctl"
        staged_plist = staging / plist.name
        shutil.copy2(source, staged_updater)
        staged_updater.chmod(0o755)
        shutil.copy2(wrapper_source, staged_wrapper)
        staged_wrapper.chmod(0o755)
        with staged_plist.open("wb") as handle:
            plistlib.dump(_maintenance_plist_payload(args), handle, sort_keys=True)

        checks = (
            _run((sys.executable, "-m", "py_compile", str(staged_updater)), cwd=args.repo.expanduser().resolve()),
            _run(("bash", "-n", str(staged_wrapper)), cwd=args.repo.expanduser().resolve()),
            _run(("plutil", "-lint", str(staged_plist)), cwd=args.repo.expanduser().resolve()),
        )
        failed = next((result for result in checks if result.returncode), None)
        if failed is not None:
            print(failed.stderr.strip() or failed.stdout.strip(), file=sys.stderr)
            return 1

        targets = {
            installed: staged_updater,
            wrapperctl: staged_wrapper,
            plist: staged_plist,
        }
        transaction = _install_transaction_dir(args)
        transaction.mkdir(parents=True)
        # Publish the rollback transaction directory durably before any installed
        # target can be replaced, so a crash cannot lose the journal namespace.
        _fsync_directory(transaction.parent)
        backups = transaction / "backups"
        backups.mkdir()
        items: list[dict[str, Any]] = []
        for index, target in enumerate(targets):
            existed = target.exists()
            mode = target.stat().st_mode & 0o777 if existed else 0o755
            backup_name = f"backups/{index}"
            if existed:
                backup_path = transaction / backup_name
                shutil.copy2(target, backup_path)
                with backup_path.open("rb") as handle:
                    os.fsync(handle.fileno())
            items.append(
                {
                    "target": str(target),
                    "existed": existed,
                    "mode": mode,
                    "backup": backup_name,
                }
            )

        backups_fd = os.open(backups, os.O_RDONLY)
        try:
            os.fsync(backups_fd)
        finally:
            os.close(backups_fd)
        was_loaded = (
            _run(("launchctl", "print", service), cwd=args.repo.expanduser().resolve()).returncode == 0
        )
        journal_path = transaction / "journal.json"
        journal: dict[str, Any] = {
            "phase": "prepared",
            "domain": domain,
            "service": service,
            "plist": str(plist),
            "was_loaded": was_loaded,
            "items": items,
        }
        _write_json_atomic(journal_path, journal)
        try:
            for target, staged in targets.items():
                _atomic_copy(staged, target)
            journal["phase"] = "files-replaced"
            _write_json_atomic(journal_path, journal)
            if args.no_load:
                journal["phase"] = "complete"
                _write_json_atomic(journal_path, journal)
                shutil.rmtree(transaction)
                print(f"Installed without loading LaunchAgent: {installed}")
                return 0

            _run(("launchctl", "bootout", service), cwd=args.repo.expanduser().resolve(), check=False)
            journal["phase"] = "old-job-stopped"
            _write_json_atomic(journal_path, journal)
            loaded = _run(("launchctl", "bootstrap", domain, str(plist)), cwd=args.repo.expanduser().resolve())
            verified = _run(("launchctl", "print", service), cwd=args.repo.expanduser().resolve())
            if loaded.returncode or verified.returncode:
                detail = loaded.stderr.strip() or verified.stderr.strip() or "could not load maintenance LaunchAgent"
                raise RuntimeError(detail)
            journal["phase"] = "complete"
            _write_json_atomic(journal_path, journal)
            shutil.rmtree(transaction)
        except BaseException as exc:
            rollback_error = ""
            try:
                _recover_install_transaction(args)
            except Exception as recovery_exc:
                rollback_error = f"; rollback remains journaled and failed: {recovery_exc}"
            if rollback_error:
                message = f"ERROR: install failed; rollback was attempted: {exc}{rollback_error}"
            else:
                message = f"ERROR: install failed and prior files were restored: {exc}"
            print(message, file=sys.stderr)
            return 1

    print(f"Installed: {installed}")
    print(f"Loaded one-shot LaunchAgent: {domain}/{MAINTENANCE_LABEL}")
    return 0


def _install(args: argparse.Namespace) -> int:
    try:
        with _maintenance_signal_guard(), _exclusive_lock(args.state_dir.expanduser().resolve()):
            _assert_no_active_lifecycle(args.state_dir.expanduser().resolve())
            return _install_unlocked(args)
    except MaintenanceBusyError as exc:
        print(f"ERROR: cannot install while maintenance is active: {exc}", file=sys.stderr)
        return 1


def _detach(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    try:
        _require_clean_integration_checkout(repo, args.integration_branch)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{MAINTENANCE_LABEL}"
    if _run(("launchctl", "print", service), cwd=repo).returncode:
        print(f"ERROR: maintenance LaunchAgent is not loaded; run {Path(__file__).name} --install", file=sys.stderr)
        return 1
    try:
        state_dir = args.state_dir.expanduser().resolve()
        with _exclusive_lock(state_dir):
            _assert_no_active_lifecycle(state_dir)
            state_path = state_dir / "state.json"
            if state_path.is_file():
                try:
                    existing = json.loads(state_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing = {}
                if existing.get("phase") in {
                    "queued",
                    "preflight",
                    "validating-candidate",
                    "updating",
                    "restarting-wrapper",
                }:
                    raise MaintenanceBusyError(
                        f"maintenance is already {existing.get('phase')}"
                    )
            queue_token = uuid.uuid4().hex
            _write_state(
                state_dir,
                {
                    "ok": False,
                    "phase": "queued",
                    "queue_token": queue_token,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                    "repo": str(repo),
                },
            )
    except MaintenanceBusyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    started = _run(("launchctl", "kickstart", service), cwd=repo, timeout=15)
    if started.returncode:
        _write_state(
            state_dir,
            {
                "ok": False,
                "phase": "failed",
                "queue_token": queue_token,
                "error": started.stderr.strip() or "could not start maintenance job",
            },
        )
        print(started.stderr.strip() or "ERROR: could not start maintenance job", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            claimed = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            claimed = {}
        if claimed.get("queue_token") != queue_token:
            print("ERROR: queued maintenance token was replaced before claim", file=sys.stderr)
            return 1
        phase = str(claimed.get("phase") or "")
        if phase == "failed":
            detail = str(claimed.get("error") or "maintenance worker failed after claiming the queue")
            print(f"ERROR: {detail}", file=sys.stderr)
            return 1
        if claimed.get("claimed_at") and phase in {
            "preflight",
            "validating-candidate",
            "updating",
            "restarting-wrapper",
            "complete",
        }:
            break
        time.sleep(0.1)
    else:
        print("ERROR: maintenance worker did not acknowledge the queued token", file=sys.stderr)
        return 1
    print("Hermes maintenance update queued in its independent LaunchAgent.")
    print("The gateway will stop only after merge simulation and focused tests pass.")
    return 0


def _status(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    state_path = args.state_dir.expanduser().resolve() / "state.json"
    if state_path.is_file():
        print(state_path.read_text(encoding="utf-8").rstrip())
    else:
        print("No maintenance run has been recorded.")
    branch = _git(repo, "branch", "--show-current", check=False).stdout.strip()
    main_sha = _rev_parse(repo, args.upstream_branch)
    upstream_ref = f"{args.remote}/{args.upstream_branch}"
    upstream_sha = _rev_parse(repo, upstream_ref)
    contains = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        upstream_sha,
        args.integration_branch,
        check=False,
    ).returncode == 0
    print(f"checkout_branch: {branch or 'detached HEAD'}")
    print(f"main_matches_upstream: {main_sha == upstream_sha}")
    print(f"{args.integration_branch}_contains_upstream: {contains}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--integration-branch", default="diatche")
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--remote", default="origin")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="read-only compatibility preflight")
    modes.add_argument("--run", action="store_true", help="run maintenance in the foreground")
    parser.add_argument("--no-fetch", action="store_true", help="do not refresh the remote ref")
    parser.add_argument("--json", action="store_true")
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--detach", action="store_true")
    modes.add_argument("--status", action="store_true")
    parser.add_argument("--no-load", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--update-timeout", type=int, default=1800, help=argparse.SUPPRESS)
    parser.add_argument(
        "--hermes",
        type=Path,
        default=DEFAULT_REPO / "venv" / "bin" / "hermes",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--wrapperctl",
        type=Path,
        default=DEFAULT_WRAPPERCTL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--health-script",
        type=Path,
        default=DEFAULT_HEALTH_SCRIPT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--installed-script",
        type=Path,
        default=DEFAULT_INSTALLED_SCRIPT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--maintenance-plist",
        type=Path,
        default=DEFAULT_MAINTENANCE_PLIST,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"ERROR: not a Git checkout: {repo}", file=sys.stderr)
        return 2

    if args.check:
        if not args.no_fetch:
            fetch = _git(repo, "fetch", args.remote, args.upstream_branch, check=False)
            if fetch.returncode:
                print(fetch.stderr.strip() or "ERROR: fetch failed", file=sys.stderr)
                return fetch.returncode or 1
        result = check_mergeability(
            repo,
            integration_branch=args.integration_branch,
            upstream_ref=f"{args.remote}/{args.upstream_branch}",
        )
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        elif result["mergeable"]:
            print(
                f"OK: {args.integration_branch} can merge "
                f"{args.remote}/{args.upstream_branch} cleanly"
            )
        else:
            print(
                f"BLOCKED: {args.integration_branch} conflicts with "
                f"{args.remote}/{args.upstream_branch}",
                file=sys.stderr,
            )
        return 0 if result["ok"] else 1

    if args.run:
        return _run_maintenance(args)
    if args.install:
        return _install(args)
    if args.detach:
        return _detach(args)
    if args.status:
        return _status(args)

    print("ERROR: choose --check, --run, --install, --detach, or --status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
