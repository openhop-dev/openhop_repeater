"""Per-egress TX metadata on a multi-radio node.

One logical packet leaves a bridge through both radios. The packet row records
the primary egress only, so the second radio's LBT figures were lost and a send
that failed left no trace at all. ``packet_egress`` keeps one row per physical
send; a single-radio node writes nothing and is unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.engine import FanoutTxResult, RadioTxResult, RepeaterHandler

LOCAL = {
    "radio_id": "local",
    "frequency_hz": 910100000,
    "bandwidth_hz": 500000,
    "spreading_factor": 7,
    "coding_rate": 5,
    "preamble_length": 17,
}
LINK = dict(LOCAL, radio_id="link", frequency_hz=910525000, bandwidth_hz=62500)
BRIDGE = [LOCAL, LINK]
SINGLE = [dict(LOCAL, radio_id="radio0")]

BASE_TS = 1_700_000_000.0


@pytest.fixture
def handler(tmp_path) -> SQLiteHandler:
    return SQLiteHandler(tmp_path)


def _egress(radio_id, success=True, attempts=0, backoff=0.0, busy=False) -> dict:
    return {
        "radio_id": radio_id,
        "success": success,
        "lbt_attempts": attempts,
        "lbt_backoff_ms_total": backoff,
        "lbt_channel_busy": busy,
    }


def _sends(store: SQLiteHandler, packet_id: int, rows, offset: float = 0.0) -> None:
    store.store_packet_egress(packet_id, BASE_TS + offset, rows)


def _diagnostics(store: SQLiteHandler, profiles=None) -> dict:
    return store.get_lbt_diagnostics(
        start_timestamp=BASE_TS - 300,
        end_timestamp=BASE_TS + 300,
        bucket_seconds=300,
        radio_profiles=profiles,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_migration_creates_the_table_on_an_existing_database(tmp_path):
    db = tmp_path / "repeater-volatile.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE migrations (id INTEGER PRIMARY KEY, migration_name TEXT)")
        conn.commit()

    store = SQLiteHandler(tmp_path)
    _sends(store, 1, [_egress("local")])

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packet_egress").fetchone()[0] == 1
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_packet_egress_time_radio" in indexes


def test_every_physical_send_is_stored_including_the_failures(handler):
    _sends(
        handler, 7, [_egress("local", attempts=2, backoff=140.0), _egress("link", success=False)]
    )

    with sqlite3.connect(handler.volatile_path) as conn:
        rows = conn.execute(
            "SELECT packet_id, radio_id, success, lbt_attempts, lbt_backoff_ms_total "
            "FROM packet_egress ORDER BY radio_id"
        ).fetchall()

    assert rows == [(7, "link", 0, 0, 0.0), (7, "local", 1, 2, 140.0)]


def test_nothing_is_written_without_a_packet_id_or_rows(handler):
    handler.store_packet_egress(None, BASE_TS, [_egress("local")])
    handler.store_packet_egress(3, BASE_TS, [])
    handler.store_packet_egress(3, BASE_TS, None)

    with sqlite3.connect(handler.volatile_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packet_egress").fetchone()[0] == 0


def test_retention_ages_egress_rows_out_with_their_packets(handler):
    now = time.time()
    handler.store_packet_egress(1, now - 40 * 24 * 3600, [_egress("local")])
    handler.store_packet_egress(2, now - 60, [_egress("link")])

    handler.cleanup_old_data(days=31)

    with sqlite3.connect(handler.volatile_path) as conn:
        assert [row[0] for row in conn.execute("SELECT packet_id FROM packet_egress")] == [2]


def test_purging_packets_takes_their_egress_rows(handler):
    handler.store_packet({"timestamp": time.time(), "type": 1, "route": 1, "length": 40})
    _sends(handler, 1, [_egress("local")])

    handler.purge_table("packets")

    with sqlite3.connect(handler.volatile_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packet_egress").fetchone()[0] == 0


def test_db_stats_lists_the_table_only_once_a_radio_has_written_to_it(handler):
    """A single-radio node must report the tables it always reported.

    The table is created everywhere but written only on a bridge, and counting
    its rows is a scan of a table that outgrows packets there.
    """

    def names():
        return [table["name"] for table in handler.get_table_stats()["tables"]]

    assert "packet_egress" not in names()

    _sends(handler, 1, [_egress("local")])

    assert "packet_egress" in names()


# ---------------------------------------------------------------------------
# LBT diagnostics
# ---------------------------------------------------------------------------


def test_a_single_radio_answer_gains_nothing(handler):
    _sends(handler, 1, [_egress("local")])

    single = _diagnostics(handler, SINGLE)
    legacy = _diagnostics(handler)

    assert "radios" not in single
    assert "unattributed_transmissions" not in single
    assert single == legacy


def test_each_radio_gets_its_own_contention_figures(handler):
    # One packet fanned out: clear on the wide radio, three checks on the narrow one.
    _sends(handler, 1, [_egress("local"), _egress("link", attempts=2, backoff=210.0, busy=True)])
    _sends(handler, 2, [_egress("local"), _egress("link", success=False, attempts=3, busy=True)])

    radios = {entry["radio_id"]: entry for entry in _diagnostics(handler, BRIDGE)["radios"]}

    assert radios["local"]["summary"]["total_transmissions"] == 2
    assert radios["local"]["summary"]["retry_packets"] == 0
    assert radios["local"]["summary"]["failed_transmissions"] == 0
    assert radios["link"]["summary"]["retry_packets"] == 2
    assert radios["link"]["summary"]["failed_transmissions"] == 1
    assert radios["link"]["summary"]["busy_channel_events"] == 2
    assert radios["link"]["summary"]["max_attempts"] == 4


def test_the_combined_figures_still_count_each_packet_once(handler):
    """A relay sent on both radios is two transmissions per radio, one combined."""
    for packet_id in (1, 2):
        handler.store_packet(
            {
                "timestamp": BASE_TS,
                "type": 1,
                "route": 1,
                "length": 40,
                "transmitted": True,
                "tx_radio_id": "local",
                "tx_radio_ids": ["local", "link"],
            }
        )
        _sends(handler, packet_id, [_egress("local"), _egress("link")])

    result = _diagnostics(handler, BRIDGE)
    per_radio = {entry["radio_id"]: entry for entry in result["radios"]}

    assert result["summary"]["total_transmissions"] == 2
    assert per_radio["local"]["summary"]["total_transmissions"] == 2
    assert per_radio["link"]["summary"]["total_transmissions"] == 2


def test_a_radio_no_longer_configured_is_counted_as_unattributed(handler):
    _sends(handler, 1, [_egress("local"), _egress("wide")])

    result = _diagnostics(handler, BRIDGE)
    radios = {entry["radio_id"]: entry for entry in result["radios"]}

    assert result["unattributed_transmissions"] == 1
    assert radios["local"]["summary"]["total_transmissions"] == 1
    assert radios["link"]["summary"]["total_transmissions"] == 0


def test_a_radio_with_no_traffic_still_reports_an_empty_series(handler):
    _sends(handler, 1, [_egress("local")])

    radios = {entry["radio_id"]: entry for entry in _diagnostics(handler, BRIDGE)["radios"]}

    assert radios["link"]["summary"]["has_lbt_data"] is False
    assert radios["link"]["buckets"] and all(
        bucket["transmissions"] == 0 for bucket in radios["link"]["buckets"]
    )


# ---------------------------------------------------------------------------
# Engine: building the rows
# ---------------------------------------------------------------------------


class _Fabric:
    def __init__(self, radio_ids):
        self.radios = OrderedDict((radio_id, object()) for radio_id in radio_ids)
        self.default_radio_id = radio_ids[0]


def _make_handler(radio_ids):
    config = {
        "repeater": {"mode": "forward", "cache_ttl": 3600, "send_advert_interval_hours": 0},
        "mesh": {"unscoped_flood_allow": True, "loop_detect": "off"},
        "delays": {"tx_delay_factor": 1.0, "direct_tx_delay_factor": 0.5},
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
        "radio": {
            "spreading_factor": 7,
            "bandwidth": 62500,
            "coding_rate": 5,
            "preamble_length": 17,
        },
    }
    radio = MagicMock()
    radio.fabric = _Fabric(radio_ids) if radio_ids else None
    dispatcher = MagicMock()
    dispatcher.radio = radio
    dispatcher.local_identity = MagicMock()
    dispatcher.send_packet = AsyncMock()
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        return RepeaterHandler(config, dispatcher, 0xAB, local_hash_bytes=bytes([0xAB]))


def _fanout(*results) -> FanoutTxResult:
    return FanoutTxResult(list(results))


def test_a_single_radio_node_writes_no_egress_rows():
    """Its packet row already says everything these would."""
    handler = _make_handler(["radio0"])

    result = handler._egress_records(_fanout(RadioTxResult("radio0", True, metadata={})))

    assert result is None


def test_a_bridge_records_one_row_per_send_with_its_lbt_figures():
    handler = _make_handler(["local", "link"])

    records = handler._egress_records(
        _fanout(
            RadioTxResult("local", True, metadata={"lbt_attempts": 0}),
            RadioTxResult(
                "link",
                False,
                metadata={
                    "lbt_attempts": 3,
                    "lbt_backoff_delays_ms": [40.0, 80.0, 90.0],
                    "lbt_channel_busy": True,
                },
            ),
        )
    )

    assert records == [
        {
            "radio_id": "local",
            "success": True,
            "lbt_attempts": 0,
            "lbt_backoff_ms_total": 0.0,
            "lbt_channel_busy": False,
        },
        {
            "radio_id": "link",
            "success": False,
            "lbt_attempts": 3,
            "lbt_backoff_ms_total": 210.0,
            "lbt_channel_busy": True,
        },
    ]


def test_an_egress_that_raised_still_records_the_attempt():
    """A radio whose send blew up carries no metadata; the failure is still the point."""
    handler = _make_handler(["local", "link"])

    records = handler._egress_records(
        _fanout(
            RadioTxResult("local", True, metadata=None),
            RadioTxResult("link", False, error=RuntimeError("modem gone")),
        )
    )

    assert [(row["radio_id"], row["success"]) for row in records] == [
        ("local", True),
        ("link", False),
    ]


# ---------------------------------------------------------------------------
# Storage collector: the rows land beside the packet, not on it
# ---------------------------------------------------------------------------


def _collector(sqlite_handler):
    from repeater.data_acquisition.storage_collector import StorageCollector

    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = sqlite_handler
    collector.rrd_handler = None
    collector.mqtt_handler = None
    collector.glass_publish_callback = None
    collector.websocket_available = False
    collector._publish_packet_to_mqtt = MagicMock()
    return collector


def test_egress_rows_are_written_against_the_stored_packet_id(handler):
    collector = _collector(handler)
    record = {"timestamp": BASE_TS, "type": 1, "route": 1, "length": 40, "transmitted": True}

    collector._record_packet_blocking(record, False, [_egress("local"), _egress("link")])

    with sqlite3.connect(handler.volatile_path) as conn:
        rows = conn.execute("SELECT packet_id, radio_id FROM packet_egress").fetchall()
    assert rows == [(record["id"], "local"), (record["id"], "link")]


def test_the_published_packet_record_never_carries_the_egress_rows(handler):
    """Every websocket, Glass and MQTT publish ships the packet record verbatim."""
    collector = _collector(handler)
    published = []
    collector._publish_to_glass = lambda record, kind: published.append(dict(record))
    record = {"timestamp": BASE_TS, "type": 1, "route": 1, "length": 40, "transmitted": True}

    collector._record_packet_blocking(record, False, [_egress("local")])

    assert "tx_egress" not in record
    assert published and all("tx_egress" not in entry for entry in published)


def test_a_single_radio_packet_takes_the_path_it_always_did(handler):
    collector = _collector(handler)
    record = {"timestamp": BASE_TS, "type": 1, "route": 1, "length": 40}

    collector._record_packet_blocking(record, False)

    with sqlite3.connect(handler.volatile_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM packet_egress").fetchone()[0] == 0
    assert isinstance(record["id"], int)


def test_the_collector_facade_takes_the_egress_rows(handler):
    """The engine calls record_packet; a signature drift here loses every row."""
    collector = _collector(handler)
    collector._submit_db = lambda fn, *args: fn(*args)
    record = {"timestamp": BASE_TS, "type": 1, "route": 1, "length": 40, "transmitted": True}

    collector.record_packet(record, skip_mqtt=False, tx_egress=[_egress("link")])

    with sqlite3.connect(handler.volatile_path) as conn:
        assert conn.execute("SELECT radio_id FROM packet_egress").fetchone()[0] == "link"
