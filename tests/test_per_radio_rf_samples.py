"""Per-radio noise floor and CRC error sampling.

A bridge has one receiver per band, so one noise floor and one CRC counter
describe neither. Every radio is now sampled and stored against its own id,
while a single-radio node stores NULL and gets byte-for-byte the answers it had.
"""

from __future__ import annotations

import sqlite3
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.data_acquisition.storage_collector import StorageCollector
from repeater.engine import RepeaterHandler
from repeater.web.api_endpoints import APIEndpoints

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

LOCAL_HASH = 0xAB


@pytest.fixture
def handler(tmp_path) -> SQLiteHandler:
    return SQLiteHandler(tmp_path)


def _noise(handler: SQLiteHandler, dbm: float, radio_id=None, ago: float = 60.0) -> None:
    handler.store_noise_floor(
        {"timestamp": time.time() - ago, "noise_floor_dbm": dbm, "radio_id": radio_id}
    )


def _crc(handler: SQLiteHandler, count: int, radio_id=None, ago: float = 60.0) -> None:
    handler.store_crc_errors({"timestamp": time.time() - ago, "count": count, "radio_id": radio_id})


@pytest.fixture
def bridge_db(handler) -> SQLiteHandler:
    """Both radios sampled, plus a row from before per-radio sampling."""
    _noise(handler, -118.0, "local", ago=90)
    _noise(handler, -103.0, "link", ago=80)
    _noise(handler, -117.0, "local", ago=70)
    _noise(handler, -95.0, None, ago=60)
    _crc(handler, 2, "local", ago=90)
    _crc(handler, 7, "link", ago=80)
    _crc(handler, 1, None, ago=60)
    return handler


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_migration_adds_radio_id_to_an_existing_database(tmp_path):
    """A database written before this work gains the column and its indexes."""
    db = tmp_path / "repeater-volatile.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE noise_floor (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, noise_floor_dbm REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE crc_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "INSERT INTO noise_floor (timestamp, noise_floor_dbm) VALUES (?, ?)", (1.0, -9)
        )
        conn.commit()

    store = SQLiteHandler(tmp_path)

    with sqlite3.connect(db) as conn:
        for table in ("noise_floor", "crc_errors"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert "radio_id" in columns
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_noise_radio_time", "idx_crc_radio_time"} <= indexes
        # The pre-existing sample is kept, unattributed rather than guessed.
        assert conn.execute("SELECT radio_id FROM noise_floor").fetchone()[0] is None

    _noise(store, -110.0, "link")
    assert [row["noise_floor_dbm"] for row in store.get_noise_floor_history(radio_id="link")] == [
        -110.0
    ]


# ---------------------------------------------------------------------------
# Single radio: unchanged answers
# ---------------------------------------------------------------------------


def test_single_radio_history_carries_no_radio_id(bridge_db):
    """Without two profiles the rows are exactly what they always were."""
    noise = bridge_db.get_noise_floor_history(radio_profiles=SINGLE)
    crc = bridge_db.get_crc_error_history(radio_profiles=SINGLE)

    assert all(set(row) == {"timestamp", "noise_floor_dbm"} for row in noise)
    assert all(set(row) == {"timestamp", "count"} for row in crc)
    assert bridge_db.get_noise_floor_history() == noise
    assert bridge_db.get_crc_error_history() == crc


def test_unfiltered_reads_still_see_every_sample(bridge_db):
    assert len(bridge_db.get_noise_floor_history()) == 4
    assert bridge_db.get_crc_error_count() == 10
    assert bridge_db.get_noise_floor_stats()["measurement_count"] == 4


# ---------------------------------------------------------------------------
# Two radios: attribution and filtering
# ---------------------------------------------------------------------------


def test_bridge_history_names_the_sampled_radio(bridge_db):
    noise = bridge_db.get_noise_floor_history(radio_profiles=BRIDGE)
    crc = bridge_db.get_crc_error_history(radio_profiles=BRIDGE)

    assert [row["radio_id"] for row in noise] == ["local", "link", "local", None]
    assert [row["radio_id"] for row in crc] == ["local", "link", None]


def test_a_radio_no_longer_configured_reads_as_unattributed(bridge_db):
    """Renaming a radio must not silently re-attribute its history to another."""
    renamed = [LOCAL, dict(LINK, radio_id="narrow")]

    rows = bridge_db.get_noise_floor_history(radio_profiles=renamed)

    assert [row["radio_id"] for row in rows] == ["local", None, "local", None]


def test_filtering_narrows_to_one_radio(bridge_db):
    noise = bridge_db.get_noise_floor_history(radio_id="local", radio_profiles=BRIDGE)
    crc = bridge_db.get_crc_error_history(radio_id="link", radio_profiles=BRIDGE)

    assert [row["noise_floor_dbm"] for row in noise] == [-118.0, -117.0]
    assert [row["count"] for row in crc] == [7]
    assert bridge_db.get_crc_error_count(radio_id="local") == 2


def test_filtered_stats_describe_one_receiver(bridge_db):
    """Averaged across a bridge these numbers describe neither band."""
    both = bridge_db.get_noise_floor_stats()
    local = bridge_db.get_noise_floor_stats(radio_id="local")

    assert local["measurement_count"] == 2
    assert local["avg_noise_floor"] == -117.5
    assert local["min_noise_floor"] == -118.0
    assert both["avg_noise_floor"] != local["avg_noise_floor"]


def test_paging_one_radio_is_stable_while_the_other_samples(handler):
    """Offset paging over the merged series would reshuffle as the other radio writes."""
    for index in range(6):
        _noise(handler, -100.0 - index, "local", ago=600 - index * 10)
        _noise(handler, -80.0, "link", ago=595 - index * 10)

    first = handler.get_noise_floor_history(limit=3, offset=0, radio_id="local")
    _noise(handler, -70.0, "link", ago=1)
    second = handler.get_noise_floor_history(limit=3, offset=3, radio_id="local")

    seen = [row["noise_floor_dbm"] for row in second + first]
    assert seen == [-100.0, -101.0, -102.0, -103.0, -104.0, -105.0]


# ---------------------------------------------------------------------------
# Sampling: one reading per radio, published for the default one only
# ---------------------------------------------------------------------------


class _PhysicalRadio:
    def __init__(self, noise_floor, crc_error_count=0):
        self._noise_floor = noise_floor
        self.crc_error_count = crc_error_count
        self.reads = 0

    def get_noise_floor(self):
        self.reads += 1
        return self._noise_floor


class _Fabric:
    def __init__(self, radios, default_radio_id):
        self.radios = OrderedDict(radios)
        self.default_radio_id = default_radio_id

    def get_radio(self, radio_id):
        return self.radios[radio_id]


def _make_handler(fabric=None):
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
    radio.fabric = fabric
    radio.crc_error_count = 0
    dispatcher = MagicMock()
    dispatcher.radio = radio
    dispatcher.local_identity = MagicMock()
    dispatcher.send_packet = AsyncMock()
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        handler = RepeaterHandler(
            config, dispatcher, LOCAL_HASH, local_hash_bytes=bytes([LOCAL_HASH])
        )
    handler.storage = MagicMock()
    return handler


def _bridge_handler(default_radio_id="local"):
    local = _PhysicalRadio(-118.0, crc_error_count=4)
    link = _PhysicalRadio(-101.0, crc_error_count=9)
    fabric = _Fabric([("local", local), ("link", link)], default_radio_id)
    return _make_handler(fabric), local, link


def test_single_radio_samples_once_with_no_radio_id():
    handler = _make_handler()

    assert handler._sampling_radios() == [(None, handler.dispatcher.radio)]


def test_the_default_radio_is_sampled_first():
    """Callers publish the first entry, and /stats reports the default radio."""
    handler, _, _ = _bridge_handler(default_radio_id="link")

    assert [radio_id for radio_id, _ in handler._sampling_radios()] == ["link", "local"]


@pytest.mark.asyncio
async def test_noise_floor_is_sampled_and_stored_per_radio():
    handler, local, link = _bridge_handler()

    await handler._record_noise_floor_async()

    assert handler.storage.record_noise_floor.call_args_list == [
        (((-118.0), "local"), {"publish": True}),
        (((-101.0), "link"), {"publish": False}),
    ]
    assert (local.reads, link.reads) == (1, 1)


@pytest.mark.asyncio
async def test_only_the_default_radio_reaches_the_observer_feed():
    """MQTT and Glass carry one noise floor per node; per-radio publishing is separate work.

    /stats keeps the same scalar for the same reason, and reports the other
    radios beside it rather than in it.
    """
    handler, _, _ = _bridge_handler(default_radio_id="link")

    await handler._record_noise_floor_async()

    published = [
        call for call in handler.storage.record_noise_floor.call_args_list if call.kwargs["publish"]
    ]
    assert [call.args for call in published] == [(-101.0, "link")]
    assert handler._cached_noise_floor == -101.0


@pytest.mark.asyncio
async def test_a_radio_that_cannot_be_read_does_not_stop_the_others():
    handler, local, link = _bridge_handler()
    local.get_noise_floor = MagicMock(side_effect=RuntimeError("modem timeout"))

    await handler._record_noise_floor_async()

    assert [call.args for call in handler.storage.record_noise_floor.call_args_list] == [
        (-101.0, "link")
    ]


@pytest.mark.asyncio
async def test_stats_reports_every_radio_noise_floor_beside_the_scalar():
    """Without this the sidebar can only refresh the default radio's curve live."""
    handler, _, _ = _bridge_handler()

    await handler._record_noise_floor_async()
    stats = handler.get_stats()

    assert stats["noise_floor_dbm"] == -118.0
    assert stats["noise_floor_radios"] == [
        {"radio_id": "local", "noise_floor_dbm": -118.0},
        {"radio_id": "link", "noise_floor_dbm": -101.0},
    ]


@pytest.mark.asyncio
async def test_a_single_radio_node_stats_gains_no_per_radio_noise_floor():
    handler = _make_handler()

    await handler._record_noise_floor_async()
    stats = handler.get_stats()

    assert "noise_floor_radios" not in stats
    assert handler.get_cached_noise_floor_by_radio() == {}


@pytest.mark.asyncio
async def test_a_radio_that_leaves_the_fabric_leaves_no_stale_figure():
    handler, local, link = _bridge_handler()
    await handler._record_noise_floor_async()
    assert set(handler.get_cached_noise_floor_by_radio()) == {"local", "link"}

    handler.dispatcher.radio.fabric.radios.pop("link")
    await handler._record_noise_floor_async()

    assert handler.get_cached_noise_floor_by_radio() == {}
    assert "noise_floor_radios" not in handler.get_stats()


@pytest.mark.asyncio
async def test_a_radio_that_cannot_be_read_keeps_its_last_figure():
    """One timed-out read should not blank a curve that was drawing fine."""
    handler, local, link = _bridge_handler()
    await handler._record_noise_floor_async()

    link.get_noise_floor = MagicMock(side_effect=RuntimeError("modem timeout"))
    await handler._record_noise_floor_async()

    assert handler.get_cached_noise_floor_by_radio() == {"local": -118.0, "link": -101.0}


@pytest.mark.asyncio
async def test_crc_deltas_are_measured_against_each_radio_own_baseline():
    handler, local, link = _bridge_handler()

    await handler._record_crc_errors_async()
    local.crc_error_count = 6
    link.crc_error_count = 9
    handler.storage.record_crc_errors.reset_mock()
    await handler._record_crc_errors_async()

    # Only local moved; a shared baseline would have charged link with the swing.
    assert [call.args for call in handler.storage.record_crc_errors.call_args_list] == [
        (2, "local")
    ]
    assert handler._crc_error_baselines == {"local": 6, "link": 9}


@pytest.mark.asyncio
async def test_crc_errors_publish_only_the_default_radio():
    handler, _, _ = _bridge_handler()

    await handler._record_crc_errors_async()

    published = {
        call.args[1]
        for call in handler.storage.record_crc_errors.call_args_list
        if call.kwargs["publish"]
    }
    assert published == {"local"}


def test_node_crc_count_sums_every_radio():
    # The scalar every reader shares -- stats.errors, /stats crc_error_count and
    # the wire response's n_recv_errors. Reading the default radio alone would
    # leave a deaf backhaul invisible in all three.
    handler, _, _ = _bridge_handler()

    assert handler.get_crc_error_count() == 13
    assert handler.get_crc_error_count_by_radio() == {"local": 4, "link": 9}


def test_node_crc_count_reconciles_with_the_per_radio_figures():
    handler, _, _ = _bridge_handler()

    by_radio = handler.get_crc_error_count_by_radio()

    assert handler.get_crc_error_count() == sum(by_radio.values())


def test_a_radio_the_fabric_cannot_hand_back_keeps_its_last_count():
    # Lifetime counters: a radio going missing does not un-drop the packets it
    # dropped, and an observer reading these as deltas must never see a negative
    # interval. The sampler's last reading stands in until the radio is back.
    handler, local, link = _bridge_handler()
    handler._crc_error_baselines = {"local": 4, "link": 9}

    def no_link(radio_id):
        if radio_id == "link":
            raise KeyError(radio_id)
        return local

    handler.dispatcher.radio.fabric.get_radio = no_link

    assert handler.get_crc_error_count_by_radio() == {"local": 4}
    assert handler.get_crc_error_count() == 13


def test_a_fabric_down_to_one_radio_still_counts_the_one_that_left():
    handler, local, _ = _bridge_handler()
    handler._crc_error_baselines = {"local": 4, "link": 9}

    handler.dispatcher.radio.fabric = _Fabric([("local", local)], "local")

    # One radio needs no label, so it reports unattributed again ...
    assert handler.get_crc_error_count_by_radio() == {}
    # ... but the node total does not drop the departed radio's 9.
    assert handler.get_crc_error_count() == 13


def test_single_radio_node_still_reports_its_one_counter():
    handler = _make_handler()
    handler.dispatcher.radio.crc_error_count = 5

    assert handler.get_crc_error_count_by_radio() == {}
    assert handler.get_crc_error_count() == 5


# ---------------------------------------------------------------------------
# API boundary: the endpoints call the storage facade, not the SQLite handler
# ---------------------------------------------------------------------------

BRIDGE_CONFIG = {
    "radios": [
        {
            "id": "local",
            "radio_type": "sx1262",
            "radio": {
                "frequency": 910100000,
                "bandwidth": 500000,
                "spreading_factor": 7,
                "coding_rate": 5,
                "preamble_length": 17,
            },
        },
        {
            "id": "link",
            "radio_type": "sx1262",
            "radio": {
                "frequency": 910525000,
                "bandwidth": 62500,
                "spreading_factor": 7,
                "coding_rate": 5,
                "preamble_length": 17,
            },
        },
    ],
    "fabric": {"default_radio": "local", "tx_mode": "bridge"},
}


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    monkeypatch.setattr(
        cherrypy, "request", SimpleNamespace(method="GET", params={}, json={}), raising=False
    )
    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )


