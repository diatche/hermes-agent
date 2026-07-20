"""Behavior tests for Pavel's guarded Hermes maintenance-update wrapper."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes-maintenance-update.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("hermes_maintenance_update", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(path: Path, relative: str, text: str) -> None:
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=Maintenance Test",
        "-c",
        "user.email=maintenance@example.test",
        "commit",
        "-m",
        message,
    )
    return _git(path, "rev-parse", "HEAD")


def _make_remote_with_local_integration(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _write(seed, "base.txt", "base\n")
    base_sha = _commit(seed, "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(checkout))

    _git(checkout, "switch", "-c", "diatche")
    _write(checkout, "local.txt", "local patch\n")
    _commit(checkout, "local patch")
    _git(checkout, "switch", "main")

    _write(seed, "upstream.txt", "upstream\n")
    upstream_sha = _commit(seed, "upstream")
    _git(seed, "push", "origin", "main")
    _git(checkout, "fetch", "origin", "main")
    _git(checkout, "switch", "diatche")
    return checkout, base_sha, upstream_sha


def _run_script(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _make_fake_runtime(repo: Path, tmp_path: Path, *, healthy: bool = True) -> Path:
    python = repo / "venv" / "bin" / "python"
    hindsight = repo / "venv" / "bin" / "hindsight-embed"
    health = tmp_path / "health.py"
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\nvenv/\n", encoding="utf-8")
    _write(
        repo,
        "venv/bin/python",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ ${1:-} == *.py ]]; then\n"
        f"  printf '%s\\n' '{{\"healthy\": {str(healthy).lower()}}}'\n"
        f"  exit {0 if healthy else 1}\n"
        "fi\n"
        "exit 0\n",
    )
    _write(repo, "venv/bin/hindsight-embed", "#!/usr/bin/env bash\nexit 0\n")
    python.chmod(0o755)
    hindsight.chmod(0o755)
    health.write_text("# fake health script\n", encoding="utf-8")
    return health


def test_check_reports_clean_merge_without_moving_refs(tmp_path: Path) -> None:
    repo, _, upstream_sha = _make_remote_with_local_integration(tmp_path)
    before_main = _git(repo, "rev-parse", "main")
    before_diatche = _git(repo, "rev-parse", "diatche")

    result = _run_script(repo, "--check", "--no-fetch", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mergeable"] is True
    assert payload["upstream_sha"] == upstream_sha
    assert _git(repo, "rev-parse", "main") == before_main
    assert _git(repo, "rev-parse", "diatche") == before_diatche
    assert _git(repo, "status", "--porcelain") == ""


def test_check_reports_conflict_without_moving_refs_or_worktree(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    _write(repo, "base.txt", "local conflict\n")
    _commit(repo, "local conflicting edit")
    before_main = _git(repo, "rev-parse", "main")
    before_diatche = _git(repo, "rev-parse", "diatche")

    seed = tmp_path / "seed"
    _write(seed, "base.txt", "upstream conflict\n")
    _commit(seed, "upstream conflicting edit")
    _git(seed, "push", "origin", "main")
    _git(repo, "fetch", "origin", "main")

    result = _run_script(repo, "--check", "--no-fetch", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["mergeable"] is False
    assert _git(repo, "rev-parse", "main") == before_main
    assert _git(repo, "rev-parse", "diatche") == before_diatche
    assert _git(repo, "status", "--porcelain") == ""


def test_promotion_transaction_refuses_main_movement_without_moving_diatche(tmp_path: Path) -> None:
    repo, _, upstream_sha = _make_remote_with_local_integration(tmp_path)
    module = _load_script_module()
    integration_before = _git(repo, "rev-parse", "diatche")
    candidate_ref = "refs/hermes-maintenance/candidates/test/post-update"
    _git(repo, "update-ref", candidate_ref, integration_before)
    # Deliberately leave local main at its older value while origin/main is current.
    try:
        module._promote_candidate(
            repo,
            integration_branch="diatche",
            integration_before=integration_before,
            upstream_branch="main",
            upstream_ref="origin/main",
            upstream_sha=upstream_sha,
            candidate_ref=candidate_ref,
            candidate_sha=integration_before,
        )
    except RuntimeError as exc:
        assert "atomically verify refs" in str(exc)
    else:
        raise AssertionError("promotion unexpectedly ignored main movement")
    assert _git(repo, "rev-parse", "diatche") == integration_before


def test_live_health_rejects_malformed_output_and_propagates_timeout(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    health = _make_fake_runtime(repo, tmp_path)
    module = _load_script_module()
    original_run = module._run
    try:
        setattr(
            module,
            "_run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="not-json\n",
                stderr="",
            ),
        )
        try:
            module._validate_live_health(repo, health)
        except RuntimeError as exc:
            assert "return JSON" in str(exc)
        else:
            raise AssertionError("malformed live health output was accepted")

        def timed_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="health", timeout=60)

        setattr(module, "_run", timed_out)
        try:
            module._validate_live_health(repo, health)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("live health timeout was swallowed")
    finally:
        setattr(module, "_run", original_run)


def test_wrapper_restart_timeout_attempts_cleanup_stop(tmp_path: Path) -> None:
    module = _load_script_module()
    calls: list[tuple[str, ...]] = []
    original_run = module._run

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        if command[-1] == "--foreground":
            raise subprocess.TimeoutExpired(cmd=command, timeout=60)
        return subprocess.CompletedProcess(command, 0, "", "")

    setattr(module, "_run", fake_run)
    try:
        try:
            module._start_and_validate_wrapper(
                tmp_path,
                tmp_path / "wrapper",
                tmp_path / "health.py",
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("restart timeout was swallowed")
    finally:
        setattr(module, "_run", original_run)
    assert calls[-1][-1] == "--stop"


def test_install_dry_run_writes_executables_and_valid_plist(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    installed = tmp_path / "installed" / "hermes-maintenance-update"
    wrapper = tmp_path / "installed" / "hermes-gateway-wrapperctl"
    plist = tmp_path / "LaunchAgents" / "maintenance.plist"

    result = _run_script(
        repo,
        "--install",
        "--no-load",
        "--installed-script",
        str(installed),
        "--wrapperctl",
        str(wrapper),
        "--maintenance-plist",
        str(plist),
        "--state-dir",
        str(tmp_path / "state"),
    )

    assert result.returncode == 0, result.stderr
    assert installed.stat().st_mode & 0o111
    assert wrapper.stat().st_mode & 0o111
    payload = plistlib.loads(plist.read_bytes())
    assert payload["Label"] == "nz.diatche.hermes-maintenance-update"
    assert payload["ProgramArguments"][0] == str(installed)
    assert payload["RunAtLoad"] is False
    assert payload["KeepAlive"] is False


def test_install_failure_restores_previous_files_and_launchagent(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    installed = tmp_path / "installed" / "hermes-maintenance-update"
    wrapper = tmp_path / "installed" / "hermes-gateway-wrapperctl"
    plist = tmp_path / "LaunchAgents" / "maintenance.plist"
    fake_bin = tmp_path / "bin"
    counter = tmp_path / "bootstrap-count"
    for target, content in (
        (installed, "old updater\n"),
        (wrapper, "old wrapper\n"),
        (plist, "old plist\n"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    installed.chmod(0o700)
    wrapper.chmod(0o711)
    _write(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case ${1:-} in\n"
        "  print|bootout) exit 0 ;;\n"
        "  bootstrap)\n"
        f"    count=$(cat '{counter}' 2>/dev/null || echo 0)\n"
        f"    echo $((count + 1)) > '{counter}'\n"
        "    [[ $count -ge 1 ]] ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    (fake_bin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_script(
        repo,
        "--install",
        "--installed-script",
        str(installed),
        "--wrapperctl",
        str(wrapper),
        "--maintenance-plist",
        str(plist),
        "--state-dir",
        str(tmp_path / "state"),
        env=env,
    )

    assert result.returncode == 1
    assert "prior files were restored" in result.stderr
    assert installed.read_text(encoding="utf-8") == "old updater\n"
    assert wrapper.read_text(encoding="utf-8") == "old wrapper\n"
    assert plist.read_text(encoding="utf-8") == "old plist\n"
    assert installed.stat().st_mode & 0o777 == 0o700
    assert wrapper.stat().st_mode & 0o777 == 0o711
    assert counter.read_text(encoding="utf-8").strip() == "2"


def test_install_recovers_persisted_interrupted_transaction_before_validation(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    installed = tmp_path / "installed" / "hermes-maintenance-update"
    wrapper = tmp_path / "installed" / "hermes-gateway-wrapperctl"
    plist = tmp_path / "LaunchAgents" / "maintenance.plist"
    state_dir = tmp_path / "state"
    transaction = state_dir / "install-transaction"
    backups = transaction / "backups"
    backups.mkdir(parents=True)
    targets = (installed, wrapper, plist)
    for index, target in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("partial replacement\n", encoding="utf-8")
        (backups / str(index)).write_text(f"old-{index}\n", encoding="utf-8")
    journal = {
        "phase": "files-replaced",
        "domain": f"gui/{os.getuid()}",
        "service": f"gui/{os.getuid()}/nz.diatche.hermes-maintenance-update",
        "plist": str(plist),
        "was_loaded": False,
        "items": [
            {
                "target": str(target),
                "existed": True,
                "mode": 0o700,
                "backup": f"backups/{index}",
            }
            for index, target in enumerate(targets)
        ],
    }
    (transaction / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    _write(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n[[ ${1:-} == bootout ]] && exit 0\nexit 1\n",
    )
    _write(fake_bin, "plutil", "#!/usr/bin/env bash\nexit 9\n")
    for command in ("launchctl", "plutil"):
        (fake_bin / command).chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_script(
        repo,
        "--install",
        "--no-load",
        "--installed-script",
        str(installed),
        "--wrapperctl",
        str(wrapper),
        "--maintenance-plist",
        str(plist),
        "--state-dir",
        str(state_dir),
        env=env,
    )

    assert result.returncode == 1
    assert [target.read_text(encoding="utf-8") for target in targets] == [
        "old-0\n",
        "old-1\n",
        "old-2\n",
    ]
    assert not transaction.exists()


def test_detach_rejects_duplicate_queued_run(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    kickstarts = tmp_path / "kickstarts"
    _write(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ ${1:-} == kickstart ]]; then\n"
        f"  count=$(cat '{kickstarts}' 2>/dev/null || echo 0)\n"
        f"  echo $((count + 1)) > '{kickstarts}'\n"
        f"  /usr/bin/python3 -c \"import json; p='{state_dir}/state.json'; d=json.load(open(p)); d['claimed_at']='test'; d['phase']='preflight'; open(p,'w').write(json.dumps(d))\"\n"
        "fi\n"
        "exit 0\n",
    )
    (fake_bin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    args = ("--detach", "--state-dir", str(state_dir))

    first = _run_script(repo, *args, env=env)
    second = _run_script(repo, *args, env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 1
    assert "maintenance is already preflight" in second.stderr
    assert kickstarts.read_text(encoding="utf-8").strip() == "1"
    queued = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert queued["phase"] == "preflight"
    assert queued["queue_token"]


def test_detach_reports_worker_failure_after_queue_claim(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    _write(
        fake_bin,
        "launchctl",
        "#!/usr/bin/env bash\n"
        "if [[ ${1:-} == kickstart ]]; then\n"
        f"  /usr/bin/python3 -c \"import json; p='{state_dir}/state.json'; d=json.load(open(p)); d.update(claimed_at='test', phase='failed', error='preflight exploded'); open(p,'w').write(json.dumps(d))\"\n"
        "fi\n"
        "exit 0\n",
    )
    (fake_bin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run_script(repo, "--detach", "--state-dir", str(state_dir), env=env)

    assert result.returncode == 1
    assert "preflight exploded" in result.stderr
    assert "queued in its independent LaunchAgent" not in result.stdout


def test_run_refuses_unfinished_git_operation_metadata(tmp_path: Path) -> None:
    repo, _, upstream_sha = _make_remote_with_local_integration(tmp_path)
    (repo / ".git" / "MERGE_HEAD").write_text(upstream_sha + "\n", encoding="utf-8")

    result = _run_script(repo, "--run", "--no-fetch", "--state-dir", str(tmp_path / "state"))

    assert result.returncode == 1
    assert "unfinished Git operation" in result.stderr


def test_run_refuses_dirty_checkout_before_creating_backups(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    state_dir = tmp_path / "state"
    before_main = _git(repo, "rev-parse", "main")
    before_diatche = _git(repo, "rev-parse", "diatche")
    _write(repo, "dirty.txt", "not committed\n")

    result = _run_script(repo, "--run", "--no-fetch", "--state-dir", str(state_dir))

    assert result.returncode == 1
    assert "checkout is dirty" in result.stderr
    assert _git(repo, "rev-parse", "main") == before_main
    assert _git(repo, "rev-parse", "diatche") == before_diatche
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/backup/") == ""


def test_lock_contention_does_not_clobber_active_run_state(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    active_state = '{"phase":"updating","run":"active"}\n'
    (state_dir / "state.json").write_text(active_state, encoding="utf-8")

    with (state_dir / "maintenance.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_script(repo, "--run", "--no-fetch", "--state-dir", str(state_dir))

    assert result.returncode == 1
    assert "already running" in result.stderr
    assert (state_dir / "state.json").read_text(encoding="utf-8") == active_state


def test_lifecycle_marker_blocks_run_install_and_detach_during_recovery(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = state_dir / "active.json"
    marker.write_text('{"run_id":"recovering","pid":123}\n', encoding="utf-8")
    fake_bin = tmp_path / "bin"
    _write(fake_bin, "launchctl", "#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    run_result = _run_script(repo, "--run", "--no-fetch", "--state-dir", str(state_dir))
    install_result = _run_script(repo, "--install", "--no-load", "--state-dir", str(state_dir))
    detach_result = _run_script(repo, "--detach", "--state-dir", str(state_dir), env=env)

    assert run_result.returncode == 1
    assert install_result.returncode == 1
    assert detach_result.returncode == 1
    assert "lifecycle" in run_result.stderr
    assert "lifecycle" in install_result.stderr
    assert "lifecycle" in detach_result.stderr
    assert marker.is_file()


def test_run_validates_candidate_then_updates_and_restores_diatche(tmp_path: Path) -> None:
    repo, _, upstream_sha = _make_remote_with_local_integration(tmp_path)
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        "  git reset --hard origin/main >/dev/null\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)
    _git(repo, "branch", "feature/example")

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    assert result.returncode == 0, result.stderr
    assert _git(repo, "branch", "--show-current") == "diatche"
    assert _git(repo, "merge-base", "--is-ancestor", upstream_sha, "diatche") == ""
    assert _git(repo, "rev-parse", "main") == upstream_sha
    assert _git(repo, "status", "--porcelain") == ""
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert call_lines == [
        "update config check",
        "wrapper --status",
        "wrapper --stop",
        "wrapper --assert-update-quiescence",
        "update update --branch main --backup --yes --no-gateway-restart",
        "wrapper --assert-update-quiescence",
        "wrapper --enforce-exclusivity",
        "update config check",
        "wrapper --foreground",
        "wrapper --status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "complete"
    assert state["ok"] is True
    assert state["upstream_sha"] == upstream_sha
    assert _git(repo, "rev-parse", "diatche") == state["candidate_sha"]
    assert _git(repo, "rev-parse", state["candidate_ref"]) == state["candidate_sha"]
    backup_refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/backup/")
    assert "/diatche" in backup_refs
    assert "/main" in backup_refs
    assert "/feature/example" in backup_refs


def test_unhealthy_post_update_runtime_is_not_restarted(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    health_script = _make_fake_runtime(repo, tmp_path, healthy=False)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        "  git reset --hard origin/main >/dev/null\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    assert result.returncode == 1
    assert "live health check" in result.stderr
    assert _git(repo, "branch", "--show-current") == "diatche"
    calls_text = calls.read_text(encoding="utf-8")
    assert "wrapper --stop" in calls_text
    assert "wrapper --foreground" in calls_text
    assert calls_text.rstrip().endswith("wrapper --stop")
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["wrapper_restored"] is False
    assert "live health check" in state["recovery_error"]
    assert (state_dir / "active.json").is_file()


def test_upstream_drift_is_revalidated_before_restart(tmp_path: Path) -> None:
    repo, _, first_upstream = _make_remote_with_local_integration(tmp_path)
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    seed = tmp_path / "seed"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        f"  printf 'drift\\n' > {seed!s}/drift.txt\n"
        f"  git -C {seed!s} add drift.txt\n"
        f"  git -C {seed!s} -c user.name=Drift -c user.email=drift@example.test commit -m drift >/dev/null\n"
        f"  git -C {seed!s} push origin main >/dev/null\n"
        "  git fetch origin main >/dev/null\n"
        "  git switch main >/dev/null\n"
        "  git reset --hard origin/main >/dev/null\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    latest_upstream = _git(seed, "rev-parse", "HEAD")
    assert latest_upstream != first_upstream
    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "main") == latest_upstream
    assert _git(repo, "merge-base", "--is-ancestor", latest_upstream, "diatche") == ""
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["upstream_sha"] == latest_upstream


def test_updater_failure_restores_integration_checkout_and_wrapper(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    integration_before = _git(repo, "rev-parse", "diatche")
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        "  exit 23\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    assert result.returncode == 1
    assert "hermes update failed" in result.stderr
    assert _git(repo, "branch", "--show-current") == "diatche"
    assert _git(repo, "rev-parse", "diatche") == integration_before
    assert _git(repo, "status", "--porcelain") == ""
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "update config check",
        "wrapper --status",
        "wrapper --stop",
        "wrapper --assert-update-quiescence",
        "update update --branch main --backup --yes --no-gateway-restart",
        "update config check",
        "wrapper --foreground",
        "wrapper --status",
    ]
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "failed"
    assert state["ok"] is False


def test_updater_timeout_terminates_child_and_recovers_wrapper(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    terminated = tmp_path / "terminated"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        f"  trap 'touch {terminated!s}; exit 143' TERM\n"
        "  sleep 30 & wait\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--update-timeout",
        "1",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    assert result.returncode == 1
    assert "timed out" in result.stderr
    assert terminated.is_file()
    assert _git(repo, "branch", "--show-current") == "diatche"
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert call_lines[-2:] == ["wrapper --foreground", "wrapper --status"]


def test_sigterm_during_update_terminates_child_and_recovers_wrapper(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    terminated = tmp_path / "terminated"
    recovery_started = tmp_path / "recovery-started"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        f"  trap 'touch {terminated!s}; exit 143' TERM\n"
        "  sleep 30 & wait\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n"
        f"if [[ $1 == --foreground ]]; then touch {recovery_started!s}; sleep 1; fi\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)
    process = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--run",
            "--no-fetch",
            "--hermes",
            str(fake_hermes),
            "--wrapperctl",
            str(fake_wrapper),
            "--state-dir",
            str(state_dir),
            "--health-script",
            str(health_script),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if calls.is_file() and "update update" in calls.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    else:
        process.kill()
        raise AssertionError("maintenance never reached the updater")

    process.terminate()
    recovery_deadline = time.monotonic() + 10
    while time.monotonic() < recovery_deadline:
        if recovery_started.is_file():
            break
        time.sleep(0.05)
    else:
        process.kill()
        raise AssertionError("maintenance never entered wrapper recovery")

    # Recovery still owns the original maintenance lock, and a second signal is ignored.
    with (state_dir / "maintenance.lock").open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("maintenance lock was released before recovery completed")
    process.terminate()
    _stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 1
    assert "interrupted by SIGTERM" in stderr
    assert terminated.is_file()
    assert _git(repo, "branch", "--show-current") == "diatche"
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert call_lines[-2:] == ["wrapper --foreground", "wrapper --status"]


def test_updater_rewriting_integration_ref_fails_closed_without_restart(tmp_path: Path) -> None:
    repo, _, _ = _make_remote_with_local_integration(tmp_path)
    integration_before = _git(repo, "rev-parse", "diatche")
    health_script = _make_fake_runtime(repo, tmp_path)
    state_dir = tmp_path / "state"
    calls = tmp_path / "calls.log"
    fake_hermes = tmp_path / "fake-hermes"
    fake_wrapper = tmp_path / "fake-wrapper"
    _write(
        tmp_path,
        "fake-hermes",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'update %s\\n' \"$*\" >> {calls!s}\n"
        "if [[ ${1:-} == update ]]; then\n"
        "  git switch main >/dev/null\n"
        "  git branch -f diatche origin/main >/dev/null\n"
        "  exit 23\n"
        "fi\n",
    )
    _write(
        tmp_path,
        "fake-wrapper",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'wrapper %s\\n' \"$*\" >> {calls!s}\n",
    )
    fake_hermes.chmod(0o755)
    fake_wrapper.chmod(0o755)

    result = _run_script(
        repo,
        "--run",
        "--no-fetch",
        "--hermes",
        str(fake_hermes),
        "--wrapperctl",
        str(fake_wrapper),
        "--state-dir",
        str(state_dir),
        "--health-script",
        str(health_script),
    )

    assert result.returncode == 1
    assert "RECOVERY ERROR" in result.stderr
    assert _git(repo, "rev-parse", "diatche") != integration_before
    call_lines = calls.read_text(encoding="utf-8").splitlines()
    assert "wrapper --stop" in call_lines
    assert "wrapper --foreground" not in call_lines
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["wrapper_restored"] is False
    assert "expected diatche" in state["recovery_error"]
    assert _git(repo, "rev-parse", state["candidate_ref"]) == state["candidate_sha"]
