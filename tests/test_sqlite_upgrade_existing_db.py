"""Regression test: opening a pre-existing database must not break packet recording.

`SQLiteHandler.__init__` runs `_init_database()` and then `_run_migrations()`.
`_init_database` used to create `idx_packets_upstream_time`, an index spanning
`upstream_hash` / `upstream_hash_size` -- columns that only exist on an older
database once `add_upstream_hash_to_packets` has added them, which happens in
the migration step afterwards.

On a fresh install this passed, because the `CREATE TABLE` includes the columns.
On any existing install it raised ``no such column: upstream_hash``, which
`_init_database` caught and logged. Every statement after the failing index was
therefore skipped, and the daemon came up looking completely healthy -- service
active, radio initialised, companions serving, HTTP up -- while recording zero
packets and doing no RX/TX.

These tests build a database with the *old* packets schema and assert that a
handler opens it, ends up with both columns and the index, and can insert.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repeater.data_acquisition.sqlite_handler import (  # noqa: E402
    AIRTIME_INDEX_COLUMNS,
    SQLiteHandler,
)

# The packets columns that predate migration 13. Enough of the real schema for
# the handler to treat this as an existing database rather than a fresh one.
_OLD_PACKETS_SCHEMA = """
CREATE TABLE packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    type INTEGER,
    route INTEGER,
    length INTEGER,
    rssi REAL,
    snr REAL,
    score REAL,
    transmitted INTEGER,
    is_duplicate INTEGER,
    drop_reason TEXT,
    src_hash TEXT,
    dst_hash TEXT,
    path_hash TEXT,
    header TEXT,
    transport_codes TEXT,
    payload TEXT,
    payload_length INTEGER,
    packet_hash TEXT
)
"""


def _make_legacy_db(path):
    con = sqlite3.connect(path)
    con.execute(_OLD_PACKETS_SCHEMA)
    con.execute("INSERT INTO packets (timestamp, type, length) VALUES (?, ?, ?)", (1.0, 1, 10))
    con.commit()
    con.close()


def _columns(path, table="packets"):
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(%s)" % table)}
    finally:
        con.close()


def _indexes(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()


@pytest.fixture
def legacy_db():
    """A storage dir holding a repeater.db with the pre-migration-13 schema."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "repeater.db")
    _make_legacy_db(path)
    yield d, path
    shutil.rmtree(d, ignore_errors=True)


def test_opens_database_missing_upstream_hash_columns(legacy_db):
    """The upgrade path must complete, not half-apply."""
    d, path = legacy_db
    assert "upstream_hash" not in _columns(path), "fixture is not a legacy db"

    SQLiteHandler(Path(d))

    cols = _columns(path)
    assert "upstream_hash" in cols
    assert "upstream_hash_size" in cols


def test_upstream_index_exists_after_upgrade(legacy_db):
    """The index must still be created -- by the migration that owns the columns."""
    d, path = legacy_db
    SQLiteHandler(Path(d))
    assert "idx_packets_upstream_time" in _indexes(path)


def _tables(path):
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_schema_init_runs_to_completion(legacy_db):
    """The actual damage: `_init_database` aborting part-way.

    This is what the caught exception hid. `idx_packets_upstream_time` was
    created roughly two thirds of the way through `_init_database`; when it
    raised, every statement after it was skipped and the error was swallowed.
    The daemon then ran with a partially-created schema.

    `room_messages` and `room_client_sync` are created *after* that point, so
    their presence is a direct proof that schema init completed. Asserting on
    the upstream columns alone is not enough -- the migration adds those
    afterwards either way, which is exactly why this bug self-heals on the
    second start and is so easy to misread as harmless.
    """
    d, path = legacy_db
    SQLiteHandler(Path(d))

    tables = _tables(path)
    for late in ("room_messages", "room_client_sync"):
        assert late in tables, (
            "%s is missing: _init_database did not run to completion on an existing database" % late
        )


