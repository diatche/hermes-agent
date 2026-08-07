#!/usr/bin/env python3
"""Coordinate Pavel's guarded three-step Hermes maintenance update.

``--pre`` (also the no-argument default) prepares and validates an immutable
merge candidate, stops the custom gateway wrapper, and prints the exact
official update and ``--post`` commands. The updater then runs directly in the
operator's terminal. ``--post`` verifies its pinned Git result, publishes
``diatche``, restores the checkout, and restarts and health-checks the wrapper.
Run ``--post`` even if the official update fails so the previous runtime can be
recovered. ``--check`` fetches live upstream into a private maintenance ref and
performs a no-checkout merge preflight without moving branch or tracking refs.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_REPO = Path.home() / ".hermes" / "hermes-agent"
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "local" / "update"
DEFAULT_WRAPPERCTL = Path.home() / ".hermes" / "local" / "bin" / "hermes-gateway-wrapperctl"
DEFAULT_HEALTH_SCRIPT = Path.home() / ".hermes" / "local" / "health" / "hermes_core_health.py"

ZERO_OID = "0" * 40
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
OID_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_PHASES = {"complete", "recovered"}
HINDSIGHT_HUB_VERSION = "1.24.0"
HINDSIGHT_HUB_REQUIREMENT = f"huggingface-hub=={HINDSIGHT_HUB_VERSION}"
HINDSIGHT_HUB_MISSING = 10
HINDSIGHT_HUB_INCOMPATIBLE = 11
HINDSIGHT_HUB_VERSION_PROBE = f"""\
from importlib.metadata import PackageNotFoundError, version
from packaging.specifiers import SpecifierSet
try:
    installed = version("huggingface-hub")
except PackageNotFoundError:
    raise SystemExit(10)
