"""Per-radio analytics for multi-radio nodes.

Packet stats, route stats, packet rates and neighbour-link history gain a
per-radio breakdown when two or more radio profiles are active. A single-radio
node must get exactly the answer it had, so each of those is checked against
the same database queried with no profiles at all.
"""

from __future__ import annotations

import time

import pytest
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import PH_TYPE_SHIFT, ROUTE_TYPE_FLOOD

from repeater.data_acquisition.sqlite_handler import (
    RADIO_PACKET_RATES_QUERY,
    RadioResolver,
    SQLiteHandler,
    packet_carriers,
)
from repeater.neighbour_links import NeighbourLinkTracker

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


def _store(handler: SQLiteHandler, **overrides) -> None:
    record = {
        "timestamp": time.time() - 60,
        "type": 1,
        "route": 1,
        "length": 40,
        "transmitted": False,
        "is_duplicate": False,
        "rssi": -90,
        "snr": 5.0,
    }
    record.update(overrides)
    handler.store_packet(record)


@pytest.fixture
def handler(tmp_path) -> SQLiteHandler:
    return SQLiteHandler(tmp_path)


@pytest.fixture
def bridge_db(handler) -> SQLiteHandler:
    """One of every attribution case on a two-radio bridge."""
    # Heard on local, not repeated.
    _store(handler, rx_radio_id="local", rssi=-80, snr=8.0)
    # A duplicate heard on link.
    _store(
        handler, rx_radio_id="link", rssi=-100, snr=-2.0, is_duplicate=True, drop_reason="Duplicate"
    )
    # Heard on local, relayed out both radios.
    _store(
        handler,
        route=2,
        rx_radio_id="local",
        rssi=-70,
        snr=10.0,
        transmitted=True,
        tx_radio_id="link",
        tx_radio_ids=["link", "local"],
    )
    # Originated here and sent on both radios; no radio received it.
    _store(
        handler,
        route=2,
        rssi=0,
        snr=0.0,
        transmitted=True,
        tx_radio_id="local",
        tx_radio_ids=["local", "link"],
    )
    # A reception whose radio is not recorded.
    _store(handler, rx_radio_id=None)
    return handler


# ---------------------------------------------------------------------------
# Shared attribution rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        ((False, "local", None, None), (["local"], [])),
        ((False, None, None, None), ([None], [])),
        ((True, "local", "link", '["link", "local"]'), (["local"], ["link", "local"])),
        ((True, None, "local", '["local"]'), ([], ["local"])),
        ((True, "link", "local", None), (["link"], ["local"])),  # before fan-out
        ((True, "link", "local", "not json"), (["link"], ["local"])),
    ],
)
def test_packet_carriers(row, expected):
    assert packet_carriers(*row) == expected


def test_resolver_gives_one_radio_everything_and_two_radios_only_known_ids():
    single = RadioResolver(SINGLE)
    assert not single.multi
    assert single.resolve(None) == "radio0"
    assert single.resolve("anything") == "radio0"

    bridge = RadioResolver(BRIDGE)
    assert bridge.multi and bridge.order == ["local", "link"]
    assert bridge.resolve("link") == "link"
    assert bridge.resolve(None) is None
    assert bridge.resolve("decommissioned") is None


# ---------------------------------------------------------------------------
# /packet_stats
# ---------------------------------------------------------------------------


def test_single_radio_packet_stats_are_unchanged(bridge_db):
    assert bridge_db.get_packet_stats(
        hours=24, radio_profiles=SINGLE
    ) == bridge_db.get_packet_stats(hours=24)
    assert "radios" not in bridge_db.get_packet_stats(hours=24, radio_profiles=SINGLE)


def test_bridge_packet_stats_keep_the_legacy_totals(bridge_db):
    legacy = bridge_db.get_packet_stats(hours=24)
    bridge = bridge_db.get_packet_stats(hours=24, radio_profiles=BRIDGE)

    for key, value in legacy.items():
        assert bridge[key] == value, key
    # One transmitted packet per relay, however many radios carried it.
    assert bridge["transmitted_packets"] == 2


