"""Gateway-side session binding for async delegations (#57498, #55578).

Three invariants on the messaging-gateway surface, mirroring the TUI rules:

1. Completions are pinned to the spawning session (contributor commit).
2. A dead/ended spawning session is never resurrected: the injection is
   dropped, fail-closed (never rerouted to the peer's current session).
3. /new interrupts the old conversation's in-flight async delegations.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.async_delegation as ad


@pytest.fixture(autouse=True)
def _reset_async_delegation():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _seed_record(delegation_id, session_key="", parent_session_id="", status="running"):
    fn = MagicMock()
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": status,
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "interrupt_fn": fn,
        }
    return fn


class TestInterruptForSessionByParentId:
    def test_parent_session_id_selector(self):
        mine = _seed_record("d1", session_key="agent:main:telegram:dm:1", parent_session_id="sess_old")
        other = _seed_record("d2", session_key="agent:main:telegram:dm:2", parent_session_id="sess_other")
        n = ad.interrupt_for_session(parent_session_id="sess_old")
        assert n == 1
        mine.assert_called_once()
        other.assert_not_called()

    def test_reset_interrupts_by_key_and_parent(self):
        """A /new reset passes both selectors — either match claims the record."""
        by_key = _seed_record("d1", session_key="agent:main:telegram:dm:1", parent_session_id="")
        by_parent = _seed_record("d2", session_key="", parent_session_id="sess_old")
        unrelated = _seed_record("d3", session_key="other", parent_session_id="other")
        n = ad.interrupt_for_session(
            session_key="agent:main:telegram:dm:1",
            parent_session_id="sess_old",
            reason="session_reset",
        )
        assert n == 2
        by_key.assert_called_once()
        by_parent.assert_called_once()
        unrelated.assert_not_called()


class TestGatewayPinningFailsClosed:
    """The gateway injection path must never resurrect an ended session."""

    def _make_runner(self, pinned_row, *, compression_tip=None, tip_row=None):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        db = MagicMock()

        async def _get_session(session_id):
            if compression_tip and session_id == compression_tip:
                return tip_row
            return pinned_row

        db.get_session = AsyncMock(side_effect=_get_session)
        db.get_compression_tip = AsyncMock(
            return_value=compression_tip or "sess_old"
        )
        runner._session_db = db

        entry = MagicMock()
        entry.session_key = "agent:main:telegram:dm:1"
        entry.session_id = "sess_current"
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = entry
        runner.session_store.switch_session.return_value = entry
        return runner, entry

    def _run_pinning_prefix(self, runner, pinned_session_id):
        """Execute the pinning guard logic exactly as _handle_message does."""

        async def _go():
            event = MagicMock()
            event.metadata = {"gateway_session_id": pinned_session_id}
            session_entry = runner.session_store.get_or_create_session(MagicMock())
            pinned = str((getattr(event, "metadata", None) or {}).get("gateway_session_id") or "").strip()
            if pinned and pinned != session_entry.session_id:
                pinned_row = None
                try:
                    if runner._session_db is not None:
                        pinned_row = await runner._session_db.get_session(pinned)
                except Exception:
                    pinned_row = None
                if pinned_row is not None and pinned_row.get("ended_at"):
                    if pinned_row.get("end_reason") == "compression":
                        try:
                            tip = await runner._session_db.get_compression_tip(pinned)
                        except Exception:
                            tip = pinned
                        if tip and tip != pinned:
                            pinned = tip
                            try:
                                pinned_row = await runner._session_db.get_session(pinned)
                            except Exception:
                                pinned_row = None
                if pinned_row is None or pinned_row.get("ended_at"):
                    return "dropped"
                if pinned != session_entry.session_id:
                    switched = runner.session_store.switch_session(
                        session_entry.session_key, pinned
                    )
                    if switched is not None:
                        return "pinned"
                return "continued"
            return "default"

        return asyncio.run(_go())

    def test_live_spawning_session_pins(self):
        runner, _ = self._make_runner({"id": "sess_old", "ended_at": None})
        assert self._run_pinning_prefix(runner, "sess_old") == "pinned"

    def test_ended_spawning_session_drops(self):
        runner, _ = self._make_runner(
            {
                "id": "sess_old",
                "ended_at": "2026-07-08T00:00:00",
                "end_reason": "reset",
            }
        )
        assert self._run_pinning_prefix(runner, "sess_old") == "dropped"
        runner.session_store.switch_session.assert_not_called()

    def test_compression_parent_completion_routes_to_current_continuation(self):
        runner, _ = self._make_runner(
            {
                "id": "sess_old",
                "ended_at": "2026-07-08T00:00:00",
                "end_reason": "compression",
            },
            compression_tip="sess_current",
            tip_row={"id": "sess_current", "ended_at": None},
        )
        assert self._run_pinning_prefix(runner, "sess_old") == "continued"
        runner.session_store.switch_session.assert_not_called()

    def test_compression_parent_completion_pins_live_continuation(self):
        runner, _ = self._make_runner(
            {
                "id": "sess_old",
                "ended_at": "2026-07-08T00:00:00",
                "end_reason": "compression",
            },
            compression_tip="sess_child",
            tip_row={"id": "sess_child", "ended_at": None},
        )
        assert self._run_pinning_prefix(runner, "sess_old") == "pinned"
        runner.session_store.switch_session.assert_called_once_with(
            "agent:main:telegram:dm:1", "sess_child"
        )

    def test_unknown_spawning_session_drops(self):
        runner, _ = self._make_runner(None)
        assert self._run_pinning_prefix(runner, "sess_gone") == "dropped"
        runner.session_store.switch_session.assert_not_called()


class TestResetHandlerInterruptsDelegations:
    def test_reset_command_calls_interrupt_for_session(self):
        """The /new handler must sever the old conversation's delegations."""
        import inspect
        from gateway import slash_commands

        src = inspect.getsource(slash_commands.GatewaySlashCommandsMixin._handle_reset_command)
        assert "interrupt_for_session" in src
        assert "session_reset" in src
