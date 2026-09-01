"""Regression coverage for bounded state.db compaction publication."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent import state_compaction
from hermes_state import SessionDB


def _open_pair(tmp_path: Path) -> tuple[SessionDB, SessionDB]:
    db_path = tmp_path / "state.db"
    writer = SessionDB(db_path)
    writer.create_session("session", source="test")
    peer = SessionDB(db_path)
    return writer, peer


def _seed(db: SessionDB) -> list[str]:
    contents = ["old user", "old assistant", "older user"]
    for index, content in enumerate(contents):
        db.append_message(
            "session",
            role="user" if index % 2 == 0 else "assistant",
            content=content,
        )
    return contents


def _stage_sessions(db: SessionDB) -> list[dict]:
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT id, model_config, hidden "
            "FROM sessions WHERE source = ? ORDER BY id",
            (state_compaction._STAGE_SOURCE,),
        ).fetchall()
    return [dict(row) for row in rows]


def _stage_messages(db: SessionDB) -> list[dict]:
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT m.id, m.session_id, m.content, m.active, m.compacted "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.source = ? ORDER BY m.id",
            (state_compaction._STAGE_SOURCE,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_public_sessiondb_method_is_bounded_implementation() -> None:
    assert SessionDB.archive_and_compact is state_compaction.archive_and_compact
    assert SessionDB._archive_and_compact_legacy is not SessionDB.archive_and_compact


def test_unowned_call_keeps_historical_single_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, peer = _open_pair(tmp_path)
    try:
        _seed(writer)

        def _stage_must_not_run(*args, **kwargs):
            raise AssertionError("unowned compaction entered staged path")

        monkeypatch.setattr(
            state_compaction,
            "_create_stage_session",
            _stage_must_not_run,
        )
        count = writer.archive_and_compact(
            "session",
            [{"role": "user", "content": "legacy summary"}],
        )

        assert count == 1
        assert [
            row["content"] for row in peer.get_messages("session")
        ] == ["legacy summary"]
        assert _stage_sessions(peer) == []
    finally:
        writer.close()
        peer.close()


def test_staging_releases_writer_and_cutover_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "bounded-stage-test"
    release_worker = threading.Event()
    thread: threading.Thread | None = None
    try:
        assert writer.try_acquire_compression_lock(
            "session",
            holder,
            ttl_seconds=60.0,
        )
        old_contents = _seed(writer)
        watermark = writer.get_active_message_watermark("session")
        compacted = [
            {"role": "user", "content": "summary one"},
            {"role": "assistant", "content": "summary two"},
        ]

        monkeypatch.setattr(state_compaction, "_STAGE_MAX_ROWS", 1)
        monkeypatch.setattr(state_compaction, "_STAGE_MAX_BYTES", 1)
        first_chunk_committed = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        original = state_compaction._stage_chunk

        def _pause_after_first_chunk(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                first_chunk_committed.set()
                if not release_worker.wait(timeout=15):
                    raise TimeoutError("test did not release compactor")
            return result

        monkeypatch.setattr(
            state_compaction,
            "_stage_chunk",
            _pause_after_first_chunk,
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
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=_compact, daemon=True)
        thread.start()
        assert first_chunk_committed.wait(timeout=15)

        stages = _stage_sessions(peer)
        staged_rows = _stage_messages(peer)
        assert len(stages) == 1
        assert stages[0]["hidden"] == 1
        marker = json.loads(stages[0]["model_config"])
        assert marker[state_compaction._STAGE_MARKER_KEY][
            "target_session_id"
        ] == "session"
        assert len(staged_rows) == 1
        assert staged_rows[0]["content"] == "summary one"
        assert staged_rows[0]["active"] == 0
        assert staged_rows[0]["compacted"] == 0

        # Until cutover the complete old view is still authoritative.
        assert [
            row["content"] for row in peer.get_messages("session")
        ] == old_contents
        assert peer.get_session("session")["message_count"] == 3
        assert peer.search_messages("summary one") == []

        # The compactor is paused after a committed stage transaction.
        # A sibling process can take SQLite's writer slot immediately.
        peer.append_message(
            "session",
            role="assistant",
            content="concurrent tail",
        )
        assert [
            row["content"] for row in peer.get_messages("session")
        ] == [*old_contents, "concurrent tail"]

        release_worker.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert failures == []
        assert result == [3]
        assert [
            row["content"] for row in writer.get_messages("session")
        ] == ["summary one", "summary two", "concurrent tail"]
        assert writer.get_session("session")["message_count"] == 3
        assert _stage_sessions(writer) == []
        assert _stage_messages(writer) == []
    finally:
        release_worker.set()
        if thread is not None:
            thread.join(timeout=20)
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_cutover_failure_keeps_old_view_and_cleans_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, peer = _open_pair(tmp_path)
    holder = "cutover-failure-test"
    try:
        assert writer.try_acquire_compression_lock(
            "session",
            holder,
            ttl_seconds=60.0,
        )
        old_contents = _seed(writer)
        compacted = [
            {"role": "user", "content": "never published"},
            {"role": "assistant", "content": "still hidden"},
        ]

        def _fail_cutover(*args, **kwargs):
            raise RuntimeError("injected cutover failure")

        monkeypatch.setattr(
            state_compaction,
            "_commit_stage",
            _fail_cutover,
        )
        with pytest.raises(
            RuntimeError,
            match="injected cutover failure",
        ):
            writer.archive_and_compact(
                "session",
                compacted,
                lock_holder=holder,
            )

        assert [
            row["content"] for row in peer.get_messages("session")
        ] == old_contents
        assert peer.get_session("session")["message_count"] == 3
        assert _stage_sessions(peer) == []
        assert _stage_messages(peer) == []
        assert all("_row_id" not in message for message in compacted)
    finally:
        writer.release_compression_lock("session", holder)
        writer.close()
        peer.close()


def test_proven_holder_reclaims_crash_residue(tmp_path: Path) -> None:
    writer, peer = _open_pair(tmp_path)
    old_holder = "crashed-stage-holder"
    new_holder = "replacement-stage-holder"
    stale_stage_id = "_hcmp_stale_test"
    try:
        _seed(writer)
        assert writer.try_acquire_compression_lock(
            "session",
            old_holder,
            ttl_seconds=60.0,
        )
        state_compaction._create_stage_session(
            writer,
            "session",
            stale_stage_id,
            old_holder,
        )
        orphan = {"role": "user", "content": "orphaned stage"}
        state_compaction._stage_chunk(
            writer,
            "session",
            stale_stage_id,
            old_holder,
            [orphan],
        )
        assert len(_stage_sessions(peer)) == 1
        assert len(_stage_messages(peer)) == 1

        writer.release_compression_lock("session", old_holder)
        assert writer.try_acquire_compression_lock(
            "session",
            new_holder,
            ttl_seconds=60.0,
        )
        count = writer.archive_and_compact(
            "session",
            [{"role": "user", "content": "fresh summary"}],
            lock_holder=new_holder,
        )

        assert count == 1
        assert [
            row["content"] for row in writer.get_messages("session")
        ] == ["fresh summary"]
        assert _stage_sessions(writer) == []
        assert _stage_messages(writer) == []
        all_rows = writer.get_messages(
            "session",
            include_inactive=True,
        )
        assert all(
            row["content"] != "orphaned stage" for row in all_rows
        )
    finally:
        writer.release_compression_lock("session", old_holder)
        writer.release_compression_lock("session", new_holder)
        writer.close()
        peer.close()
