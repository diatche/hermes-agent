#!/usr/bin/env python3
"""Narrow, crash-recoverable Git + wrapper maintenance transaction.

Usage: ``hermes-maintenance-update.py --run|--recover|--check|--status``.
The command requests the gateway's existing external-drain protocol, fetches an
immutable upstream tip into a private ref, creates a merge commit in an isolated
worktree, and atomically publishes refs.  It never runs ``hermes update``, a
general dependency update, a build, a backup, or profile/config/cache
synchronization.  It does enforce the one compatibility constraint required by
the configured local Hindsight embedding stack before starting the new runtime.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
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
DEFAULT_DRAIN_MARKER = Path.home() / ".hermes" / ".drain_request.json"
DEFAULT_GATEWAY_STATUS = Path.home() / ".hermes" / "gateway_state.json"
MAINTENANCE_LABEL = "nz.diatche.hermes-maintenance-update"
DEFAULT_INSTALLED_SCRIPT = (
    Path.home() / ".hermes" / "local" / "bin" / "hermes-maintenance-update"
)
DEFAULT_MAINTENANCE_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{MAINTENANCE_LABEL}.plist"
)
ZERO_OID = "0" * 40
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_PHASES = {"complete", "recovered"}
HINDSIGHT_HUB_REQUIREMENT = "huggingface-hub>=1.5.0,<2.0"
HINDSIGHT_HUB_MISSING = 10
HINDSIGHT_HUB_INCOMPATIBLE = 11
HINDSIGHT_HUB_VERSION_PROBE = """\
from importlib.metadata import PackageNotFoundError, version
from packaging.specifiers import SpecifierSet
try:
    installed = version("huggingface-hub")
except PackageNotFoundError:
    raise SystemExit(10)
raise SystemExit(
    0 if SpecifierSet(">=1.5.0,<2.0").contains(installed, prereleases=True) else 11
)
"""
HINDSIGHT_IMPORT_PROBE = """\
import hindsight_embed.daemon_embed_manager
import huggingface_hub
import sentence_transformers
import transformers
"""


class BusyError(RuntimeError):
    pass


class MaintenanceInterruptedError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(
    command: Sequence[str], *, cwd: Path, timeout: float = 60, check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a bounded child in its own process group and reap its descendants."""
    process = subprocess.Popen(
        list(command), cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=3)
        raise
    result = subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )
    return result


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return _run(("git", *args), cwd=repo, timeout=timeout, check=check)


def _oid(repo: Path, ref: str) -> str:
    value = _git(repo, "rev-parse", "--verify", ref).stdout.strip()
    if not OID_RE.fullmatch(value):
        raise RuntimeError(f"invalid object id for {ref}")
    return value


def _common_git_dir(repo: Path) -> Path:
    value = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(value).resolve()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


@contextmanager
def _signal_guard() -> Iterator[None]:
    previous: dict[int, Any] = {}

    def interrupted(signum: int, _frame: Any) -> None:
        for guarded in (signal.SIGINT, signal.SIGTERM):
            signal.signal(guarded, signal.SIG_IGN)
        raise MaintenanceInterruptedError(
            f"maintenance interrupted by {signal.Signals(signum).name}"
        )

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def _lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "maintenance.lock").open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyError("another maintenance or recovery operation is active") from exc
        yield


def _assert_checkout(repo: Path, integration_branch: str) -> None:
    if _git(repo, "branch", "--show-current").stdout.strip() != integration_branch:
        raise RuntimeError(f"checkout must be clean and on {integration_branch}")
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("checkout is dirty")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = Path(_git(repo, "rev-parse", "--git-path", name).stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            raise RuntimeError(f"unfinished Git operation: {name}")


def _wrapper(
    wrapper: Path, repo: Path, argument: str, timeout: float, *,
    maintenance_start: bool = False,
) -> None:
    env = None
    if maintenance_start:
        env = {**os.environ, "HERMES_MAINTENANCE_START_ALLOWED": "1"}
    result = _run((str(wrapper), argument), cwd=repo, timeout=timeout, env=env)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"wrapper {argument} failed{': ' + detail if detail else ''}")


def _health(health_script: Path, repo: Path, timeout: float) -> None:
    if not health_script.is_file():
        raise RuntimeError(f"health probe is missing: {health_script}")
    result = _run((sys.executable, str(health_script)), cwd=repo, timeout=timeout)
    if result.returncode:
        raise RuntimeError("wrapper health validation failed")


