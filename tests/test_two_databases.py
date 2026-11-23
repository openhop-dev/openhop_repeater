"""repeater.db holds durable tables, repeater-volatile.db the volatile ones."""

import sqlite3
import time

from repeater.data_acquisition.sqlite_handler import SQLiteHandler

DURABLE_TABLES = {
    "api_tokens",
    "acl_entries",
    "transport_keys",
    "companion_contacts",
    "companion_channels",
    "companion_prefs",
}
VOLATILE_TABLES = {
    "packets",
    "packet_egress",
    "adverts",
    "noise_floor",
    "crc_errors",
    "network_usage",
    "network_usage_batches",
    "network_usage_names",
    "network_usage_coverage",
    "room_messages",
    "room_client_sync",
    "companion_messages",
    "companion_message_history",
    "neighbor_scopes",
    "daemon_state",
}


def _tables(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in rows} - {"sqlite_sequence", "migrations"}


def test_each_table_lives_in_exactly_one_database(tmp_path):
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    assert _tables(handler.sqlite_path) == DURABLE_TABLES
    assert _tables(handler.volatile_path) == VOLATILE_TABLES


def test_durable_data_survives_a_lost_volatile_database(tmp_path):
    disk, ram = tmp_path / "disk", tmp_path / "ram"
    handler = SQLiteHandler(disk, volatile_dir=ram)
    token_id = handler.create_api_token("ci", "hash-1")
    handler.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    for conn_path in (handler.volatile_path,):
        assert conn_path.exists()

    # A reboot empties the tmpfs: drop the volatile file and its WAL files.
    for path in ram.iterdir():
        path.unlink()

    restarted = SQLiteHandler(disk, volatile_dir=ram)
    with restarted._connect() as conn:
        assert conn.execute("SELECT id FROM api_tokens").fetchall() == [(token_id,)]
        assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 0


def test_durable_writes_never_touch_the_volatile_file(tmp_path):
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    handler.create_api_token("ci", "hash-1")
    with sqlite3.connect(handler.volatile_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 0
    with sqlite3.connect(handler.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 1


def test_volatile_writes_never_touch_the_durable_file(tmp_path):
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    handler.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    handler.set_daemon_state("k", {"v": 1})
    with sqlite3.connect(handler.sqlite_path) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master")} >= {
            "api_tokens"
        }
        assert "packets" not in _tables(handler.sqlite_path)
    assert handler.get_daemon_state("k") == {"v": 1}


def test_a_second_handler_on_the_same_directories_sees_the_same_data(tmp_path):
    # http_server.py builds its own SQLiteHandler next to the collector's.
    first = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    second = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    token_id = first.create_api_token("ci", "hash-1")
    first.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    with second._connect() as conn:
        assert conn.execute("SELECT id FROM api_tokens").fetchall() == [(token_id,)]
        assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 1


def test_database_stats_list_tables_of_both_databases(tmp_path):
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    handler.create_api_token("ci", "hash-1")
    handler.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    stats = handler.get_table_stats()
    names = {table["name"] for table in stats["tables"]}
    assert {"api_tokens", "packets"} <= names
    assert stats["database_size_bytes"] == handler.database_size_bytes() > 0


def test_vacuum_covers_both_databases(tmp_path):
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    handler.create_api_token("ci", "hash-1")
    handler.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    handler.vacuum()


def test_an_old_single_file_database_keeps_its_durable_rows_and_hides_its_old_volatile_ones(
    tmp_path,
):
    # Pre-split layout: everything in repeater.db. The cleanup of its old volatile
    # tables is deferred; until then they must stay unread, not shadow the new ones.
    legacy = SQLiteHandler(tmp_path)
    legacy.create_api_token("ci", "hash-1")
    with sqlite3.connect(legacy.sqlite_path) as conn:
        conn.execute(
            "CREATE TABLE packets (id INTEGER PRIMARY KEY, timestamp REAL, type INTEGER)"
        )
        conn.execute("INSERT INTO packets (timestamp, type) VALUES (1.0, 1)")
    for path in tmp_path.glob("repeater-volatile.db*"):
        path.unlink()

    upgraded = SQLiteHandler(tmp_path)
    with upgraded._connect() as conn:
        assert conn.execute("SELECT name FROM api_tokens").fetchall() == [("ci",)]
        assert conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0] == 0
    upgraded.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    assert len(upgraded.get_recent_packets(10)) == 1


def test_default_layout_keeps_both_files_in_storage_dir(tmp_path):
    handler = SQLiteHandler(tmp_path)
    handler.create_api_token("ci", "hash-1")
    assert (tmp_path / "repeater.db").exists()
    assert (tmp_path / "repeater-volatile.db").exists()
    assert _tables(tmp_path / "repeater.db") == DURABLE_TABLES