def test_bridge_packet_stats_break_down_per_radio(bridge_db):
    stats = bridge_db.get_packet_stats(hours=24, radio_profiles=BRIDGE)
    local, link = stats["radios"]

    assert local == {
        "radio_id": "local",
        "received": 2,
        "duplicates": 0,
        "dropped": 1,
        "transmissions": 2,
        "avg_rssi": -75.0,
        "avg_snr": 9.0,
    }
    assert link == {
        "radio_id": "link",
        "received": 1,
        "duplicates": 1,
        "dropped": 1,
        "transmissions": 2,
        "avg_rssi": -100.0,
        "avg_snr": -2.0,
    }
    assert stats["unattributed_rx_count"] == 1
    assert stats["unattributed_tx_count"] == 0


def test_packet_stats_cache_does_not_mix_single_and_bridge_answers(bridge_db):
    assert "radios" not in bridge_db.get_packet_stats(hours=24)
    assert "radios" in bridge_db.get_packet_stats(hours=24, radio_profiles=BRIDGE)
    assert "radios" not in bridge_db.get_packet_stats(hours=24)


def test_bridge_packet_stats_on_an_empty_window_match_legacy(handler):
    assert {
        k: v
        for k, v in handler.get_packet_stats(hours=24, radio_profiles=BRIDGE).items()
        if k not in ("radios", "unattributed_rx_count", "unattributed_tx_count")
    } == handler.get_packet_stats(hours=24)


# ---------------------------------------------------------------------------
# /route_stats
# ---------------------------------------------------------------------------


def test_single_radio_route_stats_are_unchanged(bridge_db):
    single = bridge_db.get_route_stats(hours=24, radio_profiles=SINGLE)
    assert single == bridge_db.get_route_stats(hours=24)
    assert single["route_totals"] == {"Flood": 3, "Direct": 2}
    assert "radios" not in single


def test_bridge_route_stats_split_receptions_per_radio(bridge_db):
    stats = bridge_db.get_route_stats(hours=24, radio_profiles=BRIDGE)

    assert stats["route_totals"] == bridge_db.get_route_stats(hours=24)["route_totals"]
    assert stats["radios"] == [
        {"radio_id": "local", "route_totals": {"Flood": 1, "Direct": 1}, "total_packets": 2},
        {"radio_id": "link", "route_totals": {"Flood": 1}, "total_packets": 1},
    ]
    assert stats["originated"] == {"route_totals": {"Direct": 1}, "total_packets": 1}
    assert stats["unattributed_count"] == 1


# ---------------------------------------------------------------------------
# /radio_packet_rates
# ---------------------------------------------------------------------------


def test_packet_rates_follow_the_airtime_attribution(bridge_db):
    now = time.time()
    rates = bridge_db.get_radio_packet_rates(now - 3600, now, 3600, radio_profiles=BRIDGE)
    airtime = bridge_db.get_airtime_buckets(now - 3600, now, 3600, radio_profiles=BRIDGE)

    by_radio = {r["radio_id"]: r for r in rates["radios"]}
    assert (by_radio["local"]["rx_total"], by_radio["local"]["tx_total"]) == (2, 2)
    assert (by_radio["link"]["rx_total"], by_radio["link"]["tx_total"]) == (1, 2)
    for radio in airtime["radios"]:
        assert by_radio[radio["radio_id"]]["rx_total"] == radio["rx_total"]
        assert by_radio[radio["radio_id"]]["tx_total"] == radio["tx_total"]
    assert rates["unattributed_rx_count"] == airtime["unattributed_rx_count"] == 1
    assert rates["unattributed_tx_count"] == airtime["unattributed_tx_count"] == 0

    # Every row sits in one bucket, so each radio's bucket counts sum to its totals.
    for radio in rates["radios"]:
        assert sum(b["rx_count"] for b in radio["buckets"]) == radio["rx_total"]
        assert sum(b["tx_count"] for b in radio["buckets"]) == radio["tx_total"]
        assert all(b["timestamp"] % 3600 == 0 for b in radio["buckets"])


def test_packet_rates_attribute_null_ids_to_a_single_radio(handler):
    _store(handler, rx_radio_id=None)
    _store(handler, transmitted=True, rx_radio_id=None, tx_radio_id=None)
    now = time.time()

    rates = handler.get_radio_packet_rates(now - 3600, now, 3600, radio_profiles=SINGLE)

    assert [(r["radio_id"], r["rx_total"], r["tx_total"]) for r in rates["radios"]] == [
        ("radio0", 1, 1)
    ]
    assert rates["unattributed_rx_count"] == 0