@pytest.fixture
def rf_api(cherrypy_ctx, tmp_path):
    """APIEndpoints over an autospecced StorageCollector.

    Autospec, not a bare mock: the endpoints reach the database through the
    collector facade, and a plain mock accepts any keyword, so a facade that had
    not kept up with the handler would answer every call happily here and raise
    TypeError on a real node.
    """
    del cherrypy_ctx
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = {}
    api.send_advert_func = None
    api.event_loop = None
    api.stats_getter = None
    api._config_path = str(tmp_path / "config.yaml")
    api.config_manager = MagicMock()

    storage = create_autospec(StorageCollector, instance=True)
    storage.get_noise_floor_history.return_value = [
        {"timestamp": 1.0, "noise_floor_dbm": -118.0, "radio_id": "local"}
    ]
    storage.get_noise_floor_stats.return_value = {"measurement_count": 2}
    storage.get_crc_error_count.return_value = 3
    storage.get_crc_error_history.return_value = [{"timestamp": 1.0, "count": 3}]
    api.daemon_instance = SimpleNamespace(repeater_handler=SimpleNamespace(storage=storage))
    return api, storage


def test_endpoints_reach_the_storage_facade_without_a_radio(rf_api):
    api, storage = rf_api

    assert api.noise_floor_history(hours="24", limit="5")["success"] is True
    assert api.noise_floor_stats(hours="12")["success"] is True
    assert api.crc_error_count(hours="6")["success"] is True
    assert api.crc_error_history(hours="6", limit="10")["success"] is True

    assert storage.get_noise_floor_history.call_args.kwargs["radio_id"] is None
    assert storage.get_crc_error_count.call_args.kwargs["radio_id"] is None


def test_endpoints_pass_the_named_radio_and_echo_it_back(rf_api):
    api, storage = rf_api
    api.config = BRIDGE_CONFIG

    history = api.noise_floor_history(hours="24", radio_id="link")
    stats = api.noise_floor_stats(hours="24", radio_id="link")
    count = api.crc_error_count(hours="24", radio_id="link")
    crc = api.crc_error_history(hours="24", radio_id="link")

    assert history["data"]["radio_id"] == "link"
    assert stats["data"]["radio_id"] == "link"
    assert count["data"]["radio_id"] == "link"
    assert crc["data"]["radio_id"] == "link"
    assert storage.get_noise_floor_history.call_args.kwargs["radio_id"] == "link"
    assert [
        profile["radio_id"]
        for profile in storage.get_noise_floor_history.call_args.kwargs["radio_profiles"]
    ] == ["local", "link"]


def test_a_single_radio_response_keeps_its_shape(rf_api):
    api, _ = rf_api

    payload = api.noise_floor_history(hours="24")["data"]
    crc = api.crc_error_history(hours="24")["data"]

    assert set(payload) == {"history", "hours", "count", "limit", "offset"}
    assert set(crc) == {"history", "hours", "count"}
