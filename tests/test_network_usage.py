import threading
from types import SimpleNamespace

from repeater.data_acquisition.network_usage import NetworkUsageCollector
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def test_snapshot_is_consistent_when_flush_commits_between_reads(tmp_path, monkeypatch):
    db = SQLiteHandler(tmp_path)
    end = 86400
    db.store_usage_batch("seed", [], {}, set(range(0, end - 60, 60)), end)
    collector = NetworkUsageCollector(db, clock=lambda: end, counters=dict)
    collector._pending[("mqtt", "mqtt:test", end - 60)] = [3, 0, 1]
    collector._names[("mqtt", "mqtt:test")] = "Test"
    collector._coverage.add(end - 60)
    read_totals = db.get_usage_totals
    flushed = []

    def totals_then_concurrent_flush(start, stop):
        rows = read_totals(start, stop)
        writer = threading.Thread(target=lambda: flushed.append(collector.flush()))
        writer.start()
        writer.join(5)
        assert not writer.is_alive()
        assert flushed == [True]
        return rows

    monkeypatch.setattr(db, "get_usage_totals", totals_then_concurrent_flush)
    report = collector.snapshot(1)
    # Both views must describe the state before the concurrent transaction.
    assert report["partial"] is True
    assert report["mqtt"]["upload_bytes"] == 0
    monkeypatch.setattr(db, "get_usage_totals", read_totals)
    updated = collector.snapshot(1)
    assert updated["partial"] is False
    assert updated["mqtt"]["upload_bytes"] == 3
    assert updated["mqtt"]["brokers"][0]["name"] == "Test"


def test_interface_sampler_persists_deltas_not_lifetime_or_aggregate(tmp_path):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    now = [120.0]
    counters = {"eth0": SimpleNamespace(bytes_sent=1000, bytes_recv=2000)}
    db = SQLiteHandler(tmp_path)
    collector = NetworkUsageCollector(db, clock=lambda: now[0], counters=lambda: counters)
    collector.sample()
    now[0] = 130
    counters["eth0"] = SimpleNamespace(bytes_sent=1030, bytes_recv=2040)
    collector.sample()
    collector.flush()
    assert db.get_usage_totals(120, 180) == [
        {
            "kind": "interface",
            "source": "eth0",
            "upload_bytes": 30,
            "download_bytes": 40,
            "messages": 0,
        }
    ]
    report = collector.snapshot(1)
    assert report["partial"] is True
    assert report["end"] == 120
    assert report["mqtt"]["upload_bytes"] == 0
    assert "namespace" in report["scope"]


def test_retry_after_commit_is_idempotent_and_mqtt_keeps_accumulating(tmp_path, monkeypatch):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector, broker_identity

    now = [120.0]
    db = SQLiteHandler(tmp_path)
    collector = NetworkUsageCollector(db, clock=lambda: now[0], counters=dict)
    source = broker_identity({"host": "example.test", "port": 1883, "name": "Main"})
    collector.record_mqtt(source, "Main", "é")
    store = db.store_usage_batch

    def committed_then_failed(*args, **kwargs):
        store(*args, **kwargs)
        raise OSError("secret connection details")

    monkeypatch.setattr(db, "store_usage_batch", committed_then_failed)
    assert collector.flush() is False
    collector.record_mqtt(source, "Main", "a")
    monkeypatch.setattr(db, "store_usage_batch", store)
    assert collector.flush() is True
    collector.flush()
    now[0] = 180
    report = collector.snapshot(1)
    assert report["mqtt"] == {
        "upload_bytes": 3,
        "messages": 2,
        "brokers": [{"source": source, "name": "Main", "upload_bytes": 3, "messages": 2}],
    }
    assert report["collection"]["error"] is None
    assert (
        broker_identity(
            {"host": "example.test", "port": 1883, "name": "Main", "password": "private"}
        )
        == source
    )
    assert "example" not in source


