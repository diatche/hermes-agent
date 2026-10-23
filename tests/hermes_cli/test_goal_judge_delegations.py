"""The goal judge knows about delegated subagents.

In a fan-out run 4 of 5 /goal nudges fired 6-153 s after a turn that had said "waiting on workers,
nothing to dispatch": the judge prompt had no WAIT branch for delegated subagents (only for registry
processes), so it returned CONTINUE and each nudge bought a status recap (19 API calls, ~$4.9).
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import goals


@pytest.fixture
def rotated_work(tmp_path, monkeypatch):
    """Real compression lineage; every record deliberately shares a routing key."""
    from hermes_state import SessionDB
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    key = "agent:main:telegram:dm:test-peer:test-topic"
    db.create_session("root", "telegram", session_key=key)
    db.end_session("root", end_reason="compression")
    db.create_session("middle", "telegram", parent_session_id="root", session_key=key)
    db.end_session("middle", end_reason="compression")
    db.create_session("tip", "telegram", parent_session_id="middle", session_key=key)
    for sid, source, cfg in (
        ("branch", "telegram", {"_branched_from": "root"}),
        ("delegate", "telegram", {"_delegate_from": "root"}),
        ("tool", "tool", {}),
        ("reset", "telegram", {"_reset_from": "root"}),
    ):
        db.create_session(sid, source, parent_session_id="root", model_config=cfg, session_key=key)
    db.create_session("grandchild", "tool", parent_session_id="delegate", session_key=key)
    db.create_session("unrelated", "telegram", session_key=key)
    db.create_session("old-reset", "telegram", session_key=key)
    db.end_session("old-reset", end_reason="reset")
    db.create_session("after-reset", "telegram", parent_session_id="old-reset", session_key=key)
    owners = ["root", "middle", "tip", "branch", "delegate", "tool", "reset",
              "grandchild", "unrelated", "old-reset", "after-reset"]
    procs = [dict(session_id=f"proc_{sid}", owner_task_id=sid, task_id="default",
                  session_key=key, status="running") for sid in owners]
    procs.append(dict(session_id="proc_done", owner_task_id="root", status="exited"))
    monkeypatch.setattr(process_registry, "list_sessions", lambda **kw: procs)
    records = {sid: dict(parent_session_id=sid, session_key=key, status="running") for sid in owners}
    records["done"] = dict(parent_session_id="root", session_key=key, status="completed")
    monkeypatch.setattr(ad, "_records", records)
    yield db, records
    db.close()


def test_background_processes_survive_compression_without_claiming_child_work(rotated_work):
    for sid in ("tip", "root"):
        assert [p["session_id"] for p in goals.gather_background_processes(owner_task_id=sid)] == [
            "proc_root", "proc_middle", "proc_tip"]
    for sid in ("branch", "delegate", "tool", "reset", "grandchild", "unrelated", "after-reset"):
        assert [p["session_id"] for p in goals.gather_background_processes(owner_task_id=sid)] == [f"proc_{sid}"]
    for unavailable in (False, True):
        with patch.object(goals, "_get_session_db", return_value=None,
                          side_effect=RuntimeError("store unavailable") if unavailable else None):
            assert [p["session_id"] for p in goals.gather_background_processes(owner_task_id="tip")] == ["proc_tip"]


def test_delegations_survive_compression_and_keep_the_goal_parked(rotated_work):
    _db, records = rotated_work
    mgr = goals.GoalManager(session_id="tip")
    mgr.set("integrate the delegated work")
    live = goals.count_active_delegations("tip")
    assert live == 3
    with patch.object(goals, "judge_goal", return_value=("continue", "workers running", False, None, False)):
        decision = mgr.evaluate_after_turn("Waiting on workers.", active_delegations=live)
    assert decision["should_continue"] is False
    assert decision["verdict"] == "wait"
    assert mgr.state is not None
    assert mgr.state.turns_used == 0
    assert mgr.is_waiting() is True
    assert goals.count_active_delegations("root") == 3
    records["root"]["status"] = "completed"
    assert mgr.is_waiting() is False
    assert goals.count_active_delegations("tip") == 2
    for sid in ("branch", "delegate", "tool", "reset", "grandchild", "unrelated", "after-reset"):
        assert goals.count_active_delegations(sid) == 1
    for unavailable in (False, True):
        with patch.object(goals, "_get_session_db", return_value=None,
                          side_effect=RuntimeError("store unavailable") if unavailable else None):
            assert goals.count_active_delegations("tip") == 1


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_judge_prompt_states_active_delegations_and_a_wait_branch_for_them():
    seen = {}

    def fake_call_llm(*a, **kw):
        seen["prompt"] = kw.get("messages") or a
        return _resp('{"verdict": "wait", "wait_for_seconds": 900, "reason": "waiting on workers"}')

    with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm):
        verdict, _reason, parse_failed, directive, _transport = goals.judge_goal(
            "refactor everything", "Waiting on 4 workers; nothing to dispatch.", active_delegations=4)
    text = str(seen["prompt"])
    assert "Active delegations" in text
    assert (verdict, parse_failed) == ("wait", False) and directive.get("seconds") == 900

    with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm):
        goals.judge_goal("g", "r", active_delegations=0)
    assert "Active delegations" not in str(seen["prompt"])


def test_count_active_delegations_is_scoped_to_the_spawning_session():
    from tools import async_delegation as ad

    fake = {
        "a": {"status": "running", "parent_session_id": "root", "session_key": "", "origin_ui_session_id": ""},
        "b": {"status": "completed", "parent_session_id": "root", "session_key": "", "origin_ui_session_id": ""},
        "c": {"status": "running", "parent_session_id": "other", "session_key": "", "origin_ui_session_id": ""},
    }
    with patch.object(ad, "_records", fake):
        assert goals.count_active_delegations("root") == 1
        assert goals.count_active_delegations("other") == 1
        assert goals.count_active_delegations(None) == 0


def test_a_delegation_wait_lifts_when_a_batch_returns_not_only_when_the_timer_runs_out(tmp_path, monkeypatch):
    """Independent-review witness: results arrived with 1,199 s left on a 1,200 s WAIT and nothing
    re-judged; integration sat unfinished until the timer ran out."""
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes")); (tmp_path / ".hermes").mkdir()
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager(session_id="root-wait")
    mgr.set("integrate the rounds")
    with patch.object(goals, "count_active_delegations", return_value=4):
        mgr.wait_for_seconds(1200, reason="4 batches running", on_delegations=4)
        assert mgr.is_waiting() is True                     # all four still live: parked
    with patch.object(goals, "count_active_delegations", return_value=3):
        assert mgr.is_waiting() is False                    # one returned: barrier lifted early
    assert mgr.state.waiting_until == 0.0 and mgr.state.waiting_on_delegations == 0
    # a plain timed wait (no delegations) is unaffected by the delegation count
    mgr.wait_for_seconds(1200, reason="cooldown")
    with patch.object(goals, "count_active_delegations", return_value=0):
        assert mgr.is_waiting() is True
    goals._DB_CACHE.clear()


def test_active_delegations_park_a_continue_without_burning_a_goal_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes")); (tmp_path / ".hermes").mkdir()
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager(session_id="root-continue")
    mgr.set("integrate the delegated work")

    with patch.object(
        goals, "judge_goal",
        return_value=("continue", "the worker is still running", False, None, False),
    ):
        decision = mgr.evaluate_after_turn("The worker is still running.", active_delegations=2)

    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False
    state = mgr.state
    assert state is not None
    assert state.status == "active"
    assert state.turns_used == 0
    assert state.waiting_on_delegations == 2
    goals._DB_CACHE.clear()


def test_active_delegations_defer_a_blocked_verdict_instead_of_pausing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes")); (tmp_path / ".hermes").mkdir()
    goals._DB_CACHE.clear()
    mgr = goals.GoalManager(session_id="root-blocked")
    mgr.set("integrate the delegated work")

    with patch.object(
        goals, "judge_goal",
        return_value=("blocked", "the worker needs correction", False, None, False),
    ):
        decision = mgr.evaluate_after_turn("The worker needs correction.", active_delegations=1)

    assert decision["verdict"] == "wait"
    assert decision["should_continue"] is False
    state = mgr.state
    assert state is not None
    assert state.status == "active"
    assert state.turns_used == 0
    assert state.waiting_on_delegations == 1
    goals._DB_CACHE.clear()
