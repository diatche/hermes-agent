"""Runtime FTS-corruption safety on the SessionDB read/write paths.

A corrupted FTS5 shadow table (``messages_fts_data``) makes every message
write raise ``sqlite3.DatabaseError: database disk image is malformed``
through the FTS sync triggers, while the canonical ``messages`` rows stay
intact. Before this fix the gateway swallowed the failure at debug level and
the in-memory session advanced while disk silently fell behind — surfacing
later as "Persisted transcript lagged live cached history" amnesia.

Runtime writes fail closed without rebuilding or changing schema. FTS reads
degrade in memory to canonical LIKE search. Structural recovery is reserved
for explicit offline maintenance.
"""

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import hermes_state
import hermes_state_schema
from hermes_state import (
    FTS_REBUILD_DEFERRAL_KEY,
    FTS_STALE_KEY,
    LEGACY_FTS_SQL,
    LEGACY_FTS_TRIGRAM_SQL,
    SCHEMA_SQL,
    SessionDB,
    _FTS_TRIGGERS,
    _concrete_state_db_holder_pids,
    _is_inactive_orphan_desktop_holder,
    repair_state_db_schema,
)


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _corrupt_fts(db_path):
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    raw.commit()
    raw.close()


def _corrupt_trigram_fts(db_path):
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "UPDATE messages_fts_trigram_data "
        "SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    raw.commit()
    raw.close()


def _message_contents(db_path):
    raw = sqlite3.connect(str(db_path))
    rows = raw.execute("SELECT content FROM messages ORDER BY id").fetchall()
    raw.close()
    return [r[0] for r in rows]


def _meta_value(db_path, key):
    raw = sqlite3.connect(str(db_path))
    row = raw.execute(
        "SELECT value FROM state_meta WHERE key = ?", (key,)
    ).fetchone()
    raw.close()
    return None if row is None else row[0]


def _base_fts_triggers(db_path):
    raw = sqlite3.connect(str(db_path))
    rows = raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        f"AND name IN ({','.join('?' for _ in _FTS_TRIGGERS)})",
        _FTS_TRIGGERS,
    ).fetchall()
    raw.close()
    return {row[0] for row in rows}


