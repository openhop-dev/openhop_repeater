"""The two MQTT publish filters, which both used to pass everything through.

``disallowed_packet_types`` compared the payload's ``type`` field - the
literal string "PACKET" that the MC2MQTT schema puts on every packet message -
against integer payload-type codes, so no configured type was ever blocked.
``skip_mqtt_if_invalid`` was threaded from the engine down to the publish call
and then never read, so packets this node could not parse were published to
every broker anyway.
"""

import json
from unittest.mock import MagicMock, patch

from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_collector import StorageCollector
from repeater.data_acquisition.storage_utils import PacketRecord

ADVERT = 4
TXT_MSG = 2


class _FakeIdentity:
    def __init__(self, public_key_hex: str):
        self._pk = bytes.fromhex(public_key_hex)

    def get_public_key(self) -> bytes:
        return self._pk


def _make_config(disallowed=None) -> dict:
    broker = {
        "name": "test-broker",
        "enabled": True,
        "host": "broker.example",
        "port": 1883,
        "transport": "tcp",
        "format": "letsmesh",
        "use_jwt_auth": False,
        "tls": {"enabled": False, "insecure": False},
    }
    if disallowed is not None:
        broker["disallowed_packet_types"] = disallowed
    return {
        "repeater": {"node_name": "test-node"},
        "radio_type": "sx1262",
        "radio": {
            "frequency": 869618000,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
            "preamble_length": 32,
        },
        "duty_cycle": {"max_airtime_per_minute": 3600},
        "mqtt_brokers": {"iata_code": "LAX", "status_interval": 0, "brokers": [broker]},
    }


def _capturing_pusher(config: dict):
    pusher = MeshCoreToMqttPusher(local_identity=_FakeIdentity("AB" * 32), config=config)
    conn = pusher.connections[0]
    captured = []
    conn._running = True
    conn.client = MagicMock()
    conn.client.publish = lambda topic, payload, retain=False, qos=0: captured.append(
        {"topic": topic, "payload": payload}
    )
    return pusher, captured


def _packet_payload(payload_type: int) -> dict:
    record = PacketRecord.from_packet_record(
        {
            "timestamp": 1700000000.0,
            "type": payload_type,
            "route": 1,
            "rssi": -90,
            "snr": 7.5,
            "score": 0.5,
            "payload_length": 32,
            "packet_hash": "DEADBEEF" + "00" * 4,
            "raw_packet": bytes(range(40)).hex(),
            "airtime_ms": 123.0,
        },
        origin="test-node",
        origin_id="AB" * 32,
    )
    assert record is not None
    return record.to_dict()


# --------------------------------------------------------------------
# disallowed_packet_types
# --------------------------------------------------------------------
def test_disallowed_packet_type_is_not_published():
    pusher, captured = _capturing_pusher(_make_config(disallowed=["ADVERT"]))

    pusher.publish_packet(_packet_payload(ADVERT))

    assert captured == []


def test_allowed_packet_type_still_publishes():
    pusher, captured = _capturing_pusher(_make_config(disallowed=["ADVERT"]))

    pusher.publish_packet(_packet_payload(TXT_MSG))

    assert len(captured) == 1
    assert json.loads(captured[0]["payload"])["packet_type"] == str(TXT_MSG)


def test_no_disallowed_list_publishes_every_type():
    pusher, captured = _capturing_pusher(_make_config())

    pusher.publish_packet(_packet_payload(ADVERT))
    pusher.publish_packet(_packet_payload(TXT_MSG))

    assert len(captured) == 2


def test_status_is_never_filtered_by_the_packet_type_list():
    """Status carries no packet type; a filtered broker must still get it."""
    pusher, captured = _capturing_pusher(_make_config(disallowed=["ADVERT"]))

    pusher.publish_status(state="online")

    assert len(captured) == 1
    assert json.loads(captured[0]["payload"])["status"] == "online"


def test_unreadable_packet_type_is_published_rather_than_dropped():
    """A payload we cannot classify is not evidence it was disallowed."""
    pusher, captured = _capturing_pusher(_make_config(disallowed=["ADVERT"]))
    payload = _packet_payload(ADVERT)
    payload["packet_type"] = "not-a-number"

    pusher.publish("packets", payload)

    assert len(captured) == 1


# --------------------------------------------------------------------
# skip_mqtt_if_invalid
# --------------------------------------------------------------------
def _make_collector(tmp_path) -> StorageCollector:
    config = {"storage": {"storage_dir": str(tmp_path)}, "repeater": {"node_name": "test-node"}}
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
    collector._publish_to_glass = MagicMock()
    collector.websocket_broadcast_packet = MagicMock()
    collector.websocket_available = True
    return collector