def test_week_of_packet_rates_is_served_from_the_covering_index(handler):
    now = time.time()
    with handler._connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + RADIO_PACKET_RATES_QUERY, (3600, 3600, now - 7 * 86400, now)
        ).fetchall()
    assert any("COVERING INDEX idx_packets_airtime" in str(tuple(row)) for row in plan), plan


# ---------------------------------------------------------------------------
# /neighbor_link_history
# ---------------------------------------------------------------------------


@pytest.fixture
def link_history(handler) -> SQLiteHandler:
    base = time.time() - 600
    for i, radio_id in enumerate(["local", "link", "local", None]):
        _store(
            handler,
            timestamp=base + i,
            upstream_hash="AB",
            upstream_hash_size=1,
            rx_radio_id=radio_id,
            rssi=-80 - i,
        )
    return handler


def test_history_rows_name_the_receiving_radio(link_history):
    rows = link_history.get_neighbor_link_history(peer_hash="ab", path_hash_size=1)

    assert [row.get("rx_radio_id") for row in rows] == ["local", "link", "local", None]
    assert "rx_radio_id" not in rows[-1]  # absent, not null, when unrecorded


def test_history_filters_to_one_radio(link_history):
    rows = link_history.get_neighbor_link_history(
        peer_hash="AB", path_hash_size=1, radio_id="local"
    )
    assert [row["rssi"] for row in rows] == [-80, -82]

    buckets = link_history.get_neighbor_link_history(
        peer_hash="AB", path_hash_size=1, bucket_seconds=3600, radio_id="link"
    )
    assert sum(b["n"] for b in buckets) == 1


def test_history_buckets_split_per_radio_only_when_asked(link_history):
    merged = link_history.get_neighbor_link_history(
        peer_hash="AB", path_hash_size=1, bucket_seconds=86400
    )
    assert len(merged) == 1 and merged[0]["n"] == 4
    assert "radio_id" not in merged[0]

    split = link_history.get_neighbor_link_history(
        peer_hash="AB", path_hash_size=1, bucket_seconds=86400, by_radio=True
    )
    assert {b["radio_id"]: b["n"] for b in split} == {"local": 2, "link": 1, None: 1}


# ---------------------------------------------------------------------------
# Neighbour link tracker
# ---------------------------------------------------------------------------


def _flood_from(peer: int) -> Packet:
    pkt = Packet()
    pkt.header = ROUTE_TYPE_FLOOD | (1 << PH_TYPE_SHIFT)
    pkt.payload = bytearray(b"\x01\x02\x03")
    pkt.payload_len = 3
    pkt.path = bytearray([peer])
    pkt.path_len = 1
    return pkt


def _observe(tracker, rx_radio_id, rssi, is_duplicate=False):
    tracker.observe(
        _flood_from(0xAB),
        route_type=ROUTE_TYPE_FLOOD,
        payload_type=1,
        rssi=rssi,
        snr=5.0,
        score=0.5,
        is_duplicate=is_duplicate,
        rx_radio_id=rx_radio_id,
    )


def test_tracker_keeps_merged_and_per_radio_link_stats():
    tracker = NeighbourLinkTracker({})
    _observe(tracker, "link", -100)
    _observe(tracker, "local", -70)
    _observe(tracker, "local", -60, is_duplicate=True)

    (link,) = tracker.snapshot(radio_ids=["local", "link"])

    assert link["sample_count"] == 3 and link["duplicate_sample_count"] == 1
    assert [r["radio_id"] for r in link["radios"]] == ["local", "link"]  # configured order
    local, far = link["radios"]
    assert (local["sample_count"], local["duplicate_sample_count"]) == (2, 1)
    assert (local["last_rssi"], far["last_rssi"]) == (-60.0, -100.0)
    assert far["sample_count"] == 1 and far["ewma_rssi"] == -100.0


def test_tracker_snapshot_is_unchanged_without_two_radios():
    tracker = NeighbourLinkTracker({})
    _observe(tracker, None, -80)
    _observe(tracker, "radio0", -80)

    for radio_ids in (None, [], ["radio0"]):
        (link,) = tracker.snapshot(radio_ids=radio_ids)
        assert "radios" not in link


def test_tracker_lists_a_radio_no_longer_configured_after_the_configured_ones():
    tracker = NeighbourLinkTracker({})
    _observe(tracker, "old", -90)
    _observe(tracker, "link", -80)

    (link,) = tracker.snapshot(radio_ids=["local", "link"])

    assert [r["radio_id"] for r in link["radios"]] == ["link", "old"]