class TestRuntimeFtsRebuild:
    def test_reap_candidates_exclude_uninspectable_holder_suspicions(
        self, tmp_path
    ):
        db_path = tmp_path / "state.db"

        assert _concrete_state_db_holder_pids(
            db_path,
            [
                (222, "uninspectable holder: python -m hermes_cli.main serve --port 0"),
                (-1, "open-file scan failed"),
            ],
        ) == []

    def test_reap_candidates_deduplicate_multiple_proven_watched_fds(self, tmp_path):
        db_path = tmp_path / "state.db"

        assert _concrete_state_db_holder_pids(
            db_path,
            [
                (222, str(db_path)),
                (222, f"{db_path}-wal"),
                (222, f"{db_path}-shm (deleted)"),
            ],
        ) == [222]

    def test_inactive_orphan_reap_predicate_preserves_live_or_ambiguous_holders(self):
        common = {
            "ppid": 1,
            "age_seconds": 120.0,
            "min_age_seconds": 60.0,
            "ephemeral_backend": True,
            "connection_statuses": [],
        }
        assert _is_inactive_orphan_desktop_holder(**common)
        assert not _is_inactive_orphan_desktop_holder(**{**common, "ppid": 42})
        assert not _is_inactive_orphan_desktop_holder(
            **{**common, "age_seconds": 10.0}
        )
        assert not _is_inactive_orphan_desktop_holder(
            **{**common, "ephemeral_backend": False}
        )
        assert not _is_inactive_orphan_desktop_holder(
            **{
                **common,
                "connection_statuses": ["ESTABLISHED"],
            }
        )

    def test_foreign_holder_detection_includes_deleted_wal(
        self, db, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "state.db"

        class FakePsutil:
            @staticmethod
            def process_iter(_attrs):
                return iter(
                    (
                        SimpleNamespace(
                            info={
                                "pid": 111,
                                "open_files": [SimpleNamespace(path=str(db_path))],
                            }
                        ),
                        SimpleNamespace(
                            info={
                                "pid": 222,
                                "open_files": [
                                    SimpleNamespace(path=f"{db_path}-wal (deleted)")
                                ],
                            }
                        ),
                        SimpleNamespace(
                            info={
                                "pid": 333,
                                "open_files": [SimpleNamespace(path=str(tmp_path / "other.db"))],
                            }
                        ),
                    )
                )

        monkeypatch.setattr(hermes_state, "psutil", FakePsutil)
        monkeypatch.setattr(hermes_state, "_IS_WINDOWS", False)
        monkeypatch.setattr(hermes_state.os, "getpid", lambda: 111)
        # Force the macOS/psutil path even on Linux test runners
        monkeypatch.setattr(hermes_state.sys, "platform", "darwin")

        assert db._foreign_state_db_holders() == [
            (222, f"{db_path}-wal (deleted)")
        ]

    def test_foreign_holder_detection_proc_readlink_deleted_wal(
        self, db, tmp_path, monkeypatch
    ):
        """Linux /proc/<pid>/fd readlinks preserve '(deleted)' suffix.

        psutil.open_files() drops these entries (isfile_strict stats the
        literal path and fails).  The /proc path catches the split-brain
        holder that psutil silently misses.
        """
        db_path = tmp_path / "state.db"
        db_path_wal = str(db_path) + "-wal"

        # Build a fake /proc with two PIDs: self (111) and foreign (222).
        proc_root = tmp_path / "proc"
        for pid in (111, 222, 333):
            fd_dir = proc_root / str(pid) / "fd"
            fd_dir.mkdir(parents=True)
        # PID 222 holds the deleted WAL sidecar
        os.symlink(db_path_wal + " (deleted)", str(proc_root / "222" / "fd" / "3"))
        # PID 111 (self) holds the db — should be excluded
        os.symlink(str(db_path), str(proc_root / "111" / "fd" / "3"))
        # PID 333 holds an unrelated file
        other = tmp_path / "other.db"
        other.touch()
        os.symlink(str(other), str(proc_root / "333" / "fd" / "3"))

        monkeypatch.setattr(hermes_state, "_IS_WINDOWS", False)
        monkeypatch.setattr(hermes_state.os, "getpid", lambda: 111)
        monkeypatch.setattr(hermes_state.sys, "platform", "linux")
        real_listdir = os.listdir
        def _listdir(path):
            if isinstance(path, str):
                path = path.replace("/proc", str(proc_root))
            return real_listdir(path)
        monkeypatch.setattr(hermes_state.os, "listdir", _listdir)
        real_readlink = os.readlink
        def _readlink(path):
            path = path.replace("/proc", str(proc_root))
            return real_readlink(path)
        monkeypatch.setattr(hermes_state.os, "readlink", _readlink)

        holders = db._foreign_state_db_holders()
        assert holders == [(222, db_path_wal + " (deleted)")]

    def test_foreign_holder_uninspectable_process_cmdline_fallback(
        self, db, tmp_path, monkeypatch
    ):
        """A process whose fd table is unreadable (different user) is still
        flagged when /proc/<pid>/cmdline identifies it as a Hermes process."""
        db_path = tmp_path / "state.db"

        proc_root = tmp_path / "proc"
        for pid in (111, 222):
            (proc_root / str(pid) / "fd").mkdir(parents=True)
        # PID 222's fd dir is unreadable (PermissionError)
        os.chmod(proc_root / "222" / "fd", 0o000)
        # PID 222's cmdline is world-readable and looks like Hermes
        cmdline_path = proc_root / "222" / "cmdline"
        cmdline_path.write_bytes(b"python3\x00hermes_cli.main\x00chat\x00")

        monkeypatch.setattr(hermes_state, "_IS_WINDOWS", False)
        monkeypatch.setattr(hermes_state.os, "getpid", lambda: 111)
        monkeypatch.setattr(hermes_state.sys, "platform", "linux")
        real_listdir = os.listdir
        def _listdir(path):
            if isinstance(path, str):
                path = path.replace("/proc", str(proc_root))
            return real_listdir(path)
        monkeypatch.setattr(hermes_state.os, "listdir", _listdir)
        # _read_proc_cmdline opens /proc/<pid>/cmdline directly; redirect
        # it to our fake proc tree.
        def _fake_cmdline(pid):
            fake_path = str(proc_root / str(pid) / "cmdline")
            try:
                with open(fake_path, "rb") as f:
                    raw = f.read()
                if not raw:
                    return None
                return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            except OSError:
                return None
        monkeypatch.setattr(hermes_state, "_read_proc_cmdline", _fake_cmdline)

        holders = db._foreign_state_db_holders()
        # Should include PID 222 with the cmdline info
        assert len(holders) == 1
        assert holders[0][0] == 222
        assert "hermes_cli.main" in holders[0][1]

        # Cleanup
        os.chmod(proc_root / "222" / "fd", 0o755)

    def test_corruption_error_classification_requires_fts_evidence(self):
        """Generic structural corruption must not enter live FTS repair.

        Older SQLite builds may use the generic malformed-image text for an FTS
        virtual-table failure, but still expose SQLITE_CORRUPT_VTAB.  Preserve
        that route while failing closed for unscoped SQLITE_CORRUPT errors.
        """
        generic = sqlite3.DatabaseError("database disk image is malformed")
        assert not SessionDB._is_fts_write_corruption_error(generic)

        structural = sqlite3.DatabaseError("database disk image is malformed")
        structural.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        structural.sqlite_errorname = "SQLITE_CORRUPT"
        assert not SessionDB._is_fts_write_corruption_error(structural)

        fts_virtual_table = sqlite3.DatabaseError("database disk image is malformed")
        fts_virtual_table.sqlite_errorcode = sqlite3.SQLITE_CORRUPT_VTAB
        fts_virtual_table.sqlite_errorname = "SQLITE_CORRUPT_VTAB"
        assert SessionDB._is_fts_write_corruption_error(fts_virtual_table)

        contradictory = sqlite3.IntegrityError(
            'fts5: corrupt structure record for table "messages_fts"'
        )
        contradictory.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_TRIGGER
        contradictory.sqlite_errorname = "SQLITE_CONSTRAINT_TRIGGER"
        assert not SessionDB._is_fts_write_corruption_error(contradictory)

        assert SessionDB._is_fts_write_corruption_error(
            sqlite3.DatabaseError(
                'fts5: corrupt structure record for table "messages_fts"'
            )
        )
        assert not SessionDB._is_fts_write_corruption_error(
            sqlite3.DatabaseError("no such table: nothing_fts_related")
        )

    def test_structural_corruption_propagates_without_live_fts_mutation(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")

        rebuild_called = False

        def _unexpected_rebuild():
            nonlocal rebuild_called
            rebuild_called = True
            raise AssertionError("structural corruption must not rebuild FTS")

        monkeypatch.setattr(db, "rebuild_fts", _unexpected_rebuild)
        structural = sqlite3.DatabaseError("database disk image is malformed")
        structural.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        structural.sqlite_errorname = "SQLITE_CORRUPT"

        with pytest.raises(sqlite3.DatabaseError) as caught:
            db._execute_write(lambda _conn: (_ for _ in ()).throw(structural))

        assert caught.value is structural
        assert rebuild_called is False
        assert db._fts_stale is False
        assert _meta_value(tmp_path / "state.db", FTS_STALE_KEY) is None
        assert _base_fts_triggers(tmp_path / "state.db") == set(_FTS_TRIGGERS)

    def test_fts_looking_constraint_error_does_not_mutate_fts(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")

        rebuild_called = False

        def _unexpected_rebuild():
            nonlocal rebuild_called
            rebuild_called = True
            raise AssertionError("contradictory error code must fail closed")

        monkeypatch.setattr(db, "rebuild_fts", _unexpected_rebuild)
        contradictory = sqlite3.IntegrityError(
            'fts5: corrupt structure record for table "messages_fts"'
        )
        contradictory.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_TRIGGER
        contradictory.sqlite_errorname = "SQLITE_CONSTRAINT_TRIGGER"

        with pytest.raises(sqlite3.IntegrityError) as caught:
            db._execute_write(lambda _conn: (_ for _ in ()).throw(contradictory))

        assert caught.value is contradictory
        assert rebuild_called is False
        assert db._fts_stale is False
        assert _meta_value(tmp_path / "state.db", FTS_STALE_KEY) is None
        assert _base_fts_triggers(tmp_path / "state.db") == set(_FTS_TRIGGERS)

    def test_proven_fts_write_corruption_fails_closed_without_live_ddl(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "preserved seed")
        triggers_before = _base_fts_triggers(db_path)
        _corrupt_fts(db_path)

        rebuild_called = False
        drop_called = False

        def _unexpected_rebuild():
            nonlocal rebuild_called
            rebuild_called = True
            raise AssertionError("ordinary writes must not rebuild FTS")

        def _unexpected_drop(_cursor):
            nonlocal drop_called
            drop_called = True
            raise AssertionError("ordinary writes must not drop FTS triggers")

        monkeypatch.setattr(db, "rebuild_fts", _unexpected_rebuild)
        monkeypatch.setattr(db, "_drop_all_fts_triggers", _unexpected_drop)

        with pytest.raises(
            sqlite3.DatabaseError,
            match="fts5: corrupt structure|database disk image is malformed",
        ):
            db.append_message("s1", "user", "must not commit")

        assert rebuild_called is False
        assert drop_called is False
        assert _message_contents(db_path) == ["preserved seed"]
        assert _meta_value(db_path, FTS_STALE_KEY) is None
        assert _base_fts_triggers(db_path) == triggers_before

    def test_corrupt_fts_search_degrades_to_canonical_like_without_rebuild(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "canonical searchable needle")
        triggers_before = _base_fts_triggers(db_path)
        _corrupt_fts(db_path)

        monkeypatch.setattr(
            db,
            "rebuild_fts",
            lambda: (_ for _ in ()).throw(
                AssertionError("search must not rebuild FTS")
            ),
        )
        results = db.search_messages("searchable needle")

        assert results
        assert any("searchable needle" in row["snippet"] for row in results)
        assert db._fts_stale is True
        assert db._fts_enabled is False
        assert _meta_value(db_path, FTS_STALE_KEY) is None
        assert _base_fts_triggers(db_path) == triggers_before

    def test_first_cjk_corruption_fallback_preserves_boolean_semantics(
        self, db, tmp_path
    ):
        if not db._trigram_available:
            pytest.skip("trigram tokenizer unavailable in this build")
        db.create_session("cjk", source="test")
        db.append_message("cjk", "user", "大别山 keep")
        db.append_message("cjk", "user", "广西 keep")
        db.append_message("cjk", "user", "大别山 禁止")
        db._fts_cjk_available = False
        _corrupt_trigram_fts(tmp_path / "state.db")

        rows = db.search_messages("大别山 NOT 禁止 OR 广西", sort="oldest")
        snippets = [row["snippet"] for row in rows]
        assert len(rows) == 2
        assert any("大别山 keep" in snippet for snippet in snippets)
        assert any("广西 keep" in snippet for snippet in snippets)
        assert all("禁止" not in snippet for snippet in snippets)

    def test_same_process_peer_blocks_all_structural_work(self, db, tmp_path):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        peer = SessionDB(db_path=tmp_path / "state.db")
        try:
            with pytest.raises(RuntimeError, match="same-process connection"):
                db.rebuild_fts()
            with pytest.raises(RuntimeError, match="same-process connection"):
                db.vacuum()
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is False
            assert result["reason"] == (
                "structural_maintenance_requires_exclusive_access"
            )
            assert "same-process connection" in result["error"]

            repair = repair_state_db_schema(
                tmp_path / "state.db", backup=False
            )
            assert repair["repaired"] is False
            assert "open state.db connection" in repair["error"]
        finally:
            peer.close()

    # Historical self-heal tests below document the removed unsafe behavior.
    # They intentionally are not collected; the regression tests above assert
    # the replacement fail-closed contract.
    def obsolete_append_self_heals_after_fts_corruption(self, db, tmp_path):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello world")

        _corrupt_fts(tmp_path / "state.db")

        # Before the fix this raised DatabaseError and the row was lost.
        msg_id = db.append_message("s1", "user", "healed append")
        assert msg_id is not None
        assert _message_contents(tmp_path / "state.db") == [
            "hello world",
            "healed append",
        ]

    def obsolete_search_works_after_self_heal(self, db, tmp_path):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "before corruption")
        _corrupt_fts(tmp_path / "state.db")
        db.append_message("s1", "user", "searchable needle text")

        raw = sqlite3.connect(str(tmp_path / "state.db"))
        hits = raw.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'needle'"
        ).fetchall()
        raw.close()
        assert len(hits) == 1

    def obsolete_search_messages_self_heals_after_fts_corruption(self, db, tmp_path):
        """A read-only session that only SEARCHES (no write after corruption)
        must self-heal too. The MATCH read raises the corruption class
        (DatabaseError / 'fts5: corrupt structure record'), NOT the
        OperationalError that search_messages caught — so before this fix the
        search crashed until a write or restart rebuilt the index.
        """
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "a searchable needle here")

        _corrupt_fts(tmp_path / "state.db")
        # Injected via a raw connection, so no write on THIS instance has
        # consumed the one-shot rebuild yet.
        assert db._fts_runtime_rebuild_attempted is False

        results = db.search_messages("needle")

        assert db._fts_runtime_rebuild_attempted is True  # the search rebuilt it
        assert results  # non-empty: the rebuilt index matched the query
        assert any("needle" in (r.get("snippet") or "") for r in results)

    def obsolete_trigram_search_self_heals_after_fts_corruption(self, db, tmp_path):
        """The CJK/trigram MATCH branch has the same read-corruption exposure
        as the main FTS5 branch: it caught only OperationalError (query
        syntax), so a corrupt trigram shadow table raised DatabaseError
        straight out of search_messages. It must self-heal via the shared
        one-shot rebuild and answer from the rebuilt trigram index.
        """
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        if not db._trigram_available:
            pytest.skip("trigram tokenizer unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "关于大别山项目的进展报告")

        _corrupt_trigram_fts(tmp_path / "state.db")
        assert db._fts_runtime_rebuild_attempted is False

        # >=3 CJK chars per token → routed to the trigram branch.
        results = db.search_messages("大别山项目")

        assert db._fts_runtime_rebuild_attempted is True  # search rebuilt it
        assert results
        # The rebuilt trigram index answered (trigram snippets use >>> <<<),
        # i.e. we did not silently degrade to the LIKE fallback.
        assert any(">>>" in (r.get("snippet") or "") for r in results)


    def obsolete_second_corruption_fails_open_and_rebuilds_on_reopen(
        self, db, tmp_path
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)
        db.append_message("s1", "user", "first heal")  # consumes the one shot
        assert db._fts_runtime_rebuild_attempted is True

        # A second corruption must not strand the canonical transcript. The
        # derived indexes are detached and marked stale instead of looping.
        _corrupt_fts(db_path)
        db.append_message("s1", "user", "second corruption")
        assert _message_contents(db_path) == [
            "seed",
            "first heal",
            "second corruption",
        ]
        assert db._fts_stale is True
        assert _meta_value(db_path, FTS_STALE_KEY) == "1"
        assert _base_fts_triggers(db_path) == set()

        # Search remains available from canonical rows while FTS is stale.
        results = db.search_messages("second corruption")
        assert results
        assert any("second corruption" in row["snippet"] for row in results)

        # A later open atomically rebuilds all canonical rows before triggers
        # return, then clears the durable breadcrumb.
        db.close()
        reopened = SessionDB(db_path=db_path)
        try:
            assert reopened._fts_stale is False
            assert _meta_value(db_path, FTS_STALE_KEY) is None
            assert _base_fts_triggers(db_path) == set(_FTS_TRIGGERS)
            results = reopened.search_messages("second corruption")
            assert results
        finally:
            reopened.close()

    def obsolete_failed_in_place_rebuild_fails_open(self, db, tmp_path, monkeypatch):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)

        def _failed_rebuild():
            raise sqlite3.DatabaseError("rebuild could not read corrupt FTS")

        monkeypatch.setattr(db, "rebuild_fts", _failed_rebuild)
        db.append_message("s1", "user", "canonical survives")

        assert _message_contents(db_path)[-1] == "canonical survives"
        assert _meta_value(db_path, FTS_STALE_KEY) == "1"
        assert _base_fts_triggers(db_path) == set()

    def obsolete_foreign_holder_skips_runtime_rebuild_and_fails_open(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)

        monkeypatch.setattr(
            db,
            "_foreign_state_db_holders",
            lambda: [(4242, str(db_path) + "-wal")],
            raising=False,
        )

        db.append_message("s1", "user", "canonical survives foreign holder")

        assert _message_contents(db_path)[-1] == "canonical survives foreign holder"
        assert db._fts_stale is True
        assert _meta_value(db_path, FTS_STALE_KEY) == "1"
        assert _base_fts_triggers(db_path) == set()

    def obsolete_stale_search_preserves_not_semantics(self, db, tmp_path, monkeypatch):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "python language guide")
        db.append_message("s1", "user", "python java interoperability")
        _corrupt_fts(db_path)

        monkeypatch.setattr(
            db,
            "rebuild_fts",
            lambda: (_ for _ in ()).throw(
                sqlite3.DatabaseError("rebuild could not read corrupt FTS")
            ),
        )
        db.append_message("s1", "user", "canonical write survives")
        assert db._fts_stale is True

        results = db.search_messages("python NOT java")
        snippets = [row["snippet"] for row in results]
        assert any("python language guide" in snippet for snippet in snippets)
        assert all("java" not in snippet for snippet in snippets)

    def obsolete_existing_peer_observes_fail_open_marker(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        peer = SessionDB(db_path=db_path)
        try:
            _corrupt_fts(db_path)

            def _failed_rebuild():
                raise sqlite3.DatabaseError("rebuild failed")

            monkeypatch.setattr(db, "rebuild_fts", _failed_rebuild)
            db.append_message("s1", "user", "visible through canonical search")

            assert peer._fts_stale is False
            results = peer.search_messages("canonical search")
            assert peer._fts_stale is True
            assert results
        finally:
            peer.close()

    def obsolete_failed_startup_rebuild_keeps_fts_detached(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)
        monkeypatch.setattr(
            db,
            "rebuild_fts",
            lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("still corrupt")),
        )
        db.append_message("s1", "user", "before restart")
        db.close()

        monkeypatch.setattr(
            SessionDB,
            "_recover_stale_fts",
            lambda self, cursor, legacy: False,
        )
        reopened = SessionDB(db_path=db_path)
        try:
            assert reopened._fts_stale is True
            assert _meta_value(db_path, FTS_STALE_KEY) == "1"
            assert _base_fts_triggers(db_path) == set()
            reopened.append_message("s1", "user", "after failed recovery")
            assert _message_contents(db_path)[-1] == "after failed recovery"
            assert reopened.search_messages("failed recovery")
        finally:
            reopened.close()

    def obsolete_foreign_holder_defers_startup_stale_rebuild(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)
        monkeypatch.setattr(
            db,
            "rebuild_fts",
            lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("still corrupt")),
        )
        db.append_message("s1", "user", "before restart")
        db.close()

        monkeypatch.setattr(
            SessionDB,
            "_foreign_state_db_holders",
            lambda self: [(4242, str(db_path) + "-wal")],
            raising=False,
        )
        reopened = SessionDB(db_path=db_path)
        try:
            assert reopened._fts_stale is True
            assert _meta_value(db_path, FTS_STALE_KEY) == "1"
            assert _base_fts_triggers(db_path) == set()
            reopened.append_message("s1", "user", "after deferred recovery")
            assert _message_contents(db_path)[-1] == "after deferred recovery"
        finally:
            reopened.close()

    def obsolete_repeated_deferrals_reap_inactive_orphan_then_rebuild(
        self, db, tmp_path, monkeypatch
    ):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db_path = tmp_path / "state.db"
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        _corrupt_fts(db_path)
        monkeypatch.setattr(
            db,
            "rebuild_fts",
            lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("still corrupt")),
        )
        db.append_message("s1", "user", "before restart")
        db.close()

        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                FTS_REBUILD_DEFERRAL_KEY,
                json.dumps({"first_seen": 1.0, "last_seen": 30.0, "attempts": 2}),
            ),
        )
        raw.commit()
        raw.close()

        holder_scans = iter(([(4242, str(db_path) + "-wal")], []))
        reaped = []
        monkeypatch.setattr(
            SessionDB,
            "_foreign_state_db_holders",
            lambda self: next(holder_scans),
        )
        monkeypatch.setattr(
            SessionDB,
            "_reap_inactive_orphan_desktop_holders",
            lambda self, holders, *, min_age_seconds: reaped.extend(holders) or [4242],
        )
        monkeypatch.setattr(hermes_state_schema.time, "time", lambda: 120.0)

        reopened = SessionDB(db_path=db_path)
        try:
            assert reaped == [(4242, str(db_path) + "-wal")]
            assert reopened._fts_stale is False
            assert _meta_value(db_path, FTS_STALE_KEY) is None
            assert _meta_value(db_path, FTS_REBUILD_DEFERRAL_KEY) is None
            assert reopened.search_messages("before restart")
        finally:
            reopened.close()

    def obsolete_legacy_inline_fts_fails_open_and_recovers(self, tmp_path, monkeypatch):
        db_path = tmp_path / "legacy-state.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(SCHEMA_SQL)
        try:
            raw.executescript(LEGACY_FTS_SQL + LEGACY_FTS_TRIGRAM_SQL)
        except sqlite3.OperationalError as exc:
            raw.close()
            pytest.skip(f"required FTS tokenizer unavailable: {exc}")
        raw.commit()
        raw.close()

        legacy = SessionDB(db_path=db_path)
        try:
            assert legacy._db_has_legacy_inline_fts(legacy._conn.cursor())
            legacy.create_session("s1", source="test")
            legacy.append_message("s1", "user", "legacy seed")
            _corrupt_fts(db_path)
            monkeypatch.setattr(
                legacy,
                "rebuild_fts",
                lambda: (_ for _ in ()).throw(
                    sqlite3.DatabaseError("legacy rebuild failed")
                ),
            )
            legacy.append_message("s1", "user", "legacy canonical survives")
            assert _message_contents(db_path)[-1] == "legacy canonical survives"
            assert _meta_value(db_path, FTS_STALE_KEY) == "1"
        finally:
            legacy.close()

        recovered = SessionDB(db_path=db_path)
        try:
            assert recovered._fts_stale is False
            assert _meta_value(db_path, FTS_STALE_KEY) is None
            assert recovered.search_messages("canonical survives")
        finally:
            recovered.close()
