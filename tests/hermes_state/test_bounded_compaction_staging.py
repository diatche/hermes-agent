"""Behavioral contracts for bounded in-place compaction publication."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

import hermes_state_messages as state_messages
from hermes_state import SessionCompressionInProgressError, SessionDB


def _open_pair(tmp_path: Path) -> tuple[SessionDB, SessionDB]:
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    writer.create_session(
        "session",
        source="test",
        model_config={"keep": "yes", "remove": "old"},
    )
    return writer, SessionDB(path)


def _seed(db: SessionDB) -> list[str]:
    contents = ["old user", "old assistant", "older user"]
    for index, content in enumerate(contents):
        db.append_message(
            "session",
            role="user" if index % 2 == 0 else "assistant",
            content=content,
        )
    return contents


def _stage_sessions(db: SessionDB) -> list[dict[str, Any]]:
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT id, source, model_config, hidden FROM sessions WHERE hidden = 1 "
            "AND json_extract(model_config, ?) IS NOT NULL ORDER BY id",
            (f"$.{state_messages._COMPACTION_STAGE_MARKER}.target_session_id",),
        ).fetchall()
    return [dict(row) for row in rows]


def _stage_messages(db: SessionDB) -> list[dict[str, Any]]:
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT m.id, m.session_id, m.content, m.active, m.compacted "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.hidden = 1 AND json_extract(s.model_config, ?) IS NOT NULL ORDER BY m.id",
            (f"$.{state_messages._COMPACTION_STAGE_MARKER}.target_session_id",),
        ).fetchall()
    return [dict(row) for row in rows]


def _lock(db: SessionDB, holder: str) -> None:
    assert db.try_acquire_compression_lock("session", holder, ttl_seconds=60.0)


def test_unowned_call_keeps_single_transaction_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    try:
        _seed(writer)

        def _stage_must_not_run(*args, **kwargs):
            raise AssertionError("unowned compaction entered staged path")

        monkeypatch.setattr(state_messages, "_create_compaction_stage", _stage_must_not_run)
        assert writer.archive_and_compact(
            "session", [{"role": "user", "content": "legacy summary"}]
        ) == 1
        assert [row["content"] for row in peer.get_messages("session")] == [
            "legacy summary"
        ]
        assert _stage_sessions(peer) == []
    finally:
        writer.close()
        peer.close()


def test_staging_is_bounded_releases_writer_and_cutover_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "bounded-stage-test"
    release_worker = threading.Event()
    thread: threading.Thread | None = None
    try:
        _lock(writer, holder)
        old_contents = _seed(writer)
        watermark = writer.get_active_message_watermark("session")
        compacted = [
            {"role": "user", "content": "summary one"},
            {"role": "assistant", "content": "summary two"},
            {"role": "user", "content": "summary three"},
        ]

        monkeypatch.setattr(state_messages, "_COMPACTION_STAGE_MAX_ROWS", 1)
        monkeypatch.setattr(state_messages, "_COMPACTION_STAGE_MAX_BYTES", 1)
        first_chunk_committed = threading.Event()
        chunk_sizes: list[int] = []
        original = state_messages._stage_compaction_chunk

        def _pause_after_first_chunk(*args, **kwargs):
            chunk = args[-1]
            result = original(*args, **kwargs)
            chunk_sizes.append(len(chunk))
            if len(chunk_sizes) == 1:
                first_chunk_committed.set()
                if not release_worker.wait(timeout=15):
                    raise TimeoutError("test did not release compactor")
            return result

        monkeypatch.setattr(
            state_messages, "_stage_compaction_chunk", _pause_after_first_chunk
        )
        result: list[int] = []
        failures: list[BaseException] = []

        def _compact() -> None:
            try:
                result.append(
                    writer.archive_and_compact(
                        "session",
                        compacted,
                        watermark=watermark,
                        lock_holder=holder,
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        thread = threading.Thread(target=_compact, daemon=True)
        thread.start()
        assert first_chunk_committed.wait(timeout=15)

        stages = _stage_sessions(peer)
        staged_rows = _stage_messages(peer)
        assert len(stages) == 1 and stages[0]["hidden"] == 1
        assert stages[0]["source"] == "test"
        marker = json.loads(stages[0]["model_config"])
        assert marker[state_messages._COMPACTION_STAGE_MARKER][
            "target_session_id"
        ] == "session"
        assert len(staged_rows) == 1
        assert staged_rows[0]["content"] == "summary one"
        assert staged_rows[0]["active"] == 0
        assert staged_rows[0]["compacted"] == 0

        # Before cutover the full old view and counters remain authoritative.
        assert [row["content"] for row in peer.get_messages("session")] == old_contents
        assert peer.get_session("session")["message_count"] == 3
        assert peer.search_messages("summary one") == []

        # A committed stage boundary releases SQLite's writer slot.
        peer.append_message("session", role="assistant", content="concurrent tail")
        release_worker.set()
        thread.join(timeout=20)

        assert not thread.is_alive()
        assert failures == []
        assert result == [4]
        assert chunk_sizes == [1, 1, 1]
        assert [row["content"] for row in writer.get_messages("session")] == [
            "summary one",
            "summary two",
            "summary three",
            "concurrent tail",
        ]
        assert writer.get_session("session")["message_count"] == 4
        assert _stage_sessions(writer) == []
        assert _stage_messages(writer) == []
    finally:
        release_worker.set()
        if thread is not None:
            thread.join(timeout=20)
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_staging_transactions_are_also_bounded_by_encoded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "byte-bound-test"
    try:
        _lock(writer, holder)
        _seed(writer)
        messages = [
            {"role": "user", "content": "a" * 80},
            {"role": "assistant", "content": "b" * 80},
            {"role": "user", "content": "c" * 80},
        ]
        one_size = state_messages._compaction_message_size(messages[0])
        monkeypatch.setattr(state_messages, "_COMPACTION_STAGE_MAX_ROWS", 100)
        monkeypatch.setattr(
            state_messages, "_COMPACTION_STAGE_MAX_BYTES", one_size + 1
        )
        chunks: list[list[str]] = []
        original = state_messages._stage_compaction_chunk

        def _record(*args, **kwargs):
            chunks.append([message["content"] for message in args[-1]])
            return original(*args, **kwargs)

        monkeypatch.setattr(state_messages, "_stage_compaction_chunk", _record)
        assert writer.archive_and_compact(
            "session", messages, lock_holder=holder
        ) == 3
        assert chunks == [["a" * 80], ["b" * 80], ["c" * 80]]
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_cutover_preserves_model_config_tail_sidecars_display_and_search(
    tmp_path: Path,
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "fidelity-test"
    try:
        _lock(writer, holder)
        _seed(writer)
        watermark = writer.get_active_message_watermark("session")
        peer.append_message(
            "session",
            role="assistant",
            content="concurrent searchable needle",
            api_content="exact-api-bytes",
            platform_message_id="platform-42",
            token_count=17,
            reasoning_content="private chain",
            reasoning_details=[{"type": "reasoning", "text": "detail"}],
            display_kind="task_complete",
            display_metadata={"task_count": 2},
        )
        compacted = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "older user", "timestamp": 123.0},
        ]

        assert writer.archive_and_compact(
            "session",
            compacted,
            model_config_patch={"remove": None, "added": 7},
            watermark=watermark,
            lock_holder=holder,
            tail_count=1,
        ) == 3

        live = writer.get_messages("session")
        assert [row["content"] for row in live] == [
            "summary",
            "older user",
            "concurrent searchable needle",
        ]
        tail = live[-1]
        assert tail["api_content"] == "exact-api-bytes"
        assert tail["platform_message_id"] == "platform-42"
        assert tail["token_count"] == 17
        assert tail["reasoning_content"] == "private chain"
        assert json.loads(tail["reasoning_details"])[0]["text"] == "detail"
        assert tail["display_kind"] == "task_complete"
        assert tail["display_metadata"] == {"task_count": 2}
        assert json.loads(writer.get_session("session")["model_config"]) == {
            "keep": "yes",
            "added": 7,
        }
        assert writer.search_messages("needle")

        originals = [
            row
            for row in writer.get_messages("session", include_inactive=True)
            if row["content"] == "older user" and not row["active"]
        ]
        assert originals and all(not row["compacted"] for row in originals)
        assert _stage_sessions(writer) == []
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_staged_cutover_preserves_exact_noncontiguous_carried_rows(tmp_path: Path) -> None:
    """Lease-owned staging must compose with exact carried-row rewind semantics."""
    writer, peer = _open_pair(tmp_path)
    holder = "carried-row-stage-test"
    try:
        _lock(writer, holder)
        writer.append_message("session", role="user", content="question 0")
        writer.append_message(
            "session", role="assistant", content="",
            tool_calls=[{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        )
        writer.append_message(
            "session", role="tool", content="tool result that was summarized",
            tool_call_id="call-1", tool_name="read_file",
        )
        writer.append_message("session", role="user", content="question 1")
        writer.append_message("session", role="assistant", content="answer 1")

        carried = [
            message for message in writer.get_messages_as_conversation("session")
            if message.get("content") in {"question 0", "question 1", "answer 1"}
        ]
        assert carried and all("_row_id" not in message for message in carried)

        assert writer.archive_and_compact(
            "session",
            [
                {"role": "user", "content": "question 0"},
                {"role": "assistant", "content": "[CONTEXT COMPACTION] summarized tool exchange"},
                {"role": "user", "content": "question 1"},
                {"role": "assistant", "content": "answer 1"},
            ],
            lock_holder=holder,
            carried_messages=carried,
        ) == 4

        rows = writer.get_messages("session", include_inactive=True)
        tool_rows = [row for row in rows if row["content"] == "tool result that was summarized"]
        assert len(tool_rows) == 1
        assert not tool_rows[0]["active"] and tool_rows[0]["compacted"]
        assert writer.search_messages("summarized")

        carried_originals = [
            row for row in rows
            if row["content"] in {"question 0", "question 1", "answer 1"} and not row["active"]
        ]
        assert len(carried_originals) == 3
        assert all(not row["compacted"] for row in carried_originals)
        assert _stage_sessions(writer) == []
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


@pytest.mark.parametrize(
    ("source", "model_config"),
    [
        ("subagent", {}),
        ("telegram", {"_delegate_from": "parent"}),
    ],
)
def test_staging_preserves_source_sensitive_fts_exclusion(
    tmp_path: Path, source: str, model_config: dict[str, Any]
) -> None:
    path = tmp_path / "state.db"
    writer = SessionDB(path)
    holder = f"fts-eligibility-{source}"
    try:
        writer.create_session(
            "session", source=source, model_config=model_config
        )
        if not writer._trigram_available:
            pytest.skip("trigram tokenizer unavailable in this SQLite build")
        writer.append_message("session", role="user", content="old content")
        _lock(writer, holder)

        assert writer.archive_and_compact(
            "session",
            [{"role": "assistant", "content": "replacement substring needle"}],
            lock_holder=holder,
        ) == 1

        live_id = writer.get_messages("session")[0]["id"]
        trigram_ids = {
            int(row[0])
            for row in writer._conn.execute(
                "SELECT id FROM messages_fts_trigram_docsize"
            ).fetchall()
        }
        base_ids = {
            int(row[0])
            for row in writer._conn.execute(
                "SELECT id FROM messages_fts_docsize"
            ).fetchall()
        }
        assert live_id not in trigram_ids
        assert live_id in base_ids
        writer._conn.execute(
            "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('integrity-check')"
        )
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()


def test_cutover_failure_keeps_old_view_restores_inputs_and_cleans_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "cutover-failure-test"
    try:
        _lock(writer, holder)
        old_contents = _seed(writer)
        compacted = [
            {"role": "user", "content": "never published"},
            {"role": "assistant", "content": "still hidden", "_row_id": 91},
        ]
        watermark = writer.get_active_message_watermark("session")
        peer.append_message("session", role="assistant", content="concurrent tail")

        def _fail_during_cutover(*args, **kwargs):
            raise RuntimeError("injected cutover failure")

        # This hook runs after the old rows were archived and staged rows activated.
        # Raising here proves the cutover transaction rolls all visibility changes back.
        monkeypatch.setattr(writer, "_clone_message_rows", _fail_during_cutover)
        with pytest.raises(RuntimeError, match="injected cutover failure"):
            writer.archive_and_compact(
                "session", compacted, watermark=watermark, lock_holder=holder
            )

        assert [row["content"] for row in peer.get_messages("session")] == [
            *old_contents,
            "concurrent tail",
        ]
        assert peer.get_session("session")["message_count"] == 4
        assert _stage_sessions(peer) == []
        assert _stage_messages(peer) == []
        assert "_row_id" not in compacted[0]
        assert compacted[1]["_row_id"] == 91
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_lost_lease_during_staging_never_changes_active_view_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    old_holder = "old-holder"
    new_holder = "new-holder"
    try:
        _lock(writer, old_holder)
        old_contents = _seed(writer)
        monkeypatch.setattr(state_messages, "_COMPACTION_STAGE_MAX_ROWS", 1)
        original = state_messages._stage_compaction_chunk
        calls = 0

        def _lose_after_first(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                writer.release_compression_lock("session", old_holder)
                _lock(peer, new_holder)
            return result

        monkeypatch.setattr(
            state_messages, "_stage_compaction_chunk", _lose_after_first
        )
        with pytest.raises(SessionCompressionInProgressError):
            writer.archive_and_compact(
                "session",
                [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
                lock_holder=old_holder,
            )

        assert [row["content"] for row in peer.get_messages("session")] == old_contents
        assert _stage_sessions(peer) == []
        assert _stage_messages(peer) == []
    finally:
        writer.release_compression_lock("session", old_holder)
        peer.release_compression_lock("session", new_holder)
        writer.close()
        peer.close()


def test_proven_holder_reclaims_stale_stage_in_bounded_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, peer = _open_pair(tmp_path)
    old_holder = "crashed-holder"
    new_holder = "replacement-holder"
    stale_stage_id = "_hcmp_stale_test"
    try:
        _seed(writer)
        _lock(writer, old_holder)
        state_messages._create_compaction_stage(
            writer, "session", stale_stage_id, old_holder
        )
        state_messages._stage_compaction_chunk(
            writer,
            "session",
            stale_stage_id,
            old_holder,
            [{"role": "user", "content": f"orphan-{i}"} for i in range(5)],
        )
        writer.release_compression_lock("session", old_holder)
        _lock(peer, new_holder)

        monkeypatch.setattr(state_messages, "_COMPACTION_CLEANUP_MAX_ROWS", 2)
        # The helper itself loops in bounded transactions; trace SQL to prove no
        # DELETE transaction removes more than the configured row bound.
        deleted_per_transaction: list[int] = []
        execute = writer._execute_write

        def _tracked_execute(fn, *args, **kwargs):
            before = len(_stage_messages(peer))
            result = execute(fn, *args, **kwargs)
            after = len(_stage_messages(peer))
            if before > after:
                deleted_per_transaction.append(before - after)
            return result

        monkeypatch.setattr(writer, "_execute_write", _tracked_execute)
        assert writer.archive_and_compact(
            "session",
            [{"role": "user", "content": "fresh summary"}],
            lock_holder=new_holder,
        ) == 1

        assert deleted_per_transaction[:3] == [2, 2, 1]
        assert all(step <= 2 for step in deleted_per_transaction[:3])
        assert _stage_sessions(writer) == []
        assert _stage_messages(writer) == []
        assert [row["content"] for row in writer.get_messages("session")] == [
            "fresh summary"
        ]
    finally:
        writer.release_compression_lock("session", old_holder)
        peer.release_compression_lock("session", new_holder)
        writer.close()
        peer.close()