def test_recording_works_after_upgrade(legacy_db):
    """A packet insert must still land once the upgrade has run."""
    d, path = legacy_db
    SQLiteHandler(Path(d))

    con = sqlite3.connect(path)
    try:
        before = con.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        con.execute(
            "INSERT INTO packets (timestamp, type, length, upstream_hash) VALUES (?, ?, ?, ?)",
            (2.0, 1, 12, "ABCD"),
        )
        con.commit()
        after = con.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    finally:
        con.close()
    assert after == before + 1


def test_upgrade_adds_tx_radio_ids_without_touching_existing_rows(legacy_db):
    d, path = legacy_db
    assert "tx_radio_ids" not in _columns(path), "fixture is not a legacy db"

    SQLiteHandler(Path(d))

    assert "tx_radio_ids" in _columns(path)
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT timestamp, type, length, tx_radio_ids FROM packets").fetchall()
    finally:
        con.close()
    assert rows == [(1.0, 1, 10, None)]


def test_tx_radio_ids_round_trip_through_every_packet_read():
    d = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(d))
        base = {"type": 2, "route": 1, "length": 4, "transmitted": True}
        handler.store_packet(
            {
                **base,
                "timestamp": 5.0,
                "packet_hash": "FANOUT0000000001",
                "rx_radio_id": "local",
                "tx_radio_id": "link",
                "tx_radio_ids": ["link", "local"],
            }
        )
        handler.store_packet({**base, "timestamp": 6.0, "packet_hash": "SINGLE0000000002"})

        con = sqlite3.connect(os.path.join(d, "repeater.db"))
        try:
            stored = con.execute(
                "SELECT tx_radio_ids FROM packets WHERE packet_hash = ?", ("FANOUT0000000001",)
            ).fetchone()[0]
        finally:
            con.close()
        assert stored == '["link", "local"]'  # JSON text on disk

        by_hash = handler.get_packet_by_hash("FANOUT0000000001")
        assert by_hash["tx_radio_id"] == "link"
        assert by_hash["tx_radio_ids"] == ["link", "local"]
        assert handler.get_packet_by_id(by_hash["id"])["tx_radio_ids"] == ["link", "local"]

        for rows in (handler.get_recent_packets(10), handler.get_filtered_packets(limit=10)):
            by = {row["packet_hash"]: row for row in rows}
            assert by["FANOUT0000000001"]["tx_radio_ids"] == ["link", "local"]
            assert by["SINGLE0000000002"]["tx_radio_ids"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_fresh_database_also_gets_column_and_index():
    """The fresh-install path must be unaffected by moving the index."""
    d = tempfile.mkdtemp()
    try:
        SQLiteHandler(Path(d))
        path = os.path.join(d, "repeater.db")
        assert {"upstream_hash", "upstream_hash_size"} <= _columns(path)
        assert "idx_packets_upstream_time" in _indexes(path)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _index_columns(path, name):
    con = sqlite3.connect(path)
    try:
        return tuple(row[2] for row in con.execute("PRAGMA index_info(%s)" % name))
    finally:
        con.close()


def test_upgrade_widens_the_existing_airtime_index_to_radio_ids(legacy_db):
    """An install that already built the four-column index gets it rebuilt.

    CREATE INDEX IF NOT EXISTS leaves an existing index as it is, so without the
    migration an upgraded database would keep the narrow index and the airtime
    chart would read the row heap for every packet in its window.
    """
    d, path = legacy_db
    con = sqlite3.connect(path)
    con.execute(
        "CREATE INDEX idx_packets_airtime ON packets(timestamp, length, payload_length, transmitted)"
    )
    con.commit()
    con.close()

    SQLiteHandler(Path(d))

    assert _index_columns(path, "idx_packets_airtime") == AIRTIME_INDEX_COLUMNS
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT timestamp, type, length FROM packets").fetchall() == [
            (1.0, 1, 10)
        ]
    finally:
        con.close()


def test_fresh_database_airtime_index_covers_radio_ids():
    d = tempfile.mkdtemp()
    try:
        SQLiteHandler(Path(d))
        path = os.path.join(d, "repeater.db")
        assert _index_columns(path, "idx_packets_airtime") == AIRTIME_INDEX_COLUMNS
    finally:
        shutil.rmtree(d, ignore_errors=True)
