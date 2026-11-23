from pathlib import Path

from repeater.data_acquisition import rrdtool_handler
from repeater.data_acquisition.rrdtool_handler import RRDToolHandler


class _FakeRRDTool:
    """Stands in for the rrdtool module and keeps every update string."""

    def __init__(self, last_ds=None):
        self.last_ds = last_ds or {}
        self.updates = []

    def create(self, path, *args):
        Path(path).touch()

    def lastupdate(self, path):
        return {"date": None, "ds": dict(self.last_ds)}

    def update(self, path, values):
        self.updates.append(values)


def _make_handler(monkeypatch, tmp_path, last_ds=None):
    fake = _FakeRRDTool(last_ds)
    monkeypatch.setattr(rrdtool_handler, "rrdtool", fake, raising=False)
    monkeypatch.setattr(rrdtool_handler, "RRDTOOL_AVAILABLE", True)
    return RRDToolHandler(tmp_path), fake


def _counters(update: str) -> dict:
    # timestamp:rx:tx:drop:rssi:snr:length:score:neighbors:type_0..type_15:type_other
    fields = update.split(":")
    counters = {"rx": int(fields[1]), "tx": int(fields[2]), "drop": int(fields[3])}
    for i in range(16):
        counters[f"type_{i}"] = int(fields[9 + i])
    counters["type_other"] = int(fields[25])
    return counters


def test_packet_counters_count_every_record_by_outcome_and_type(monkeypatch, tmp_path):
    handler, fake = _make_handler(monkeypatch, tmp_path)

    handler.update_packet_metrics({"timestamp": 100, "type": 4, "transmitted": True})
    # Same second as the write above: rrdtool takes no second update for it,
    # but the packet still has to show in the next one.
    handler.update_packet_metrics({"timestamp": 100, "type": 5, "transmitted": False})
    handler.update_packet_metrics({"timestamp": 101, "type": 20, "transmitted": False})

    assert len(fake.updates) == 2
    counters = _counters(fake.updates[-1])
    assert (counters["rx"], counters["tx"], counters["drop"]) == (3, 1, 2)
    assert (counters["type_4"], counters["type_5"], counters["type_other"]) == (1, 1, 1)


def test_packet_counters_continue_from_the_last_values_in_the_file(monkeypatch, tmp_path):
    # rrdtool.lastupdate() answers floats, and None for a source never written.
    last_ds = {"rx_count": 5000.0, "tx_count": 1200.0, "drop_count": 3800.0, "type_4": 900.0}
    last_ds["type_5"] = None
    handler, fake = _make_handler(monkeypatch, tmp_path, last_ds)

    handler.update_packet_metrics({"timestamp": 100, "type": 4, "transmitted": True})

    counters = _counters(fake.updates[-1])
    assert (counters["rx"], counters["tx"], counters["drop"]) == (5001, 1201, 3800)
    assert (counters["type_4"], counters["type_5"]) == (901, 0)