raise SystemExit(
    0 if SpecifierSet("=={HINDSIGHT_HUB_VERSION}").contains(installed, prereleases=True) else 11
)
"""
HINDSIGHT_IMPORT_PROBE = """\
import hindsight_embed.daemon_embed_manager
import huggingface_hub
import sentence_transformers
import transformers
"""
OFFICIAL_UPDATE_ARGUMENTS = (
    "update", "--branch", "main", "--no-backup", "--yes", "--no-gateway-restart"
)


class BusyError(RuntimeError):
    pass


class MaintenanceInterruptedError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress(message: str) -> None:
    """Emit immediate operator feedback without polluting structured stdout."""
    print(f"==> {message}", file=sys.stderr, flush=True)


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
            raise RuntimeError(
                f"huggingface-hub remains outside the required exact version "
                f"{HINDSIGHT_HUB_VERSION}"
            )
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
    # merge-tree may leave harmless unreachable temporary objects, but it does
    # not move refs or alter the checkout. Avoid a second disposable repository
    # merely to isolate garbage that normal Git maintenance can reclaim.
    result = _git(
        repo, "merge-tree", "--write-tree", "--name-only",
        integration_oid, upstream_oid,
        check=False,
    )
    if result.returncode:
        first_block = result.stdout.split("\n\n", 1)[0].splitlines()
        conflicting_files = first_block[1:] if len(first_block) > 1 else []
        files = "\n".join(f"  - {path}" for path in conflicting_files)
        if not files:
            files = "  - Git did not report the conflicting paths"
        raise RuntimeError(
            f"upstream commit {upstream_oid} does not merge cleanly into "
            f"diatche commit {integration_oid}.\n"
            f"Conflicting files:\n{files}\n"
            "Next: resolve these conflicts in an isolated integration "
            "branch/worktree, validate the result, then rerun "
            "hermes-maintenance-update --check.\n"
            "No refs or checkout files were changed."
        )


def _assert_updater_has_forward_transition(main_oid: str, upstream_oid: str) -> None:
    """Require the official updater to retain ownership of advancing main."""
    if main_oid == upstream_oid:
        raise RuntimeError(
            "local main already equals the pinned upstream commit; the official "
            "updater's full synchronization path cannot be assumed. Refusing "
            "normal maintenance; controlled recovery is required"
        )


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


def _publish_integration(
    repo: Path, *, main_oid: str, origin_main_oid: str, upstream_ref: str,
    upstream_oid: str, integration_branch: str, integration_old: str,
    integration_new: str,
) -> None:
    """CAS only the local integration ref while verifying updater-owned refs."""
    _update_ref_transaction(repo, [
        f"verify refs/heads/main {main_oid}",
        f"verify refs/remotes/origin/main {origin_main_oid}",
        f"verify {upstream_ref} {upstream_oid}",
        f"update refs/heads/{integration_branch} {integration_new} {integration_old}",
    ])


def _assert_official_update_result(
    repo: Path, *, upstream_ref: str, upstream_oid: str, integration_old: str,
) -> None:
    expected = (
        ("refs/heads/main", upstream_oid),
        ("refs/remotes/origin/main", upstream_oid),
        (upstream_ref, upstream_oid),
        ("refs/heads/diatche", integration_old),
    )
    for ref, oid in expected:
        if _oid(repo, ref) != oid:
            raise RuntimeError(f"official updater left unexpected Git state: {ref}")
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    expected_head = {"main": upstream_oid, "diatche": integration_old}.get(branch)
    if expected_head is None:
        raise RuntimeError("official updater left the checkout on an unexpected branch")
    if _oid(repo, "HEAD") != expected_head:
        raise RuntimeError("official updater left unexpected Git state: HEAD")
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("official updater left a dirty checkout")


def _assert_pre_stop_snapshot(
    repo: Path, *, main_oid: str, integration_oid: str, upstream_ref: str,
    upstream_oid: str,
) -> None:
    _assert_checkout(repo, "diatche")
    expected = (
        ("refs/heads/main", main_oid),
        ("refs/heads/diatche", integration_oid),
        (upstream_ref, upstream_oid),
    )
    for ref, oid in expected:
        if _oid(repo, ref) != oid:
            raise RuntimeError(f"Git snapshot moved before stop: {ref}")


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


def _assert_transaction_checkout(
    repo: Path, *, integration_old: str, integration_new: str,
) -> None:
    if _git(repo, "branch", "--show-current").stdout.strip() != "diatche":
        raise RuntimeError("cannot recover after concurrent checkout movement")
    if _oid(repo, "HEAD") != integration_new:
        raise RuntimeError("cannot recover after concurrent checkout movement")
    if _git(repo, "diff", "--quiet", check=False).returncode:
        raise RuntimeError("cannot recover after concurrent checkout changes")
    if _git(repo, "ls-files", "--others", "--exclude-standard").stdout.strip():
        raise RuntimeError("cannot recover after concurrent checkout changes")
    index_is_old = not _git(
        repo, "diff", "--cached", "--quiet", integration_old, check=False
    ).returncode
    index_is_new = not _git(
        repo, "diff", "--cached", "--quiet", integration_new, check=False
    ).returncode
    if not (index_is_old or index_is_new):
        raise RuntimeError("cannot recover after concurrent index changes")


def _recover_owned_git_state(repo: Path, payload: dict[str, Any]) -> None:
    """Restore only Git states provably produced by this orchestration run."""
    main_old = str(payload["main_old"])
    upstream = str(payload["main_new"])
    integration_old = str(payload["integration_old"])
    integration_new = str(payload["integration_new"])
    origin_ref = str(payload.get("origin_ref", "refs/remotes/origin/main"))
    origin_old = payload.get("origin_old")
    fetch_ref = payload.get("fetch_ref")

    current_main = _oid(repo, "refs/heads/main")
    current_integration = _oid(repo, "refs/heads/diatche")
    if current_main not in {main_old, upstream}:
        raise RuntimeError("cannot recover after concurrent main movement")
    if current_integration not in {integration_old, integration_new}:
        raise RuntimeError("cannot recover after concurrent diatche movement")
    current_origin = _oid(repo, origin_ref) if origin_old is not None else None
    if origin_old is not None and current_origin not in {str(origin_old), upstream}:
        raise RuntimeError("cannot recover after concurrent origin/main movement")
    if fetch_ref is not None and _oid(repo, str(fetch_ref)) != upstream:
        raise RuntimeError("cannot recover after private fetch ref movement")

    branch = _git(repo, "branch", "--show-current").stdout.strip()
    head = _oid(repo, "HEAD")
    expected_head = current_main if branch == "main" else current_integration
    if branch not in {"main", "diatche"} or head != expected_head:
        raise RuntimeError("cannot recover after concurrent checkout movement")
    if _git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("cannot recover after concurrent checkout changes")

    commands: list[str] = []
    if fetch_ref is not None:
        commands.append(f"verify {fetch_ref} {upstream}")
    if current_main != main_old:
        commands.append(f"update refs/heads/main {main_old} {current_main}")
    else:
        commands.append(f"verify refs/heads/main {main_old}")
    if origin_old is not None:
        if current_origin != str(origin_old):
            commands.append(f"update {origin_ref} {origin_old} {current_origin}")
        else:
            commands.append(f"verify {origin_ref} {origin_old}")
    if current_integration != integration_old:
        commands.append(
            f"update refs/heads/diatche {integration_old} {current_integration}"
        )
    else:
        commands.append(f"verify refs/heads/diatche {integration_old}")
    _update_ref_transaction(repo, commands)
    _restore_checkout(repo, "diatche", integration_old)


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
        "integration_new", "phase", "owner_pid",
    }
    if not required.issubset(payload):
        raise RuntimeError("malformed recovery journal")
    if payload["run_id"] != run_id or payload["repo"] != str(repo.resolve()) or payload["common_git_dir"] != str(_common_git_dir(repo)):
        raise RuntimeError("foreign recovery journal")
    if payload["integration_branch"] != "diatche" or payload["main_ref"] != "refs/heads/main" or payload["integration_ref"] != "refs/heads/diatche":
        raise RuntimeError("foreign recovery journal refs")
    if not all(OID_RE.fullmatch(str(payload[key])) for key in ("main_old", "integration_old", "main_new", "integration_new")):
        raise RuntimeError("malformed recovery journal OID")

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
    *, wrapper_timeout: float, health_timeout: float,
) -> None:
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
        # A hard crash may have left a candidate runtime alive. Quiesce it
        # before changing refs or checkout files underneath that process.
        _wrapper(wrapper, repo, "--force-stop", wrapper_timeout)
        if "origin_old" in payload:
            _recover_owned_git_state(repo, payload)
        elif refs_are_original:
            _assert_checkout(repo, "diatche")
            if _oid(repo, "HEAD") != payload["integration_old"]:
                raise RuntimeError("cannot recover after concurrent checkout movement")
        else:
            _assert_transaction_checkout(
                repo,
                integration_old=payload["integration_old"],
                integration_new=payload["integration_new"],
            )
            _restore_refs(
                repo, main_old=payload["main_old"], main_new=payload["main_new"],
                integration_branch="diatche",
                integration_old=payload["integration_old"],
                integration_new=payload["integration_new"],
            )
            _restore_checkout(repo, "diatche", payload["integration_old"])
        _ensure_hindsight_embeddings(repo, health_timeout)
        _wrapper(
            wrapper, repo, "--foreground", wrapper_timeout, maintenance_start=True
        )
        _wrapper(wrapper, repo, "--status", wrapper_timeout)
        _health(health_script, repo, health_timeout)

def _run_pre_locked(
    args: argparse.Namespace, repo: Path, state_dir: Path, run_id: str,
) -> int:
    journal: dict[str, Any] | None = None
    wrapper_stopped = False
    try:
        _progress("Checking for interrupted maintenance to recover")
        _recover_interrupted(args, repo, state_dir)
        _progress("Validating the diatche checkout and gateway wrapper")
        _assert_checkout(repo, "diatche")
        _wrapper(args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout)
        main_old = _oid(repo, "refs/heads/main")
        origin_ref = f"refs/remotes/{args.remote}/{args.upstream_branch}"
        origin_old = _oid(repo, origin_ref)
        integration_old = _oid(repo, "refs/heads/diatche")
        _progress(f"Fetching {args.remote}/{args.upstream_branch}")
        fetch_ref, upstream_oid = _fetch_private(
            repo, args.remote, args.upstream_branch, run_id
        )
        _assert_updater_has_forward_transition(main_old, upstream_oid)
        _progress("Checking mergeability without changing refs or checkout")
        _check_merge(repo, integration_old, upstream_oid)
        _progress("Building and validating the isolated merge candidate")
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
            origin_ref=origin_ref,
            origin_old=origin_old,
            rollback_scope=(
                "Git refs and checkout only; official updater dependency, asset, "
                "skill, config, cache, and backup changes are not rolled back"
            ),
        )
        _write_journal(state_dir, journal)
        updater = getattr(args, "updater_executable", None)
        updater = updater.resolve() if updater else repo / "venv" / "bin" / "hermes"
        if not updater.is_file() or not os.access(updater, os.X_OK):
            raise RuntimeError(f"official Hermes updater is missing: {updater}")
        _assert_pre_stop_snapshot(
            repo,
            main_oid=main_old,
            integration_oid=integration_old,
            upstream_ref=fetch_ref,
            upstream_oid=upstream_oid,
        )
        journal.update(phase="stopping", updated_at=_now())
        _write_journal(state_dir, journal)
        # A failed stop may still have partially unloaded the supervisor.
        wrapper_stopped = True
        _progress("Stopping the custom Hermes gateway wrapper")
        _wrapper(args.wrapperctl.resolve(), repo, "--force-stop", args.wrapper_timeout)
        journal.update(phase="awaiting-official-update", updated_at=_now())
        _write_journal(state_dir, journal)
        update_command = " ".join(
            shlex.quote(str(part)) for part in (updater, *OFFICIAL_UPDATE_ARGUMENTS)
        )
        post_command = Path(sys.argv[0]).resolve()
        print()
        print("Pre-update checks passed and the gateway is stopped.")
        print("Run these commands in order:")
        print()
        print(f"  cd {shlex.quote(str(repo))} && {update_command}")
        print(f"  {shlex.quote(str(post_command))} --post")
        print()
        print("Run the post command even if the official update fails or is interrupted.")
        return 0
    except Exception as exc:
        recovery_error = ""
        if journal is not None and wrapper_stopped:
            try:
                _progress("Pre-update failed after gateway interruption; recovering the previous runtime")
                _recover_owned_git_state(repo, journal)
                _ensure_hindsight_embeddings(repo, args.health_timeout)
                _wrapper(
                    args.wrapperctl.resolve(), repo, "--foreground",
                    args.wrapper_timeout, maintenance_start=True,
                )
                _wrapper(
                    args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout
                )
                _health(args.health_script.resolve(), repo, args.health_timeout)
                wrapper_stopped = False
                journal["recovered"] = True
                _progress("Previous Hermes runtime recovered and healthy")
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
        if journal is not None:
            journal.update(
                phase="failed", error=str(exc), recovery_error=recovery_error,
                updated_at=_now(),
            )
            _write_journal(state_dir, journal)
        print(f"ERROR: {exc}", file=sys.stderr)
        if recovery_error:
            print(f"RECOVERY ERROR: {recovery_error}", file=sys.stderr)
        return 1


def _run_pre(args: argparse.Namespace) -> int:
    repo, state_dir = args.repo.resolve(), args.state_dir.resolve()
    with _signal_guard(), _lock(state_dir):
        return _run_pre_locked(args, repo, state_dir, uuid.uuid4().hex)


def _load_post_handoff(repo: Path, state_dir: Path) -> dict[str, Any]:
    state = state_dir / "state.json"
    try:
        raw = json.loads(state.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("no prepared maintenance handoff; run --pre first") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("malformed current maintenance journal") from exc
    if not isinstance(raw, dict) or raw.get("phase") != "awaiting-official-update":
        raise RuntimeError("no prepared maintenance handoff; run --pre first")
    run_id = str(raw.get("run_id", ""))
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeError("malformed current maintenance journal")
    return _validate_journal(raw, repo, run_id)


def _run_post_locked(
    args: argparse.Namespace, repo: Path, state_dir: Path,
) -> int:
    journal: dict[str, Any] | None = None
    wrapper_started = False
    try:
        _progress("Loading the prepared maintenance handoff")
        journal = _load_post_handoff(repo, state_dir)
        upstream_oid = str(journal["main_new"])
        integration_old = str(journal["integration_old"])
        candidate_oid = str(journal["integration_new"])
        fetch_ref = str(journal["fetch_ref"])
        _progress("Verifying the official Hermes update result")
        _assert_official_update_result(
            repo,
            upstream_ref=fetch_ref,
            upstream_oid=upstream_oid,
            integration_old=integration_old,
        )
        journal.update(
            phase="official-updated", owner_pid=os.getpid(), updated_at=_now()
        )
        _write_journal(state_dir, journal)
        _progress("Publishing the validated diatche ref atomically")
        _publish_integration(
            repo,
            main_oid=upstream_oid,
            origin_main_oid=upstream_oid,
            upstream_ref=fetch_ref,
            upstream_oid=upstream_oid,
            integration_branch="diatche",
            integration_old=integration_old,
            integration_new=candidate_oid,
        )
        journal.update(phase="published", updated_at=_now())
        _write_journal(state_dir, journal)
        _progress("Restoring the validated diatche checkout")
        _restore_checkout(repo, "diatche", candidate_oid)
        _progress("Checking Hindsight embedding compatibility")
        _ensure_hindsight_embeddings(repo, args.health_timeout)
        _progress("Starting the custom Hermes gateway wrapper")
        _wrapper(
            args.wrapperctl.resolve(), repo, "--foreground",
            args.wrapper_timeout, maintenance_start=True,
        )
        wrapper_started = True
        _progress("Verifying wrapper ownership and live Hermes health")
        _wrapper(args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout)
        _health(args.health_script.resolve(), repo, args.health_timeout)
        _assert_checkout(repo, "diatche")
        if _oid(repo, "HEAD") != candidate_oid:
            raise RuntimeError("runtime checkout moved during startup")
        journal.update(
            phase="complete", upstream_sha=upstream_oid,
            completed_at=_now(), updated_at=_now(),
        )
        _write_journal(state_dir, journal)
        print(f"Hermes maintenance complete at {candidate_oid[:12]}")
        return 0
    except Exception as exc:
        recovery_error = ""
        if journal is not None:
            try:
                _progress("Post-update verification failed; recovering the previous runtime")
                if wrapper_started:
                    _wrapper(
                        args.wrapperctl.resolve(), repo, "--force-stop",
                        args.wrapper_timeout,
                    )
                _recover_owned_git_state(repo, journal)
                _ensure_hindsight_embeddings(repo, args.health_timeout)
                _wrapper(
                    args.wrapperctl.resolve(), repo, "--foreground",
                    args.wrapper_timeout, maintenance_start=True,
                )
                _wrapper(
                    args.wrapperctl.resolve(), repo, "--status", args.wrapper_timeout
                )
                _health(args.health_script.resolve(), repo, args.health_timeout)
                journal["recovered"] = True
                _progress("Previous Hermes runtime recovered and healthy")
            except Exception as recovery_exc:
                recovery_error = str(recovery_exc)
        if journal is not None:
            journal.update(
                phase="failed", error=str(exc), recovery_error=recovery_error,
                updated_at=_now(),
            )
            _write_journal(state_dir, journal)
        print(f"ERROR: {exc}", file=sys.stderr)
        if recovery_error:
            print(f"RECOVERY ERROR: {recovery_error}", file=sys.stderr)
        return 1


def _run_post(args: argparse.Namespace) -> int:
    repo, state_dir = args.repo.resolve(), args.state_dir.resolve()
    with _signal_guard(), _lock(state_dir):
        return _run_post_locked(args, repo, state_dir)


def _recover_interrupted(
    args: argparse.Namespace, repo: Path, state_dir: Path
) -> None:
    """Recover only the latest state pointer; run files are historical records."""
    state = state_dir / "state.json"
    if not state.exists():
        return
    try:
        raw = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("malformed current maintenance journal") from exc
    terminal = isinstance(raw, dict) and (
        raw.get("phase") in TERMINAL_PHASES
        or (raw.get("phase") == "failed" and raw.get("recovered") is True)
    )
    if terminal:
        return
    if not isinstance(raw, dict) or not RUN_ID_RE.fullmatch(str(raw.get("run_id", ""))):
        raise RuntimeError("malformed current maintenance journal")
    run_id = str(raw["run_id"])
    payload = _validate_journal(raw, repo, run_id)
    _recover_payload(
        payload,
        repo,
        args.wrapperctl.resolve(),
        args.health_script.resolve(),
        wrapper_timeout=args.wrapper_timeout,
        health_timeout=args.health_timeout,
    )
    payload.update(
        phase="recovered",
        recovered_at=_now(),
        updated_at=_now(),
        owner_pid=os.getpid(),
    )
    _write_journal(state_dir, payload)
    print(f"Recovered interrupted maintenance run {run_id}")


def _check(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    try:
        _assert_checkout(repo, "diatche")
        integration = _oid(repo, "refs/heads/diatche")
        fetch_ref, upstream = _fetch_private(
            repo, args.remote, args.upstream_branch, f"check-{uuid.uuid4().hex}"
        )
        _check_merge(repo, integration, upstream)
        payload = {"ok": True, "mergeable": True, "integration_sha": integration,
                   "upstream_sha": upstream, "fetch_ref": fetch_ref}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "OK: upstream merges cleanly into diatche.\n"
                f"Checked upstream: {args.remote}/{args.upstream_branch} @ {upstream}\n"
                f"Against diatche: {integration}\n"
                "Next: run hermes-maintenance-update when ready."
            )
        return 0
    except Exception as exc:
        payload = {"ok": False, "mergeable": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    hidden = argparse.SUPPRESS
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument(
        "--pre",
        action="store_true",
        help="prepare the update, stop Hermes, and print the next commands",
    )
    phase.add_argument(
        "--post",
        action="store_true",
        help="verify the official update, publish diatche, and restart Hermes",
    )
    phase.add_argument(
        "--check",
        action="store_true",
        help="fetch live upstream privately and check checkout cleanliness and mergeability",
    )
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help=hidden)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help=hidden)
    parser.add_argument("--wrapperctl", type=Path, default=DEFAULT_WRAPPERCTL, help=hidden)
    parser.add_argument("--health-script", type=Path, default=DEFAULT_HEALTH_SCRIPT, help=hidden)
    parser.add_argument("--updater-executable", type=Path, default=None, help=hidden)
    parser.add_argument("--remote", default="origin", help=hidden)
    parser.add_argument("--upstream-branch", default="main", help=hidden)
    parser.add_argument("--wrapper-timeout", type=float, default=60, help=hidden)
    parser.add_argument("--health-timeout", type=float, default=120, help=hidden)
    parser.add_argument("--json", action="store_true", help=hidden)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    args.repo = repo
    if not _git(repo, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true":
        print(f"ERROR: not a Git checkout: {repo}", file=sys.stderr)
        return 2
    if args.check:
        return _check(args)
    if args.post:
        return _run_post(args)
    return _run_pre(args)


if __name__ == "__main__":
    raise SystemExit(main())
