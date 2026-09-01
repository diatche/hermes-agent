"""Bounded, crash-recoverable publication for in-place compaction.

The historical ``SessionDB.archive_and_compact`` transaction did four jobs
while holding SQLite's single writer slot: archive the old transcript,
serialize the replacement rows, feed every replacement through both FTS
indexes, and publish the new counters.  On large long-lived databases that
writer hold reached 25-140 seconds, long enough for unrelated gateway turns
to exhaust transcript-write patience and stop with "session storage was
busy" (#90173).

Lease-backed replacement rows are now built under a hidden staging session in
bounded transactions.  The old session remains fully authoritative while those
rows are serialized and indexed.  A final lease-fenced transaction only
changes visibility: archive the old active rows, move the complete staged
set onto the real session, clone the post-watermark concurrent tail, update
counters/config, and remove the stage session.  Readers therefore observe
either the complete old transcript or the complete compacted transcript,
never a partial replacement.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

logger = logging.getLogger("hermes_state")

# Bound by both row count and serialized payload.  One oversized message is
# necessarily indivisible, but surrounding rows still get writer-release
# boundaries instead of sharing one unbounded transaction.
_STAGE_MAX_ROWS = 32
_STAGE_MAX_BYTES = 512 * 1024
_CLEANUP_MAX_ROWS = 32
_SQLITE_ID_CHUNK = 800

_STAGE_SOURCE = "_compaction_stage"
_STAGE_MARKER_KEY = "_hermes_compaction_stage"


def _compression_in_progress_error(message: str) -> BaseException:
    # Runtime import: hermes_state is still assembling SessionDB when this
    # module is imported through hermes_state_schema.
    from hermes_state import SessionCompressionInProgressError

    return SessionCompressionInProgressError(message)


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _verify_lock(
    conn: Any,
    session_id: str,
    lock_holder: str | None,
) -> None:
    """Reproduce the historical commit-fence verdict inside a write txn."""
    if lock_holder is None:
        return
    lock_row = conn.execute(
        "SELECT holder, expires_at FROM compression_locks "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if (
        lock_row is None
        or _row_value(lock_row, "holder", 0) != lock_holder
        or float(_row_value(lock_row, "expires_at", 1)) <= time.time()
    ):
        raise _compression_in_progress_error(
            f"Compression lease for {session_id!r} lost before "
            "commit; refusing to publish a stale compaction"
        )


def _message_size(message: Any) -> int:
    try:
        raw = json.dumps(
            message,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception:
        raw = str(message).encode("utf-8", "replace")
    return max(1, len(raw))


def _stage_chunks(
    messages: Sequence[dict[str, Any]],
) -> Iterator[list[dict[str, Any]]]:
    max_rows = max(1, int(_STAGE_MAX_ROWS))
    max_bytes = max(1, int(_STAGE_MAX_BYTES))
    chunk: list[dict[str, Any]] = []
    chunk_bytes = 0

    for message in messages:
        size = _message_size(message)
        if chunk and (
            len(chunk) >= max_rows or chunk_bytes + size > max_bytes
        ):
            yield chunk
            chunk = []
            chunk_bytes = 0
        chunk.append(message)
        chunk_bytes += size
        if len(chunk) >= max_rows or chunk_bytes >= max_bytes:
            yield chunk
            chunk = []
            chunk_bytes = 0

    if chunk:
        yield chunk


def _id_chunks(ids: Sequence[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), _SQLITE_ID_CHUNK):
        yield list(ids[start : start + _SQLITE_ID_CHUNK])


def _stage_metadata(
    target_session_id: str,
    lock_holder: str | None,
) -> str:
    return json.dumps(
        {
            _STAGE_MARKER_KEY: {
                "target_session_id": target_session_id,
                "lock_holder": lock_holder,
            }
        },
        separators=(",", ":"),
    )


def _create_stage_session(
    db: Any,
    target_session_id: str,
    stage_session_id: str,
    lock_holder: str | None,
) -> None:
    def _create(conn: Any) -> None:
        _verify_lock(conn, target_session_id, lock_holder)
        target = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?",
            (target_session_id,),
        ).fetchone()
        if target is None:
            raise RuntimeError(
                f"Compaction target session {target_session_id!r} is missing"
            )
        conn.execute(
            """INSERT INTO sessions (
                   id, source, started_at, model_config, hidden
               ) VALUES (?, ?, ?, ?, 1)""",
            (
                stage_session_id,
                _STAGE_SOURCE,
                time.time(),
                _stage_metadata(target_session_id, lock_holder),
            ),
        )

    db._execute_write(_create)


def _cleanup_stage(
    db: Any,
    stage_session_id: str,
    *,
    target_session_id: str | None = None,
    lock_holder: str | None = None,
) -> int:
    """Delete one hidden stage in bounded transactions.

    Exact-stage failure cleanup needs no target lease: the random stage id
    is private to the failed call.  Stale-stage reclamation supplies a
    target/holder pair, so every cleanup transaction revalidates the current
    compression owner before deleting residue from a prior owner.
    """
    deleted_total = 0
    while True:
        def _delete_step(conn: Any) -> tuple[int, bool]:
            if target_session_id is not None:
                _verify_lock(conn, target_session_id, lock_holder)
            rows = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? "
                "ORDER BY id LIMIT ?",
                (stage_session_id, _CLEANUP_MAX_ROWS),
            ).fetchall()
            ids = [int(_row_value(row, "id", 0)) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",
                    ids,
                )
                return len(ids), True

            conn.execute(
                "DELETE FROM sessions WHERE id = ? AND source = ? "
                "AND hidden = 1",
                (stage_session_id, _STAGE_SOURCE),
            )
            return 0, False

        deleted, more = db._execute_write(_delete_step)
        deleted_total += int(deleted)
        if not more:
            return deleted_total


def _reclaim_stale_stages(
    db: Any,
    target_session_id: str,
    lock_holder: str | None,
) -> int:
    """Reclaim prior holder-backed stages once ownership is proven.

    Only the lease-backed batch path stages rows.  A valid current holder
    therefore proves every older stage for this target is abandoned.
    """
    if lock_holder is None:
        return 0

    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions "
            "WHERE source = ? AND hidden = 1 "
            "AND json_extract(model_config, ?) = ? "
            "AND json_type(model_config, ?) = 'text'",
            (
                _STAGE_SOURCE,
                f"$.{_STAGE_MARKER_KEY}.target_session_id",
                target_session_id,
                f"$.{_STAGE_MARKER_KEY}.lock_holder",
            ),
        ).fetchall()
    stage_ids = [str(_row_value(row, "id", 0)) for row in rows]
    deleted = 0
    for stage_id in stage_ids:
        deleted += _cleanup_stage(
            db,
            stage_id,
            target_session_id=target_session_id,
            lock_holder=lock_holder,
        )
    return deleted


def _stage_chunk(
    db: Any,
    target_session_id: str,
    stage_session_id: str,
    lock_holder: str | None,
    chunk: list[dict[str, Any]],
) -> tuple[int, int, list[int], float]:
    started = time.monotonic()

    def _insert(conn: Any) -> tuple[int, int, list[int]]:
        _verify_lock(conn, target_session_id, lock_holder)
        inserted, tool_calls = db._insert_message_rows(
            conn,
            stage_session_id,
            chunk,
        )
        rows = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (stage_session_id, int(inserted)),
        ).fetchall()
        row_ids = sorted(int(_row_value(row, "id", 0)) for row in rows)
        if len(row_ids) != inserted:
            raise RuntimeError(
                "staged compaction row capture did not match inserts: "
                f"expected={inserted}, captured={len(row_ids)}"
            )
        if row_ids:
            for ids in _id_chunks(row_ids):
                placeholders = ",".join("?" for _ in ids)
                cursor = conn.execute(
                    "UPDATE messages SET active = 0, compacted = 0 "
                    f"WHERE session_id = ? AND id IN ({placeholders}) "
                    "AND active = 1",
                    (stage_session_id, *ids),
                )
                changed = cursor.rowcount
                if changed is None or changed < 0:
                    changed = conn.execute("SELECT changes()").fetchone()[0]
                if int(changed) != len(ids):
                    raise RuntimeError(
                        "staged compaction visibility flip was incomplete: "
                        f"expected={len(ids)}, changed={changed}"
                    )
        return inserted, tool_calls, row_ids

    inserted, tool_calls, row_ids = db._execute_write(_insert)
    return (
        int(inserted),
        int(tool_calls),
        list(row_ids),
        time.monotonic() - started,
    )


def _tool_call_count(raw: Any) -> int:
    if not raw:
        return 0
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return 0
    return len(parsed) if isinstance(parsed, list) else 0


def _commit_stage(
    db: Any,
    target_session_id: str,
    stage_session_id: str,
    lock_holder: str | None,
    *,
    stage_message_count: int,
    stage_tool_call_count: int,
    model_config_patch: dict[str, Any] | None,
    watermark: int | None,
    tail_count: int,
) -> tuple[int, float]:
    started = time.monotonic()

    def _commit(conn: Any) -> int:
        _verify_lock(conn, target_session_id, lock_holder)

        target = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?",
            (target_session_id,),
        ).fetchone()
        if target is None:
            raise RuntimeError(
                f"Compaction target session {target_session_id!r} is missing"
            )
        stage = conn.execute(
            "SELECT 1 FROM sessions "
            "WHERE id = ? AND source = ? AND hidden = 1 "
            "AND json_extract(model_config, ?) = ?",
            (
                stage_session_id,
                _STAGE_SOURCE,
                f"$.{_STAGE_MARKER_KEY}.target_session_id",
                target_session_id,
            ),
        ).fetchone()
        if stage is None:
            raise RuntimeError("compaction stage session disappeared")
        stage_rows = conn.execute(
            "SELECT id, active, compacted FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (stage_session_id,),
        ).fetchall()
        if len(stage_rows) != stage_message_count:
            raise RuntimeError(
                "staged compaction set changed before cutover: "
                f"expected={stage_message_count}, found={len(stage_rows)}"
            )
        if any(
            bool(_row_value(row, "active", 1))
            or bool(_row_value(row, "compacted", 2))
            for row in stage_rows
        ):
            raise RuntimeError(
                "staged compaction rows became visible before cutover"
            )

        patched_model_config = None
        if model_config_patch is not None:
            patched_model_config = db._merge_model_config_json(
                conn,
                target_session_id,
                model_config_patch,
                on_missing="raise",
            )

        tail_ids: list[int] = []
        tail_tool_calls = 0
        if watermark is not None:
            rows = conn.execute(
                "SELECT id, tool_calls FROM messages "
                "WHERE session_id = ? AND active = 1 AND id > ? "
                "ORDER BY id",
                (target_session_id, int(watermark)),
            ).fetchall()
            for row in rows:
                tail_ids.append(int(_row_value(row, "id", 0)))
                tail_tool_calls += _tool_call_count(
                    _row_value(row, "tool_calls", 1)
                )

        rewind_tail_ids: list[int] = []
        if tail_count > 0:
            if watermark is None:
                rows = conn.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND active = 1 "
                    "ORDER BY id DESC LIMIT ?",
                    (target_session_id, int(tail_count)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND active = 1 AND id <= ? "
                    "ORDER BY id DESC LIMIT ?",
                    (
                        target_session_id,
                        int(watermark),
                        int(tail_count),
                    ),
                ).fetchall()
            rewind_tail_ids = [
                int(_row_value(row, "id", 0)) for row in rows
            ]

        # Visibility-only cutover.  The replacement content has already
        # paid its serialization/FTS cost in the stage transactions.
        conn.execute(
            "UPDATE messages SET active = 0, compacted = 1 "
            "WHERE session_id = ? AND active = 1",
            (target_session_id,),
        )
        rewind_ids = [*rewind_tail_ids, *tail_ids]
        for ids in _id_chunks(rewind_ids):
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                "UPDATE messages SET compacted = 0 "
                f"WHERE session_id = ? AND id IN ({placeholders})",
                (target_session_id, *ids),
            )

        cursor = conn.execute(
            "UPDATE messages SET session_id = ?, active = 1, compacted = 0 "
            "WHERE session_id = ? AND active = 0 AND compacted = 0",
            (target_session_id, stage_session_id),
        )
        activated = cursor.rowcount
        if activated is None or activated < 0:
            activated = conn.execute("SELECT changes()").fetchone()[0]
        if int(activated) != stage_message_count:
            raise RuntimeError(
                "staged compaction activation was incomplete: "
                f"expected={stage_message_count}, activated={activated}"
            )

        conn.execute(
            "DELETE FROM sessions WHERE id = ? AND source = ? "
            "AND hidden = 1",
            (stage_session_id, _STAGE_SOURCE),
        )

        inserted = stage_message_count
        tool_calls_total = stage_tool_call_count
        if tail_ids:
            clone_cols = [
                column
                for column in db._message_column_names(conn)
                if column not in ("id", "active", "compacted")
            ]
            col_list = ", ".join(clone_cols)
            for ids in _id_chunks(tail_ids):
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"INSERT INTO messages ({col_list}, active, compacted) "
                    f"SELECT {col_list}, 1, 0 FROM messages "
                    f"WHERE id IN ({placeholders}) ORDER BY id",
                    ids,
                )
            inserted += len(tail_ids)
            tool_calls_total += tail_tool_calls

        if model_config_patch is None:
            conn.execute(
                "UPDATE sessions SET message_count = ?, "
                "tool_call_count = ? WHERE id = ?",
                (
                    inserted,
                    tool_calls_total,
                    target_session_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE sessions SET message_count = ?, "
                "tool_call_count = ?, model_config = ? WHERE id = ?",
                (
                    inserted,
                    tool_calls_total,
                    patched_model_config,
                    target_session_id,
                ),
            )
        return inserted

    inserted = int(db._execute_write(_commit))
    return inserted, time.monotonic() - started


def archive_and_compact(
    self: Any,
    session_id: str,
    compacted_messages: list[dict[str, Any]],
    model_config_patch: dict[str, Any] | None = None,
    watermark: int | None = None,
    lock_holder: str | None = None,
    tail_count: int = 0,
) -> int:
    """Publish compaction via bounded hidden staging and an atomic cutover.

    Signature and durable semantics match the historical SessionDB method.
    ``lock_holder`` remains caller-owned: this method validates it but never
    acquires or releases it, so the surrounding compressor's lease lifetime
    and refresher are unchanged.

    Calls without a holder keep the historical single-transaction method.
    Those are the micro/prune call sites covered separately by #77861 and
    #80245; widening them into a multi-transaction protocol here would
    duplicate those lanes and create a stale-snapshot window without
    pre-summary ownership.
    """
    if lock_holder is None:
        legacy_kwargs = {
            "model_config_patch": model_config_patch,
            "watermark": watermark,
            "lock_holder": None,
        }
        # Older carried branches predate tail_count. Preserve their exact
        # no-holder path when no rewind-tail behavior was requested.
        if tail_count:
            legacy_kwargs["tail_count"] = tail_count
        return self._archive_and_compact_legacy(
            session_id,
            compacted_messages,
            **legacy_kwargs,
        )

    stage_session_id = f"_hcmp_{uuid.uuid4().hex}"
    previous_row_ids: dict[int, tuple[dict[str, Any], bool, Any]] = {}
    for message in compacted_messages:
        if not isinstance(message, dict):
            continue
        identity = id(message)
        if identity not in previous_row_ids:
            previous_row_ids[identity] = (
                message,
                "_row_id" in message,
                message.get("_row_id"),
            )

    stage_durations: list[float] = []
    stage_message_count = 0
    stage_tool_call_count = 0
    stale_rows_reclaimed = 0
    started = time.monotonic()
    stage_created = False

    try:
        stale_rows_reclaimed = _reclaim_stale_stages(
            self,
            session_id,
            lock_holder,
        )
        _create_stage_session(
            self,
            session_id,
            stage_session_id,
            lock_holder,
        )
        stage_created = True

        for chunk in _stage_chunks(compacted_messages):
            (
                inserted,
                tool_calls,
                _row_ids,
                elapsed,
            ) = _stage_chunk(
                self,
                session_id,
                stage_session_id,
                lock_holder,
                chunk,
            )
            stage_message_count += inserted
            stage_tool_call_count += tool_calls
            stage_durations.append(elapsed)

        inserted, cutover_elapsed = _commit_stage(
            self,
            session_id,
            stage_session_id,
            lock_holder,
            stage_message_count=stage_message_count,
            stage_tool_call_count=stage_tool_call_count,
            model_config_patch=model_config_patch,
            watermark=watermark,
            tail_count=tail_count,
        )
        logger.info(
            "state.db compaction published: session=%s rows=%d "
            "stage_transactions=%d max_stage_seconds=%.3f "
            "cutover_seconds=%.3f total_seconds=%.3f "
            "stale_stage_rows_reclaimed=%d",
            session_id,
            inserted,
            len(stage_durations),
            max(stage_durations, default=0.0),
            cutover_elapsed,
            time.monotonic() - started,
            stale_rows_reclaimed,
        )
        return inserted
    except BaseException:
        if stage_created:
            try:
                _cleanup_stage(self, stage_session_id)
            except Exception:
                # Fail-safe residue stays under a hidden internal session
                # with active=0/compacted=0 rows.  It cannot enter live
                # context or default search, and a later proven lock owner
                # reclaims it before staging.
                logger.error(
                    "Could not remove abandoned compaction stage %s for %s",
                    stage_session_id,
                    session_id,
                    exc_info=True,
                )
        for message, had_row_id, row_id in previous_row_ids.values():
            if had_row_id:
                message["_row_id"] = row_id
            else:
                message.pop("_row_id", None)
        raise