def _ensure_hindsight_embeddings(repo: Path, timeout: float) -> None:
    """Repair the known shared-venv HF downgrade, then prove imports work."""
    python = repo / "venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"Hermes venv Python is missing: {python}")
    version_probe = (str(python), "-c", HINDSIGHT_HUB_VERSION_PROBE)
    version_checked = _run(version_probe, cwd=repo, timeout=timeout)
    repairable_version_results = {
        HINDSIGHT_HUB_MISSING,
        HINDSIGHT_HUB_INCOMPATIBLE,
    }
    if (
        version_checked.returncode
        and version_checked.returncode not in repairable_version_results
    ):
        detail = version_checked.stderr.strip() or version_checked.stdout.strip()
        raise RuntimeError(
            "could not inspect huggingface-hub compatibility"
            + (f": {detail}" if detail else "")
        )
    if version_checked.returncode in repairable_version_results:
        repaired = _run(
            (
                str(python), "-m", "pip", "install", "--no-deps",
                HINDSIGHT_HUB_REQUIREMENT,
            ),
            cwd=repo,
            timeout=timeout,
        )
        if repaired.returncode:
            detail = repaired.stderr.strip() or repaired.stdout.strip()
            raise RuntimeError(
                "could not repair Hindsight embedding dependencies"
                + (f": {detail}" if detail else "")
            )
        version_verified = _run(version_probe, cwd=repo, timeout=timeout)
        if version_verified.returncode in repairable_version_results:
            raise RuntimeError("huggingface-hub remains outside >=1.5.0,<2.0")
        if version_verified.returncode:
            detail = version_verified.stderr.strip() or version_verified.stdout.strip()
            raise RuntimeError(
                "could not re-inspect huggingface-hub compatibility"
                + (f": {detail}" if detail else "")
            )

    import_probe = (str(python), "-c", HINDSIGHT_IMPORT_PROBE)
    imports_verified = _run(import_probe, cwd=repo, timeout=timeout)
    if imports_verified.returncode:
        detail = imports_verified.stderr.strip() or imports_verified.stdout.strip()
        raise RuntimeError(
            "Hindsight embedding imports fail"
            + (f": {detail}" if detail else "")
        )


