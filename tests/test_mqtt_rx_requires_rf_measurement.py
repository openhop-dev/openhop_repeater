"""A published packet record must mean the node heard it on the air.

A repeater and its companions share one process and one radio, so a lot of what reaches
``_publish_packet_to_mqtt`` was never received: packets the node originated or relayed, and
internal repeater<->companion traffic. Those records carry no RF measurement, and the MC2MQTT
schema has no way to say "not measured" -- so they went out as ``RSSI: "0"``, which aggregators
read as 0 dBm, the strongest reading physically possible.

The damage is on the map rather than in the logs. Whichever node sent the packet appears to be
sitting next to the observer, and since a node's own packet carries no path it lands at zero
hops -- an apparent direct decode. That is exactly the evidence people use to judge a link,
rank an observer, or choose a site for a repeater.
"""

from unittest.mock import MagicMock

from repeater.data_acquisition.storage_collector import StorageCollector

REQ_TYPE = 0
RAW = "0141" + "be50" + "deadbeef"


def _collector() -> tuple:
    """A StorageCollector carrying only the fields _publish_packet_to_mqtt touches."""
    collector = object.__new__(StorageCollector)
    collector.config = {"repeater": {"node_name": "test-node"}}
    collector.mqtt_handler = MagicMock()
    collector.mqtt_handler.public_key = "AB" * 32
    return collector, collector.mqtt_handler


def _record(rssi, snr) -> dict:
    return {
        "timestamp": 1700000000.0,
        "type": REQ_TYPE,
        "route": 1,
        "rssi": rssi,
        "snr": snr,
        "score": 0.5,
        "payload_length": 4,
        "packet_hash": "DEADBEEF" + "00" * 4,
        "raw_packet": RAW,
        "transmitted": False,
        "airtime_ms": 100.0,
    }


def test_a_measured_reception_is_published():
    """The ordinary path is unchanged."""
    collector, handler = _collector()
    collector._publish_packet_to_mqtt(_record(rssi=-90, snr=7.5))

    handler.publish_packet.assert_called_once()
    assert handler.publish_packet.call_args.args[0]["RSSI"] == "-90"


def test_a_packet_with_no_measurement_is_not_published():
    """Nothing measured it, so there is no reception to report."""
    collector, handler = _collector()
    collector._publish_packet_to_mqtt(_record(rssi=0, snr=0.0))

    handler.publish_packet.assert_not_called()


def test_zero_snr_with_a_real_rssi_is_a_real_reception():
    """0.0 dB SNR happens on the air; 0 dBm RSSI alongside it does not."""
    collector, handler = _collector()
    collector._publish_packet_to_mqtt(_record(rssi=-95, snr=0.0))

    handler.publish_packet.assert_called_once()


def test_a_weak_reception_is_still_a_reception():
    """A packet decoded at the noise floor is the most interesting kind."""
    collector, handler = _collector()
    collector._publish_packet_to_mqtt(_record(rssi=-122, snr=-14.5))

    handler.publish_packet.assert_called_once()


def test_missing_rssi_and_snr_keys_are_treated_as_unmeasured():
    """An absent measurement is not a strong one."""
    collector, handler = _collector()
    record = _record(rssi=0, snr=0.0)
    del record["rssi"]
    del record["snr"]
    collector._publish_packet_to_mqtt(record)

    handler.publish_packet.assert_not_called()