def test_resets_disappearance_sampling_errors_and_long_gaps_are_not_usage(tmp_path):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    now = [120.0]
    values = {"eth0": SimpleNamespace(bytes_sent=100, bytes_recv=100)}

    def read():
        if values.get("error"):
            raise RuntimeError("private details")
        return dict(values)

    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: now[0], counters=read)
    collector.sample()
    for timestamp, counters in [
        (130, {"eth0": SimpleNamespace(bytes_sent=5, bytes_recv=5)}),
        (140, {}),
        (150, {"eth0": SimpleNamespace(bytes_sent=1000, bytes_recv=1000)}),
        (160, {"error": True}),
        (170, {"eth0": SimpleNamespace(bytes_sent=2000, bytes_recv=2000)}),
        (300, {"eth0": SimpleNamespace(bytes_sent=3000, bytes_recv=3000)}),
    ]:
        now[0] = timestamp
        values.clear()
        values.update(counters)
        collector.sample()
    collector.flush()
    assert all(row["upload_bytes"] == 0 for row in collector.database.get_usage_totals(0, 360))
    assert collector.snapshot(1)["partial"] is True


def test_coverage_persists_quiet_minutes_and_marks_restart_gap(tmp_path):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    now = [0.0]
    db = SQLiteHandler(tmp_path)
    collector = NetworkUsageCollector(db, clock=lambda: now[0], counters=dict)
    for timestamp in range(0, 86401, 10):
        now[0] = timestamp
        collector.sample()
        collector.flush()
    report = collector.snapshot(1)
    assert report["available_since"] == 0
    assert report["partial"] is False
    assert report["mqtt"]["messages"] == 0
    now[0] += 120
    restarted = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: now[0], counters=dict)
    restarted.sample()
    restarted.flush()
    assert restarted.snapshot(1)["partial"] is True


def test_worker_shutdown_flushes_and_reclaims_thread(tmp_path):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), counters=dict)
    collector.start()
    collector.record_mqtt("mqtt:test", "Test", "abc")
    collector.stop()
    assert collector.running is False
    assert not collector._thread.is_alive()
    assert collector.database.get_usage_totals(0, 9999999999)[0]["messages"] == 1


def test_pending_memory_bounded_on_database_outage(tmp_path, monkeypatch):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    now = [120.0]
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: now[0], counters=dict)
    monkeypatch.setattr(
        collector.database, "store_usage_batch", lambda *args: (_ for _ in ()).throw(OSError())
    )
    for i in range(5000):
        now[0] += 60
        collector.record_mqtt("mqtt:" + str(i), str(i), "a")
        collector.flush()
    assert len(collector._pending) <= collector.MAX_PENDING
    assert len(collector._names) <= collector.MAX_PENDING
    assert collector.snapshot(1)["partial"] is True


def test_expired_retry_cannot_replay_after_receipt_retention(tmp_path, monkeypatch):
    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    now = [120.0]
    db = SQLiteHandler(tmp_path)
    collector = NetworkUsageCollector(db, clock=lambda: now[0], counters=dict)
    collector.record_mqtt("mqtt:test", "Test", "abc")
    store = db.store_usage_batch

    def ambiguous(*args):
        store(*args)
        raise OSError()

    monkeypatch.setattr(db, "store_usage_batch", ambiguous)
    collector.flush()
    now[0] += collector.RETENTION + 120
    db.prune_usage_before(int(now[0] - collector.RETENTION))
    monkeypatch.setattr(db, "store_usage_batch", store)
    collector.flush()
    assert db.get_usage_totals(0, int(now[0])) == []


def test_accepted_publisher_is_not_blocked_by_database_flush(tmp_path, monkeypatch):
    import threading

    from repeater.data_acquisition.network_usage import NetworkUsageCollector

    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), counters=dict)
    entered, release, recorded = threading.Event(), threading.Event(), threading.Event()
    store = collector.database.store_usage_batch

    def slow_store(*args):
        entered.set()
        assert release.wait(5)
        store(*args)

    monkeypatch.setattr(collector.database, "store_usage_batch", slow_store)
    writer = threading.Thread(target=collector.flush)
    writer.start()
    try:
        assert entered.wait(5)
        publisher = threading.Thread(
            target=lambda: (collector.record_mqtt("mqtt:test", "Test", "a"), recorded.set())
        )
        publisher.start()
        assert recorded.wait(1), "Publisher blocked behind SQLite I/O"
        publisher.join(5)
    finally:
        release.set()
        writer.join(5)
    collector.flush()
    assert collector.database.get_usage_totals(0, 9999999999)[0]["messages"] == 1