def _record(**overrides) -> dict:
    record = {
        "timestamp": 1700000000.0,
        "type": ADVERT,
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


def test_invalid_packet_is_withheld_from_brokers(tmp_path):
    collector = _make_collector(tmp_path)

    collector._publish_packet_sync(_record(drop_reason="Invalid advert"), skip_mqtt=True)

    collector.mqtt_handler.publish_packet.assert_not_called()


def test_invalid_packet_still_reaches_glass_and_the_dashboard(tmp_path):
    """The operator's own surfaces are how they debug their RF; only the
    network-wide observer feed is withheld."""
    collector = _make_collector(tmp_path)

    collector._publish_packet_sync(_record(drop_reason="Invalid advert"), skip_mqtt=True)

    collector._publish_to_glass.assert_called_once()
    collector.websocket_broadcast_packet.assert_called_once()


def test_operational_drops_are_still_published(tmp_path):
    """A duplicate or a policy drop is a valid packet this node chose not to
    repeat, which is exactly what an observer wants to see."""
    collector = _make_collector(tmp_path)

    collector._publish_packet_sync(_record(drop_reason="Duplicate"), skip_mqtt=False)

    collector.mqtt_handler.publish_packet.assert_called_once()


def test_the_callers_judgement_is_final(tmp_path):
    """The collector must not re-derive invalidity from drop_reason: traces,
    duplicates and policy drops all carry one and all belong on the feed."""
    collector = _make_collector(tmp_path)

    collector._publish_packet_sync(_record(), skip_mqtt=True)

    collector.mqtt_handler.publish_packet.assert_not_called()


def test_record_packet_publishes_by_default(tmp_path):
    """The default must be to publish. Callers that classify a packet as
    invalid say so; everyone else gets the observer feed."""
    collector = _make_collector(tmp_path)

    collector.record_packet(_record(drop_reason="trace_received"))
    collector._db_executor.shutdown(wait=True)

    collector.mqtt_handler.publish_packet.assert_called_once()


# --------------------------------------------------------------------
# TRACE, which carries an operational drop_reason on every record
# --------------------------------------------------------------------
def _trace_record(rx_radio_id=None):
    """A record built by the real TraceHelper, which bypasses the engine's
    _build_packet_record and writes its own."""
    from repeater.handler_helpers.trace import TraceHelper

    repeater_handler = MagicMock()
    repeater_handler.calculate_packet_score.return_value = 0.9
    repeater_handler.radio_config = {"spreading_factor": 8}
    helper = TraceHelper(local_hash=0x42, repeater_handler=repeater_handler)

    packet = MagicMock()
    packet.header = 0x09
    packet.payload = bytearray(b"\x01\x02")
    packet.path = bytearray()
    packet.path_len = 0
    packet.snr = 2.5
    packet.rssi = -70
    packet.get_payload_type.return_value = 0x09
    packet.get_route_type.return_value = 1
    packet.get_snr.return_value = 2.5
    packet.get_raw_length.return_value = 20
    packet.calculate_packet_hash.return_value = bytes.fromhex("A1B2C3D4E5F6A7B8")
    packet.write_to.return_value = b"\x01\x02\x03"
    # Both, so the MagicMock does not auto-create a truthy fallback attribute.
    packet._rx_radio_id = rx_radio_id
    packet.rx_radio_id = rx_radio_id

    return helper._create_trace_record(packet, {"trace_tag": 7, "auth_code": 0, "flags": 0})


def test_trace_records_carry_an_operational_drop_reason():
    """This is why the invalid filter cannot key on drop_reason alone."""
    assert _trace_record()["drop_reason"] == "trace_received"


def test_trace_reaches_the_brokers_through_the_default_path(tmp_path):
    """TraceHelper -> log_trace_record -> record_packet (no flag) -> MQTT."""
    collector = _make_collector(tmp_path)

    collector.record_packet(_trace_record())
    collector._db_executor.shutdown(wait=True)

    collector.mqtt_handler.publish_packet.assert_called_once()


def test_log_trace_record_does_not_ask_for_the_invalid_filter():
    """The engine's trace path relies on the default, so the default matters."""
    from unittest.mock import AsyncMock

    from repeater.engine import RepeaterHandler

    dispatcher = MagicMock()
    dispatcher.send_packet = AsyncMock()
    config = {
        "repeater": {"mode": "forward", "cache_ttl": 3600, "node_name": "test-node"},
        "duty_cycle": {"max_airtime_per_minute": 3600},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 125000,
            "coding_rate": 8,
            "preamble_length": 17,
        },
    }
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        handler = RepeaterHandler(config, dispatcher, 0x42, local_hash_bytes=bytes([0x42]))
    handler.storage = MagicMock()

    record = _trace_record()
    handler.log_trace_record(record)

    handler.storage.record_packet.assert_called_once()
    assert handler.storage.record_packet.call_args.kwargs.get("skip_mqtt", False) is False


def test_trace_record_carries_the_ingress_radio_on_a_multi_radio_node():
    """A trace is as much a bridge observation as any other packet."""
    assert _trace_record(rx_radio_id="link")["rx_radio_id"] == "link"
    assert _trace_record()["rx_radio_id"] is None
