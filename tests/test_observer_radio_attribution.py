"""Radio attribution on the MQTT observer feed.

A two-radio bridge is one MeshCore node with one pubkey, and the path byte it
appends is the same whichever radio carried the packet, so nothing on the air
says which band a hop used. The node itself does know, and these tests lock the
two halves that let an observer read it: the ingress/egress radio ids on each
published packet, and the id -> frequency map in the status message that gives
those ids meaning.

Single-radio nodes must publish exactly what they published before any of this
existed.
"""

import json
from unittest.mock import MagicMock

from repeater.config import build_radio_status_entries, get_node_info
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_utils import PacketRecord

LOCAL_RADIO = {
    "id": "local",
    "radio_type": "sx1262",
    "radio": {
        "frequency": 869618000,
        "bandwidth": 62500,
        "spreading_factor": 8,
        "coding_rate": 8,
        "preamble_length": 32,
    },
}
LINK_RADIO = {
    "id": "link",
    "radio_type": "sx1262",
    "radio": {
        "frequency": 864200000,
        "bandwidth": 62500,
        "spreading_factor": 11,
        "coding_rate": 8,
        "preamble_length": 32,
    },
}


class _FakeIdentity:
    def __init__(self, public_key_hex: str):
        self._pk = bytes.fromhex(public_key_hex)

    def get_public_key(self) -> bytes:
        return self._pk


def _packet_record(**overrides) -> dict:
    record = {
        "timestamp": 1700000000.0,
        "type": 4,
        "route": 1,
        "rssi": -90,
        "snr": 7.5,
        "score": 0.5,
        "payload_length": 32,
        "packet_hash": "DEADBEEF" + "00" * 4,
        "raw_packet": bytes(range(40)).hex(),
        "airtime_ms": 123.0,
    }
    record.update(overrides)
    return record


def _serialize(record: dict, include_radio_ids: bool) -> dict:
    packet = PacketRecord.from_packet_record(
        record,
        origin="test-node",
        origin_id="AB" * 32,
        include_radio_ids=include_radio_ids,
    )
    assert packet is not None
    return packet.to_dict()


# --------------------------------------------------------------------
# Packet payload
# --------------------------------------------------------------------
def test_single_radio_payload_carries_no_radio_ids():
    """The ids say nothing a single-radio status message doesn't already say."""
    payload = _serialize(
        _packet_record(rx_radio_id="radio0", tx_radio_id="radio0", tx_radio_ids=["radio0"]),
        include_radio_ids=False,
    )

    assert "rx_radio_id" not in payload
    assert "tx_radio_ids" not in payload


def test_single_radio_payload_is_unchanged_from_pre_attribution_builds():
    """Observers parsing the old schema must see byte-identical JSON."""
    expected_keys = {
        "origin",
        "origin_id",
        "timestamp",
        "type",
        "direction",
        "time",
        "date",
        "len",
        "packet_type",
        "route",
        "payload_len",
        "raw",
        "SNR",
        "RSSI",
        "score",
        "duration",
        "hash",
    }

    assert set(_serialize(_packet_record(), include_radio_ids=False)) == expected_keys


def test_bridge_egress_on_both_sides_is_one_packet_with_both_radios():
    """Repeating on both sides duplicates the TX, not the upload: one payload
    naming both egress radios, not two payloads."""
    payload = _serialize(
        _packet_record(
            rx_radio_id="local",
            tx_radio_id="link",
            tx_radio_ids=["link", "local"],
        ),
        include_radio_ids=True,
    )

    assert payload["rx_radio_id"] == "local"
    assert payload["tx_radio_ids"] == ["link", "local"]


def test_tx_radio_ids_is_a_list_even_for_a_single_egress():
    """A consumer must never have to branch on the JSON type of this field."""
    payload = _serialize(
        _packet_record(rx_radio_id="local", tx_radio_id="link"),
        include_radio_ids=True,
    )

    assert payload["tx_radio_ids"] == ["link"]


def test_packet_heard_but_not_repeated_has_no_egress_key():
    """Absent, not empty: the node received it and transmitted nothing."""
    payload = _serialize(
        _packet_record(rx_radio_id="link", tx_radio_id=None, tx_radio_ids=None),
        include_radio_ids=True,
    )

    assert payload["rx_radio_id"] == "link"
    assert "tx_radio_ids" not in payload


def test_pre_fabric_rows_with_null_ids_omit_the_fields():
    """Rows written before radio attribution existed carry NULL ids."""
    payload = _serialize(_packet_record(), include_radio_ids=True)

    assert "rx_radio_id" not in payload
    assert "tx_radio_ids" not in payload


# --------------------------------------------------------------------
# Status radio map
# --------------------------------------------------------------------
def test_radio_status_entries_map_each_id_to_its_air_settings():
    entries = build_radio_status_entries(
        {"radios": [LOCAL_RADIO, LINK_RADIO], "fabric": {"default_radio": "local"}}
    )

    assert entries == [
        {"id": "local", "radio": "869.618,62.5,8,8"},
        {"id": "link", "radio": "864.2,62.5,11,8"},
    ]


def test_radio_status_entry_matches_the_single_radio_status_string():
    """The per-radio value reuses the top-level ``radio`` format, so consumers
    need no second parser."""
    config = {
        "radio_type": "sx1262",
        "radio": LOCAL_RADIO["radio"],
        "repeater": {"node_name": "test-node"},
    }

    entries = build_radio_status_entries(config)

    assert len(entries) == 1
    assert entries[0]["radio"] == get_node_info(config)["radio_config"]


