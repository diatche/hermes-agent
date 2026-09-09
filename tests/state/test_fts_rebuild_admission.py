"""Cross-process admission for full structural FTS rebuilds (PR #93200 class).

Several independent Hermes processes routinely share one state.db (gateway,
Desktop's ``hermes serve`` backend, CLI sessions, the TUI slash worker). Two
of them detecting FTS corruption at once each ran the full FTS5 'rebuild' on
the same file in parallel, colliding on write and structurally corrupting
state.db (two documented production incidents, 2026-08-15 and 2026-08-23).

The fix: every full structural rebuild entry point — ``rebuild_fts()``, the
``_init_schema`` trigger-repair rebuilds, and ``_recover_stale_fts`` — admits
through one cross-process file lock (``fts_rebuild_admission`` in
hermes_state_common) and FAILS CLOSED: a process that cannot acquire the
authority defers the rebuild instead of racing the holder. These tests use
real spawned processes holding the real lock file, per the review contract
on PR #93200 — the bug is cross-process ownership, so monkeypatched helpers
prove nothing.
"""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

import hermes_state_common
from hermes_state import FTS_STALE_KEY, SessionDB, _FTS_TRIGGERS

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX flock child-process harness"
)


_HOLD_LOCK_SCRIPT = """
import sys, time, fcntl, pathlib
lock_path = pathlib.Path({lock!r})
handle = lock_path.open("a+b")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep({hold})
"""


def _lock_file(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".fts_rebuild.lock")


@contextlib.contextmanager
def _rebuild_lock_held_by_other_process(db_path: Path, hold_seconds: float = 30.0):
    """Hold the FTS rebuild authority for *db_path* in a real child process."""
    script = _HOLD_LOCK_SCRIPT.format(
        lock=str(_lock_file(db_path)), hold=hold_seconds
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _fts_docsize_count(db_path: Path) -> int:
    raw = sqlite3.connect(str(db_path))
    try:
        return raw.execute("SELECT count(*) FROM messages_fts_docsize").fetchone()[0]
    finally:
        raw.close()


def _base_fts_triggers(db_path: Path) -> set:
    raw = sqlite3.connect(str(db_path))
    try:
        rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            f"AND name IN ({','.join('?' for _ in _FTS_TRIGGERS)})",
            _FTS_TRIGGERS,
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        raw.close()


def _meta_value(db_path: Path, key: str):
    raw = sqlite3.connect(str(db_path))
    try:
        row = raw.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        raw.close()


@pytest.fixture
def fast_timeout(monkeypatch):
    monkeypatch.setattr(
        hermes_state_common, "_FTS_REBUILD_LOCK_TIMEOUT_SECONDS", 0.5
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(d, "_foreign_state_db_holders", lambda: [])
    if not d._fts_enabled:
        d.close()
        pytest.skip("FTS5 unavailable in this build")
    d.create_session("s1", source="test")
    for i in range(5):
        d.append_message("s1", "user", f"hello world {i}")
    yield d
    try:
        d.close()
    except Exception:
        pass


class TestRebuildFtsAdmission:
    def test_late_same_process_opener_is_refused_for_complete_window(self, db):
        from hermes_cli.sqlite_safe_read import connect_tracked

        with db._structural_maintenance_guard():
            with pytest.raises(sqlite3.OperationalError, match="reserved"):
                connect_tracked(db.db_path)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    db.append_message, "s1", "user", "late write"
                )
                with pytest.raises(sqlite3.OperationalError, match="reserved"):
                    future.result()

    def test_nested_maintenance_is_rejected_from_non_owner_thread(self, db):
        with db._structural_maintenance_guard():
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    lambda: db._structural_maintenance_guard().__enter__()
                )
                with pytest.raises(RuntimeError, match="owned by another thread"):
                    future.result()

    @pytest.mark.live_system_guard_bypass
    def test_rebuild_defers_while_another_process_holds_authority(
        self, db, fast_timeout
    ):
        """Fail closed: the contender must NOT rebuild while the lock is held."""
        with _rebuild_lock_held_by_other_process(db.db_path):
            assert db.rebuild_fts() == 0

    @pytest.mark.live_system_guard_bypass
    def test_rebuild_proceeds_after_holder_releases(self, db, fast_timeout):
        with _rebuild_lock_held_by_other_process(db.db_path):
            assert db.rebuild_fts() == 0
        # Holder killed on context exit → kernel drops the flock → the next
        # caller acquires the authority and the rebuild really runs.
        assert db.rebuild_fts() >= 1

    @pytest.mark.live_system_guard_bypass
    def test_rebuild_waits_out_a_short_holder(self, db, monkeypatch):
        """A holder that releases within the bounded wait does not cause deferral."""
        monkeypatch.setattr(
            hermes_state_common, "_FTS_REBUILD_LOCK_TIMEOUT_SECONDS", 10.0
        )
        with _rebuild_lock_held_by_other_process(db.db_path, hold_seconds=1.0):
            # Child exits after 1s; deadline is 10s — this must acquire and rebuild.
            assert db.rebuild_fts() >= 1

    def test_admission_yields_true_for_pathless_db(self):
        """In-memory / pathless stores have no cross-process surface."""
        with hermes_state_common.fts_rebuild_admission(None) as admitted:
            assert admitted is True


