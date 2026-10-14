"""Conditional updates protect reviewed raw records without replaying old one-shots.

Run with scripts/run_tests.sh tests/cron/test_conditional_update.py. All stores
are temporary; child processes import the real API against the same temp home.
"""

import copy
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from cron import jobs


def raw_job(job_id):
    return next(j for j in jobs.load_jobs(repair=False) if j["id"] == job_id)


def unchanged_file(path):
    return path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino


def test_conditional_update_rejects_competitor_and_preserves_raw_profile_state(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    created = jobs.create_job(prompt="reviewed task", schedule="every 1h")
    job_id = created["id"]
    expected = raw_job(job_id)
    jobs.pause_job(job_id, reason="competing operator")
    path = home_a / "cron" / "jobs.json"
    before = unchanged_file(path)
    # A conflict must not even enter the save path (including load-time repair).
    with patch.object(jobs, "save_jobs", side_effect=AssertionError("unexpected write")):
        with pytest.raises(jobs.JobUpdateConflict) as caught:
            jobs.update_job(job_id, {"prompt": "stale replacement"}, expected_job=expected)
    assert caught.value.job_id == job_id
    assert caught.value.current_job == raw_job(job_id)
    assert unchanged_file(path) == before

    expected = raw_job(job_id)
    # Reordering object keys is immaterial; every field, including pause markers, counts.
    jobs.update_job(job_id, {"paused_reason": "changed reason only"})
    before = unchanged_file(path)
    with pytest.raises(jobs.JobUpdateConflict):
        jobs.update_job(job_id, {"prompt": "stale"}, expected_job=expected)
    assert unchanged_file(path) == before
    expected = raw_job(job_id)
    reordered = dict(reversed(list(expected.items())))
    updated = jobs.update_job(job_id, {"prompt": "accepted replacement"}, expected_job=reordered)
    assert updated["prompt"] == "accepted replacement"
    before = unchanged_file(path)
    with pytest.raises(jobs.JobUpdateConflict):
        jobs.update_job(job_id, {"prompt": "accepted replacement"}, expected_job=expected)
    assert unchanged_file(path) == before
    current = raw_job(job_id)
    jobs.update_job(job_id, {"prompt": "accepted replacement"}, expected_job=current)

    # A -> B -> A with the SAME id: neither matching nor conflict writes cross homes.
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    jobs.save_jobs([{**current, "prompt": "profile B"}])
    b_before = unchanged_file(home_b / "cron" / "jobs.json")
    with pytest.raises(jobs.JobUpdateConflict):
        jobs.update_job(job_id, {"prompt": "wrong profile"}, expected_job=current)
    assert unchanged_file(home_b / "cron" / "jobs.json") == b_before
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert raw_job(job_id)["prompt"] == "accepted replacement"

    # Raw nullable fields and absent keys must not collapse to normalized views.
    raw = {**current, "name": None, "prompt": None}
    raw.pop("paused_reason")
    jobs.save_jobs([raw])
    normalized = jobs.get_job(job_id)
    before = unchanged_file(path)
    with pytest.raises(jobs.JobUpdateConflict):
        jobs.update_job(job_id, {"name": "new"}, expected_job=normalized)
    assert unchanged_file(path) == before
    # A repairable bare-list store must not be rewritten on mismatch.
    for field, value in (("enabled", 0), ("paused_at", None), ("paused_reason", None)):
        different = {**raw, field: value}
        with pytest.raises(jobs.JobUpdateConflict):
            jobs.update_job(job_id, {"name": "new"}, expected_job=different)
        assert unchanged_file(path) == before
    path.write_text(json.dumps([raw]))
    before = unchanged_file(path)
    with pytest.raises(jobs.JobUpdateConflict):
        jobs.update_job(job_id, {"name": "new"}, expected_job=normalized)
    assert unchanged_file(path) == before
    assert jobs.update_job(job_id, {"name": "new"}, expected_job=raw)["name"] == "new"
    jobs.remove_job(job_id)
    before = unchanged_file(path)
    with pytest.raises(jobs.JobUpdateConflict) as caught:
        jobs.update_job(job_id, {}, expected_job=raw)
    assert caught.value.current_job is None
    assert unchanged_file(path) == before
    assert jobs.update_job(job_id, {}) is None


def test_conditional_update_one_winner_across_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    created = jobs.create_job(prompt="reviewed", schedule="every 1h")
    expected = raw_job(created["id"])
    code = """
import json, sys
from cron.jobs import update_job, JobUpdateConflict
expected = json.loads(sys.argv[1])
try:
    update_job(expected['id'], {'prompt': sys.argv[2]}, expected_job=expected)
    print('updated')
except JobUpdateConflict:
    print('conflict')
"""
    children = [subprocess.Popen(
        [sys.executable, "-c", code, json.dumps(expected), prompt],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy(),
    ) for prompt in ("winner A", "winner B")]
    outcomes = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
        outcomes.append(stdout.strip())
    assert sorted(outcomes) == ["conflict", "updated"]
    assert raw_job(created["id"])["prompt"] in {"winner A", "winner B"}


def test_conditional_restore_does_not_rearm_stale_oneshot_and_fails_closed_without_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Seed a retained, enabled one-shot whose instant has long expired.
    original = {
        "id": "retained-once", "name": "retained task", "prompt": "do not replay",
        "schedule": {"kind": "once", "run_at": "2000-01-01T00:00:00+00:00"},
        "next_run_at": "2000-01-01T00:00:00+00:00", "enabled": True,
        "state": "scheduled", "paused_at": None, "paused_reason": None,
        "repeat": {"times": 1, "completed": 0}, "skills": [], "skill": None,
    }
    jobs.save_jobs([copy.deepcopy(original)])
    jobs.pause_job(original["id"], reason="maintenance")
    paused = raw_job(original["id"])
    path = tmp_path / "cron" / "jobs.json"
    before = unchanged_file(path)
    with pytest.raises(ValueError, match="Cannot resume"):
        jobs.resume_job(original["id"])
    assert unchanged_file(path) == before
    controls = {k: original[k] for k in ("enabled", "state", "paused_at", "paused_reason")}
    jobs.update_job(original["id"], controls, expected_job=paused)
    assert raw_job(original["id"]) == original
    assert jobs.compute_next_run(original["schedule"]) is None
    expected = raw_job(original["id"])
    before = unchanged_file(path)
    for lock_result in (False, None):
        with patch.object(jobs, "_acquire_flock", return_value=lock_result):
            with pytest.raises(RuntimeError, match="cross-process lock"):
                jobs.update_job(original["id"], {"name": "unsafe"}, expected_job=expected)
    assert unchanged_file(path) == before
    with patch.object(jobs, "_acquire_flock", side_effect=OSError("unavailable")):
        with pytest.raises(RuntimeError, match="cross-process lock"):
            jobs.update_job(original["id"], {"name": "unsafe"}, expected_job=expected)
    assert unchanged_file(path) == before
    # A nested call must not borrow another profile's held advisory lock.
    with jobs._jobs_lock():
        with jobs.use_cron_store(tmp_path / "other"):
            with pytest.raises(RuntimeError, match="cross-process lock"):
                jobs.update_job(original["id"], {}, expected_job=expected)
    assert unchanged_file(path) == before
    assert jobs.get_due_jobs() == []  # real due scan cannot dispatch the retained instant