def test_unreadable_radio_settings_publish_no_map_at_all():
    """A partial map would attribute a packet to the wrong band."""
    broken = {"id": "link", "radio_type": "sx1262", "radio": {"frequency": "not-a-number"}}

    assert build_radio_status_entries({"radios": [LOCAL_RADIO, broken]}) == []


# --------------------------------------------------------------------
# Status message on the wire
# --------------------------------------------------------------------
def _make_config(radios=None) -> dict:
    config = {
        "repeater": {"node_name": "test-node"},
        "radio_type": "sx1262",
        "radio": LOCAL_RADIO["radio"],
        "duty_cycle": {"max_airtime_per_minute": 3600},
        "mqtt_brokers": {
            "iata_code": "LAX",
            "status_interval": 0,
            "brokers": [
                {
                    "name": "test-broker",
                    "enabled": True,
                    "host": "broker.example",
                    "port": 1883,
                    "transport": "tcp",
                    "format": "letsmesh",
                    "use_jwt_auth": False,
                    "tls": {"enabled": False, "insecure": False},
                }
            ],
        },
    }
    if radios:
        config["radios"] = radios
        config["fabric"] = {"tx_mode": "bridge", "default_radio": "link"}
    return config


def _publish_status(config: dict) -> dict:
    pusher = MeshCoreToMqttPusher(local_identity=_FakeIdentity("AB" * 32), config=config)
    conn = pusher.connections[0]
    captured = []
    conn._running = True
    conn.client = MagicMock()
    conn.client.publish = lambda topic, payload, retain=False, qos=0: captured.append(payload)

    pusher.publish_status(state="online")

    assert len(captured) == 1
    return json.loads(captured[0])


def test_status_publishes_the_radio_map_for_a_two_radio_node():
    status = _publish_status(_make_config(radios=[LOCAL_RADIO, LINK_RADIO]))

    assert status["radios"] == [
        {"id": "local", "radio": "869.618,62.5,8,8"},
        {"id": "link", "radio": "864.2,62.5,11,8"},
    ]


def test_status_radio_field_reports_the_default_radio_on_a_fabric_node():
    """The top-level ``radio:`` block is the base entries inherit from, so it
    can name a band no radio is on. Observers ignoring ``radios`` get the
    radio Fabric transmits on by default."""
    status = _publish_status(_make_config(radios=[LOCAL_RADIO, LINK_RADIO]))

    assert status["radio"] == "864.2,62.5,11,8"


def test_single_radio_status_has_no_radio_map():
    status = _publish_status(_make_config())

    assert "radios" not in status
    assert status["radio"] == "869.618,62.5,8,8"


def test_status_ids_resolve_every_radio_id_a_packet_can_name():
    """The two halves must join: every id the packet payload can carry has an
    entry in the status map."""
    config = _make_config(radios=[LOCAL_RADIO, LINK_RADIO])
    status = _publish_status(config)
    payload = _serialize(
        _packet_record(rx_radio_id="local", tx_radio_id="link", tx_radio_ids=["link", "local"]),
        include_radio_ids=True,
    )

    known = {entry["id"] for entry in status["radios"]}
    assert payload["rx_radio_id"] in known
    assert set(payload["tx_radio_ids"]) <= known


# --------------------------------------------------------------------
# Collector wiring
# --------------------------------------------------------------------
def _make_collector(tmp_path, radios=None):
    """A StorageCollector with its storage backends stubbed out."""
    from unittest.mock import patch

    from repeater.data_acquisition.storage_collector import StorageCollector

    config = {"storage": {"storage_dir": str(tmp_path)}, "repeater": {"node_name": "test-node"}}
    config["radio_type"] = "sx1262"
    config["radio"] = LOCAL_RADIO["radio"]
    if radios:
        config["radios"] = radios

    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler"),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(config=config)

    collector._stats_stop_event.set()
    if collector._stats_thread is not None:
        collector._stats_thread.join(timeout=1)
    collector.mqtt_handler = MagicMock()
    collector.mqtt_handler.public_key = "AB" * 32
    return collector


def _published_payload(collector, record) -> dict:
    collector._publish_packet_to_mqtt(record)
    collector.mqtt_handler.publish_packet.assert_called_once()
    return collector.mqtt_handler.publish_packet.call_args[0][0]


def test_collector_publishes_radio_ids_on_a_two_radio_node(tmp_path):
    collector = _make_collector(tmp_path, radios=[LOCAL_RADIO, LINK_RADIO])
    assert collector._multi_radio is True

    payload = _published_payload(
        collector,
        _packet_record(rx_radio_id="local", tx_radio_id="link", tx_radio_ids=["link", "local"]),
    )

    assert payload["rx_radio_id"] == "local"
    assert payload["tx_radio_ids"] == ["link", "local"]


def test_collector_omits_radio_ids_on_a_single_radio_node(tmp_path):
    collector = _make_collector(tmp_path)
    assert collector._multi_radio is False

    payload = _published_payload(
        collector, _packet_record(rx_radio_id="radio0", tx_radio_id="radio0")
    )

    assert "rx_radio_id" not in payload
    assert "tx_radio_ids" not in payload