class TestSchemaPathAdmission:
    @pytest.mark.parametrize("version", [0, 1, 25])
    def test_missing_trigger_refusal_is_independent_of_schema_version(
        self, tmp_path, version
    ):
        db_path = tmp_path / f"state-{version}.db"
        d = SessionDB(db_path=db_path)
        if not d._fts_enabled:
            d.close()
            pytest.skip("FTS5 unavailable in this build")
        d.create_session("s1", source="test")
        d.append_message("s1", "user", "canonical row")
        d.close()
        raw = sqlite3.connect(db_path)
        raw.execute("DROP TRIGGER messages_fts_delete")
        raw.execute("UPDATE schema_version SET version=?", (version,))
        raw.commit()
        raw.close()

        with pytest.raises(sqlite3.DatabaseError, match="refusing automatic"):
            SessionDB(db_path=db_path)

    def test_startup_trigger_repair_fails_closed_without_schema_ddl(
        self, tmp_path
    ):
        """Startup must not recreate triggers or rebuild populated FTS."""
        db_path = tmp_path / "state.db"
        d = SessionDB(db_path=db_path)
        if not d._fts_enabled:
            d.close()
            pytest.skip("FTS5 unavailable in this build")
        d.create_session("s1", source="test")
        d.append_message("s1", "user", "hello schema path")
        d.close()

        # Drop one sync trigger out-of-band: next open takes the
        # triggers_need_repair branch in _init_schema.
        raw = sqlite3.connect(str(db_path))
        raw.execute(f"DROP TRIGGER IF EXISTS {sorted(_FTS_TRIGGERS)[0]}")
        raw.commit()
        raw.close()

        with pytest.raises(sqlite3.DatabaseError, match="refusing automatic"):
            SessionDB(db_path=db_path)

        assert _meta_value(db_path, FTS_STALE_KEY) is None
        assert len(_base_fts_triggers(db_path)) == len(_FTS_TRIGGERS) - 1

    def test_stale_recovery_remains_detached_until_explicit_repair(
        self, tmp_path
    ):
        """Startup observes stale state but never performs structural repair."""
        db_path = tmp_path / "state.db"
        d = SessionDB(db_path=db_path)
        if not d._fts_enabled:
            d.close()
            pytest.skip("FTS5 unavailable in this build")
        d.create_session("s1", source="test")
        d.append_message("s1", "user", "hello recovery path")
        d.close()

        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (FTS_STALE_KEY,),
        )
        for trig in _FTS_TRIGGERS:
            raw.execute(f"DROP TRIGGER IF EXISTS {trig}")
        raw.commit()
        raw.close()

        d2 = SessionDB(db_path=db_path)
        try:
            assert d2._fts_enabled is False
        finally:
            d2.close()
        assert _meta_value(db_path, FTS_STALE_KEY) == "1"
        assert _base_fts_triggers(db_path) == set()
