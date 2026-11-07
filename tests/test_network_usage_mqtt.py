"""Accounting at the real local Paho handoff (no broker/network connections)."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repeater.data_acquisition.glass_handler import GlassHandler
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.network_usage import NetworkUsageCollector
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def make_pusher(collector, legacy=False):
    broker = {
        "name": "Main",
        "host": "example.test",
        "port": 1883,
        "enabled": True,
        "format": "mqtt",
        "use_jwt_auth": False,
        "tls": {"enabled": False},
    }
    config = {"repeater": {"node_name": "Test"}, "mqtt_brokers": {"brokers": [broker]}}
    if legacy:
        config = {
            "repeater": {"node_name": "Test"},
            "mqtt": {"broker": "example.test", "port": 1883, "enabled": True},
        }
    return MeshCoreToMqttPusher(
        SimpleNamespace(get_public_key=lambda: b"a" * 32),
        config,
        usage_recorder=collector.record_mqtt,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_every_subtopic_accounts_only_local_accepted_utf8(tmp_path, legacy):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    pusher = make_pusher(collector, legacy)
    conn = pusher.connections[0]
    conn.client = Mock()
    conn._running = True
    conn.client.publish.return_value = SimpleNamespace(rc=0)
    for subtopic in ["packets", "status", "neighbors", "custom"]:
        conn.publish(subtopic, "é")
    conn.client.publish.return_value = SimpleNamespace(rc=4)
    conn.publish("packets", "rejected")
    conn._running = False
    conn.publish("status", "offline")
    collector.flush()
    totals = collector.database.get_usage_totals(0, 180)
    assert sum(row["upload_bytes"] for row in totals) == 8
    assert sum(row["messages"] for row in totals) == 4
    assert conn.sent == {"messages": 4, "payload_bytes": 8}


def test_concurrent_publishers_do_not_lose_deltas(tmp_path):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    pusher = make_pusher(collector)
    conn = pusher.connections[0]
    conn.client = Mock()
    conn.client.publish.return_value = SimpleNamespace(rc=0)
    conn._running = True
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: conn.publish("packets", "é"), range(1000)))
    collector.flush()
    assert collector.database.get_usage_totals(0, 180)[0]["upload_bytes"] == 2000
    assert conn.sent == {"messages": 1000, "payload_bytes": 2000}


def test_session_sent_snapshot_is_a_copy(tmp_path):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    conn = make_pusher(collector).connections[0]
    snapshot = conn.sent
    snapshot["messages"] = 99
    assert conn.sent == {"messages": 0, "payload_bytes": 0}


def test_session_snapshot_cannot_read_between_message_and_byte_updates(tmp_path):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    conn = make_pusher(collector).connections[0]
    halfway, release, reading = Event(), Event(), Event()

    class PausingCounters(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "messages":
                halfway.set()
                assert release.wait(2), "Writer was not released"

    conn._sent = PausingCounters(conn.sent)
    conn._running = True
    conn.client = Mock()
    conn.client.publish.return_value = SimpleNamespace(rc=0)

    def read_snapshot():
        reading.set()
        return conn.sent

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(conn.publish, "status", "é")
        try:
            assert halfway.wait(2)
            reader = pool.submit(read_snapshot)
            assert reading.wait(2)
            with pytest.raises(FutureTimeoutError):
                reader.result(timeout=0.05)
        finally:
            release.set()
        writer.result(timeout=2)
        assert reader.result(timeout=2) == {"messages": 1, "payload_bytes": 2}


def test_glass_mqtt_payload_also_uses_local_acceptance(tmp_path):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    storage = SimpleNamespace(network_usage=collector)
    glass = GlassHandler(
        {}, daemon_instance=SimpleNamespace(repeater_handler=SimpleNamespace(storage=storage))
    )
    glass.enabled = glass.mqtt_enabled = glass._mqtt_ready = True
    glass._mqtt_client = Mock()
    glass._mqtt_client.publish.return_value = SimpleNamespace(rc=0)
    glass.publish_telemetry("event", {"timestamp": 120, "value": "é"})
    message = glass._mqtt_client.publish.call_args.args[1]
    collector.flush()
    totals = collector.database.get_usage_totals(0, 180)
    assert len(totals) == 1
    assert totals[0]["upload_bytes"] == len(message.encode("utf-8"))
    glass._mqtt_client.publish.return_value = SimpleNamespace(rc=4)
    glass.publish_telemetry("event", {"timestamp": 120})
    collector.flush()
    assert collector.database.get_usage_totals(0, 180)[0]["messages"] == 1


def test_storage_lifecycle_wires_collector_before_mqtt_connect(tmp_path, monkeypatch):
    from repeater.data_acquisition import storage_collector
    from repeater.data_acquisition.storage_collector import StorageCollector

    publisher = Mock()

    def build(*args, **kwargs):
        # Constructor wiring must precede connect (which can immediately publish status).
        kwargs["usage_recorder"]("mqtt:test", "Test", "startup")
        return publisher

    monkeypatch.setattr(storage_collector, "MeshCoreToMqttPusher", build)
    storage = StorageCollector(
        {
            "storage": {"storage_dir": str(tmp_path)},
            "metrics": {"rrd_enabled": False},
            "mqtt": {"broker": "example.test"},
        },
        local_identity=SimpleNamespace(get_public_key=lambda: b"a" * 32),
    )
    try:
        assert storage.network_usage.running is True
    finally:
        storage.close()
    assert storage.network_usage.running is False
    assert (
        sum(row["messages"] for row in storage.sqlite_handler.get_usage_totals(0, 9999999999)) == 1
    )
    publisher.disconnect.assert_called_once()


def test_legacy_endpoint_userinfo_never_enters_usage_names(tmp_path):
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 120)
    config = {
        "mqtt": {
            "broker": "mqtt://user:private@example.test/path?token=private",
            "port": 1883,
            "enabled": True,
        }
    }
    pusher = MeshCoreToMqttPusher(
        SimpleNamespace(get_public_key=lambda: b"a" * 32),
        config,
        usage_recorder=collector.record_mqtt,
    )
    conn = pusher.connections[0]
    conn.client = Mock()
    conn.client.publish.return_value = SimpleNamespace(rc=0)
    conn._running = True
    conn.publish("status", "a")
    collector.flush()
    _first, _count, names = collector.database.get_usage_coverage(0, 180)
    assert "private" not in str(names)
    assert "user" not in str(names)