def _create_drain_marker(marker: Path, request_id: str) -> int:
    """Create the external marker without adopting an existing request."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "action": "drain", "requested_at": _now(),
        "principal": "hermes-maintenance", "request_id": request_id,
        "suppress_notification": True,
    }
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BusyError(f"drain marker already exists: {marker}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(marker.parent)
    return marker.stat().st_mtime_ns


def _owned_marker(marker: Path, request_id: str) -> bool:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("request_id") == request_id


def _remove_owned_marker(marker: Path, request_id: str) -> bool:
    if not _owned_marker(marker, request_id):
        return False
    # Re-read immediately before unlink; replacement by a cooperating writer is
    # detected. Atomic ownership+unlink is not available for ordinary files.
    if not _owned_marker(marker, request_id):
        return False
    marker.unlink()
    _fsync_dir(marker.parent)
    return True


def _wait_for_drain(
    marker: Path, status: Path, request_id: str, marker_mtime_ns: int,
    *, timeout: float, interval: float, sample_interval: float,
    stable_samples: int,
) -> None:
    deadline = time.monotonic() + timeout
    stable: list[tuple[int, str]] = []
    while time.monotonic() < deadline:
        if not _owned_marker(marker, request_id):
            raise RuntimeError("drain marker ownership was lost")
        try:
            stat = status.stat()
            raw = status.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            stable.clear()
            time.sleep(interval)
            continue
        signature = (stat.st_mtime_ns, raw)
        fresh = stat.st_mtime_ns > marker_mtime_ns
        valid = (
            fresh and isinstance(payload, dict)
            and payload.get("gateway_state") == "draining"
            and type(payload.get("active_agents")) is int
            and payload.get("active_agents") == 0
        )
        if valid and (not stable or signature != stable[-1]):
            stable.append(signature)
            if len(stable) >= stable_samples:
                return
            time.sleep(sample_interval)
        else:
            if not valid:
                stable.clear()
            time.sleep(interval)
    raise RuntimeError(
        f"drain timed out without {stable_samples} fresh acknowledged stable samples"
    )


def _fetch_private(repo: Path, remote: str, upstream_branch: str, run_id: str) -> tuple[str, str]:
    ref = f"refs/hermes-maintenance/fetches/{run_id}/upstream"
    result = _git(
        repo, "fetch", "--no-write-fetch-head", "--refmap=", remote,
        f"refs/heads/{upstream_branch}:{ref}", check=False, timeout=300,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "upstream fetch failed")
    return ref, _oid(repo, ref)


def _check_merge(repo: Path, integration_oid: str, upstream_oid: str) -> None:
    result = _git(repo, "merge-tree", "--write-tree", integration_oid, upstream_oid, check=False)
    if result.returncode:
        raise RuntimeError("upstream conflicts with diatche; refs and checkout were not changed")


def _build_candidate(
    repo: Path, state_dir: Path, run_id: str, integration_oid: str, upstream_oid: str
) -> tuple[str, str]:
    candidate_ref = f"refs/hermes-maintenance/candidates/{run_id}"
    root = Path(tempfile.mkdtemp(prefix=f"candidate-{run_id}-", dir=state_dir))
    worktree = root / "worktree"
    added = False
    try:
        _git(repo, "worktree", "add", "--detach", str(worktree), integration_oid, timeout=120)
        added = True
        merged = _git(
            worktree, "-c", "user.name=Hermes Maintenance", "-c",
            "user.email=maintenance@localhost", "merge", "--no-ff", "--no-edit",
            upstream_oid, check=False, timeout=120,
        )
        if merged.returncode:
            raise RuntimeError("isolated candidate merge failed")
        candidate_oid = _oid(worktree, "HEAD")
        checked = _git(worktree, "diff", "--check", f"{integration_oid}..{candidate_oid}", check=False)
        if checked.returncode:
            raise RuntimeError("candidate failed git diff --check")
        created = _git(repo, "update-ref", candidate_ref, candidate_oid, ZERO_OID, check=False)
        if created.returncode:
            raise RuntimeError("candidate ref creation compare-and-swap failed")
        return candidate_ref, candidate_oid
    finally:
        if added:
            _git(repo, "worktree", "remove", "--force", str(worktree), check=False, timeout=60)
        shutil.rmtree(root, ignore_errors=True)


def _update_ref_transaction(repo: Path, commands: list[str]) -> None:
    body = "\n".join(["start", *commands, "prepare", "commit", ""])
    result = subprocess.run(
        ("git", "update-ref", "--stdin"), cwd=repo, input=body,
        text=True, capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            "ref compare-and-swap transaction failed: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def _publish_refs(
    repo: Path, *, main_old: str, main_new: str, integration_branch: str,
    integration_old: str, integration_new: str,
) -> None:
    _update_ref_transaction(repo, [
        f"update refs/heads/main {main_new} {main_old}",
        f"update refs/heads/{integration_branch} {integration_new} {integration_old}",
    ])


def _restore_refs(
    repo: Path, *, main_old: str, main_new: str, integration_branch: str,
    integration_old: str, integration_new: str,
) -> None:
    current_main = _oid(repo, "refs/heads/main")
    current_integration = _oid(repo, f"refs/heads/{integration_branch}")
    if current_main == main_old and current_integration == integration_old:
        return
    if current_main != main_new or current_integration != integration_new:
        raise RuntimeError("cannot recover after concurrent ref movement")
    _update_ref_transaction(repo, [
        f"update refs/heads/main {main_old} {main_new}",
        f"update refs/heads/{integration_branch} {integration_old} {integration_new}",
    ])


def _restore_checkout(repo: Path, integration_branch: str, expected_oid: str) -> None:
    switched = _git(repo, "switch", integration_branch, check=False)
    if switched.returncode:
        raise RuntimeError("could not restore integration checkout")
    reset = _git(repo, "reset", "--hard", expected_oid, check=False)
    if reset.returncode:
        raise RuntimeError("could not restore integration checkout files")
    _assert_checkout(repo, integration_branch)
    if _oid(repo, "HEAD") != expected_oid or _oid(repo, f"refs/heads/{integration_branch}") != expected_oid:
        raise RuntimeError("restored checkout identity does not match the expected OID")


def _journal_payload(
    repo: Path, run_id: str, main_old: str, integration_old: str,
    main_new: str, integration_new: str, phase: str, *, owner_pid: int | None = None,
) -> dict[str, Any]:
    return {
        "version": 2, "run_id": run_id, "repo": str(repo.resolve()),
        "common_git_dir": str(_common_git_dir(repo)), "integration_branch": "diatche",
        "main_ref": "refs/heads/main", "integration_ref": "refs/heads/diatche",
        "main_old": main_old, "integration_old": integration_old,
        "main_new": main_new, "integration_new": integration_new,
        "phase": phase, "owner_pid": os.getpid() if owner_pid is None else owner_pid,
        "updated_at": _now(),
    }


def _validate_journal(payload: Any, repo: Path, run_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise RuntimeError("malformed recovery journal")
    required = {
        "run_id", "repo", "common_git_dir", "integration_branch", "main_ref",
        "integration_ref", "main_old", "integration_old", "main_new",
        "integration_new", "phase", "owner_pid", "request_id", "drain_marker",
    }
    if not required.issubset(payload):
        raise RuntimeError("malformed recovery journal")
    if payload["run_id"] != run_id or payload["repo"] != str(repo.resolve()) or payload["common_git_dir"] != str(_common_git_dir(repo)):
        raise RuntimeError("foreign recovery journal")
    if payload["integration_branch"] != "diatche" or payload["main_ref"] != "refs/heads/main" or payload["integration_ref"] != "refs/heads/diatche":
        raise RuntimeError("foreign recovery journal refs")
    if not all(OID_RE.fullmatch(str(payload[key])) for key in ("main_old", "integration_old", "main_new", "integration_new")):
        raise RuntimeError("malformed recovery journal OID")
    if not RUN_ID_RE.fullmatch(str(payload["request_id"])):
        raise RuntimeError("malformed recovery journal request id")
    owner = payload["owner_pid"]
    if type(owner) is not int or owner <= 0:
        raise RuntimeError("malformed recovery journal owner")
    if owner != os.getpid():
        try:
            os.kill(owner, 0)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError("recovery journal has a current active owner") from exc
        else:
            raise RuntimeError("recovery journal has a current active owner")
    if payload["phase"] in TERMINAL_PHASES:
        raise RuntimeError("recovery journal is already terminal")
    return payload


def _journal_path(state_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeError("invalid run id")
    return state_dir / "runs" / f"{run_id}.json"


def _write_journal(state_dir: Path, payload: dict[str, Any]) -> None:
    _write_json(_journal_path(state_dir, str(payload["run_id"])), payload)
    _write_json(state_dir / "state.json", payload)


def _recover_payload(
    payload: dict[str, Any], repo: Path, wrapper: Path, health_script: Path,
    marker: Path, *, wrapper_timeout: float, health_timeout: float,
) -> None:
    if payload["drain_marker"] != str(marker.resolve()):
        raise RuntimeError("foreign recovery journal drain marker")
    request_id = str(payload["request_id"])
    if marker.exists() and not _owned_marker(marker, request_id):
        raise RuntimeError("drain marker is owned by another operation")

    refs_are_original = (
        _oid(repo, "refs/heads/main") == payload["main_old"]
        and _oid(repo, "refs/heads/diatche") == payload["integration_old"]
    )
    wrapper_healthy = False
    if refs_are_original:
        try:
            _assert_checkout(repo, "diatche")
            if _oid(repo, "HEAD") != payload["integration_old"]:
                raise RuntimeError("checkout is not at the original runtime")
            _wrapper(wrapper, repo, "--status", wrapper_timeout)
            _health(health_script, repo, health_timeout)
            wrapper_healthy = True
        except Exception:
            wrapper_healthy = False

    if not wrapper_healthy:
        _restore_refs(
            repo, main_old=payload["main_old"], main_new=payload["main_new"],
            integration_branch="diatche", integration_old=payload["integration_old"],
            integration_new=payload["integration_new"],
        )
        _restore_checkout(repo, "diatche", payload["integration_old"])
        _ensure_hindsight_embeddings(repo, health_timeout)
        _wrapper(
            wrapper, repo, "--foreground", wrapper_timeout, maintenance_start=True
        )
        _wrapper(wrapper, repo, "--status", wrapper_timeout)
        _health(health_script, repo, health_timeout)

    if marker.exists() and not _remove_owned_marker(marker, request_id):
        raise RuntimeError("could not remove owned drain marker after recovery")


def _run_maintenance(args: argparse.Namespace) -> int:
    repo, state_dir = args.repo.resolve(), args.state_dir.resolve()
    run_id = uuid.uuid4().hex
    request_id = uuid.uuid4().hex
    marker = args.drain_marker.resolve()
    journal: dict[str, Any] | None = None
    marker_owned = False
    wrapper_stopped = False
    wrapper_started = False
    published = False
    try:
        with _signal_guard(), _lock(state_dir):
            _assert_checkout(repo, "diatche")
            _wrapper(args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout)
            main_old = _oid(repo, "refs/heads/main")
            integration_old = _oid(repo, "refs/heads/diatche")
            fetch_ref, upstream_oid = _fetch_private(repo, args.remote, args.upstream_branch, run_id)
            _check_merge(repo, integration_old, upstream_oid)
            candidate_ref, candidate_oid = _build_candidate(
                repo, state_dir, run_id, integration_old, upstream_oid
            )
            journal = _journal_payload(
                repo, run_id, main_old, integration_old, upstream_oid,
                candidate_oid, "prepared",
            )
            journal.update(
                fetch_ref=fetch_ref,
                candidate_ref=candidate_ref,
                request_id=request_id,
                drain_marker=str(marker),
            )
            _write_journal(state_dir, journal)
            journal["phase"] = "stopping"; journal["updated_at"] = _now(); _write_journal(state_dir, journal)
            # Treat a stop attempt as potentially destructive even when the
            # controller returns non-zero: it may have partially unloaded the
            # supervisor. Recovery must therefore reassert the old wrapper.
            wrapper_stopped = True
            _wrapper(args.wrapperctl.resolve(), repo, "--force-stop", args.wrapper_timeout)
            journal["phase"] = "stopped"; journal["updated_at"] = _now(); _write_journal(state_dir, journal)
            _publish_refs(
                repo, main_old=main_old, main_new=upstream_oid,
                integration_branch="diatche", integration_old=integration_old,
                integration_new=candidate_oid,
            )
            published = True
            journal["phase"] = "published"; journal["updated_at"] = _now(); _write_journal(state_dir, journal)
            _restore_checkout(repo, "diatche", candidate_oid)
            _ensure_hindsight_embeddings(repo, args.health_timeout)
            _wrapper(
                args.wrapperctl.resolve(), repo, "--foreground",
                args.wrapper_timeout, maintenance_start=True,
            )
            wrapper_started = True
            _wrapper(args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout)
            _health(args.health_script.resolve(), repo, args.health_timeout)
            wrapper_stopped = False
            _assert_checkout(repo, "diatche")
            if _oid(repo, "HEAD") != candidate_oid:
                raise RuntimeError("runtime checkout moved during startup")
            journal.update(phase="complete", upstream_sha=upstream_oid, completed_at=_now(), updated_at=_now())
            _write_journal(state_dir, journal)
            print(f"Hermes maintenance complete at {candidate_oid[:12]}")
            return 0
    except Exception as exc:
        recovery_error = ""
        if marker_owned:
            _remove_owned_marker(marker, request_id)
        if journal is not None and (wrapper_stopped or published):
            try:
                if wrapper_started:
                    _wrapper(args.wrapperctl.resolve(), repo, "--force-stop", args.wrapper_timeout)
                if published:
                    _restore_refs(
                        repo, main_old=journal["main_old"], main_new=journal["main_new"],
                        integration_branch="diatche", integration_old=journal["integration_old"],
                        integration_new=journal["integration_new"],
                    )
                _restore_checkout(repo, "diatche", journal["integration_old"])
                _ensure_hindsight_embeddings(repo, args.health_timeout)
                _wrapper(
                    args.wrapperctl.resolve(), repo, "--foreground",
                    args.wrapper_timeout, maintenance_start=True,
                )
                _wrapper(args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout)
                _health(args.health_script.resolve(), repo, args.health_timeout)
                wrapper_stopped = False
                journal["recovered"] = True
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
        if journal is not None:
            journal.update(phase="failed", error=str(exc), recovery_error=recovery_error, updated_at=_now())
            _write_journal(state_dir, journal)
        print(f"ERROR: {exc}", file=sys.stderr)
        if recovery_error:
            print(f"RECOVERY ERROR: {recovery_error}", file=sys.stderr)
        return 1


def _recover(args: argparse.Namespace) -> int:
    repo, state_dir = args.repo.resolve(), args.state_dir.resolve()
    try:
        with _lock(state_dir):
            run_id = args.run_id
            if not run_id:
                candidates: list[str] = []
                for path in (state_dir / "runs").glob("*.json"):
                    if not RUN_ID_RE.fullmatch(path.stem):
                        continue
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict) and payload.get("phase") not in TERMINAL_PHASES:
                        candidates.append(path.stem)
                if len(candidates) != 1:
                    raise RuntimeError("--recover requires --run-id when there is not exactly one interrupted run")
                run_id = candidates[0]
            path = _journal_path(state_dir, run_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("malformed or missing recovery journal") from exc
            payload = _validate_journal(raw, repo, run_id)
            _recover_payload(
                payload, repo, args.wrapperctl.resolve(), args.health_script.resolve(),
                wrapper_timeout=args.wrapper_timeout, health_timeout=args.health_timeout,
            )
            payload.update(phase="recovered", recovered_at=_now(), updated_at=_now(), owner_pid=os.getpid())
            _write_journal(state_dir, payload)
            print(f"Recovered maintenance run {run_id}")
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _check(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    try:
        _assert_checkout(repo, "diatche")
        integration = _oid(repo, "refs/heads/diatche")
        if args.no_fetch:
            upstream = _oid(repo, f"refs/heads/{args.upstream_branch}")
            fetch_ref = ""
        else:
            run_id = "check-" + uuid.uuid4().hex
            fetch_ref, upstream = _fetch_private(repo, args.remote, args.upstream_branch, run_id)
        _check_merge(repo, integration, upstream)
        payload = {"ok": True, "mergeable": True, "integration_sha": integration,
                   "upstream_sha": upstream, "fetch_ref": fetch_ref}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else "OK: mergeable")
        return 0
    except Exception as exc:
        payload = {"ok": False, "mergeable": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1


def _status(args: argparse.Namespace) -> int:
    state = args.state_dir.resolve() / "state.json"
    if state.is_file():
        print(state.read_text(encoding="utf-8").rstrip())
    else:
        print("No maintenance run has been recorded.")
    return 0


def _detach(args: argparse.Namespace) -> int:
    """Queue the already-loaded one-shot LaunchAgent without running inline."""
    state_dir = args.state_dir.resolve()
    domain = f"gui/{os.getuid()}/{MAINTENANCE_LABEL}"
    try:
        with _lock(state_dir):
            loaded = _run(("launchctl", "print", domain), cwd=args.repo.resolve())
            if loaded.returncode:
                raise RuntimeError("maintenance LaunchAgent is not loaded")
            _write_json(
                state_dir / "state.json",
                {"phase": "queued", "queued_at": _now(), "repo": str(args.repo.resolve())},
            )
            started = _run(("launchctl", "kickstart", domain), cwd=args.repo.resolve())
            if started.returncode:
                raise RuntimeError("could not start maintenance LaunchAgent")
        print("Hermes maintenance update queued; active sessions will be interrupted.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--wrapperctl", type=Path, default=DEFAULT_WRAPPERCTL)
    parser.add_argument("--health-script", type=Path, default=DEFAULT_HEALTH_SCRIPT)
    parser.add_argument("--drain-marker", type=Path, default=DEFAULT_DRAIN_MARKER)
    parser.add_argument("--gateway-status", type=Path, default=DEFAULT_GATEWAY_STATUS)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--drain-timeout", type=float, default=300)
    parser.add_argument("--drain-interval", type=float, default=.25)
    parser.add_argument("--sample-interval", type=float, default=.5)
    parser.add_argument("--stable-drain-samples", type=int, default=3)
    parser.add_argument("--wrapper-timeout", type=float, default=60)
    parser.add_argument("--health-timeout", type=float, default=120)
    parser.add_argument("--run-id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--recover", action="store_true")
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--detach", action="store_true")
    modes.add_argument("--install", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    args.repo = repo
    if not _git(repo, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true":
        print(f"ERROR: not a Git checkout: {repo}", file=sys.stderr)
        return 2
    if args.install:
        print("Installation is unsupported by the narrow maintenance command (no-op).")
        return 0
    if args.detach:
        return _detach(args)
    if args.status:
        return _status(args)
    if args.check:
        return _check(args)
    if args.recover:
        return _recover(args)
    return _run_maintenance(args)


if __name__ == "__main__":
    raise SystemExit(main())
