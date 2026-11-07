"""Tests for the spectrum sweep engine, driven the way the endpoints drive it."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from repeater.config import NullRadio
from repeater.web.spectrum_sweep_engine import SpectrumSweepEngine

MHZ = 1_000_000


class _FakeRadio:
    def __init__(self, fail_at=None):
        self.calls: list[int] = []
        self.kwargs: list[dict] = []
        self._fail_at = fail_at

    async def measure_rssi_dwell(self, freq_hz, dwell_s=0.4, **kwargs):
        self.calls.append(int(freq_hz))
        self.kwargs.append(kwargs)
        await asyncio.sleep(dwell_s)
        if self._fail_at == int(freq_hz):
            return {"error": "waited_for_tx_lock_timeout", "freq_hz": int(freq_hz)}
        return {
            "freq_hz": int(freq_hz),
            "dwell_s": dwell_s,
            "started_ts": time.time(),
            "n": 3,
            "samples": [-100.0, -98.0, -101.0],
            "discarded": 0,
        }


class _OldCoreRadio(_FakeRadio):
    """A core from before the CAD arguments: passing them is a TypeError."""

    async def measure_rssi_dwell(self, freq_hz, dwell_s=0.4):
        return await super().measure_rssi_dwell(freq_hz, dwell_s=dwell_s)


class _FakeFabric:
    def __init__(self, radios, default_id):
        self._radios = dict(radios)
        self.default_radio_id = default_id

    def get_radio(self, radio_id):
        return self._radios[radio_id]


def _daemon(radio, radio_status="ok"):
    return SimpleNamespace(radio=radio, radio_status=radio_status)


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def _plan_1mhz(**extra):
    return {
        "start_hz": 902 * MHZ,
        "stop_hz": 903 * MHZ,
        "step_hz": 200_000,
        "dwell_s": 0.05,
        **extra,
    }


def _finish(engine):
    engine.sweep_thread.join(timeout=5)
    assert not engine.sweep_thread.is_alive()


def test_build_plan_is_start_inclusive_stop_exclusive():
    assert SpectrumSweepEngine.build_plan(902 * MHZ, 903 * MHZ, 200_000) == [
        902_000_000,
        902_200_000,
        902_400_000,
        902_600_000,
        902_800_000,
    ]
    with pytest.raises(ValueError):
        SpectrumSweepEngine.build_plan(903 * MHZ, 902 * MHZ, 200_000)


def test_describe_single_radio_and_unsupported_radios():
    assert SpectrumSweepEngine(_daemon(_FakeRadio())).describe() == {
        "supported": True,
        "reason": None,
        "radio_id": "radio0",
        "mesh_suspended": True,
    }
    assert SpectrumSweepEngine(_daemon(NullRadio())).describe()["supported"] is False
    assert SpectrumSweepEngine(_daemon(object())).describe()["supported"] is False
    assert (
        SpectrumSweepEngine(_daemon(_FakeRadio(), "degraded")).describe()["reason"]
        == "radio is degraded"
    )


def test_describe_fabric_spare_radio_keeps_the_mesh_up():
    root = SimpleNamespace(
        fabric=_FakeFabric({"radio0": _FakeRadio(), "radio1": _FakeRadio()}, "radio0")
    )
    engine = SpectrumSweepEngine(_daemon(root))
    assert engine.describe()["mesh_suspended"] is True
    assert engine.describe("radio1") == {
        "supported": True,
        "reason": None,
        "radio_id": "radio1",
        "mesh_suspended": False,
    }
    assert engine.describe("radio9")["supported"] is False


def test_start_refuses_the_mesh_radio_without_confirmation(loop_in_thread):
    engine = SpectrumSweepEngine(_daemon(_FakeRadio()), loop_in_thread)
    started, reason = engine.start_sweep(_plan_1mhz())
    assert started is False and "confirm_mesh_suspend" in reason


def test_worker_sweeps_every_channel_and_streams_results(loop_in_thread):
    radio = _FakeRadio()
    engine = SpectrumSweepEngine(_daemon(radio), loop_in_thread)
    started, status = engine.start_sweep(_plan_1mhz(passes=2, confirm_mesh_suspend=True))
    assert started is True and status["progress"]["total"] == 10
    _finish(engine)
    types = [m["type"] for m in engine.message_queue]
    assert types[0] == "status" and types[-1] == "completed"
    assert types.count("result") == 10 and engine.errors == 0
    assert radio.calls == engine.build_plan(902 * MHZ, 903 * MHZ, 200_000) * 2
    assert engine.running is False


def test_worker_records_a_refused_dwell_and_carries_on(loop_in_thread):
    engine = SpectrumSweepEngine(_daemon(_FakeRadio(fail_at=902_400_000)), loop_in_thread)
    engine.start_sweep(_plan_1mhz(confirm_mesh_suspend=True))
    _finish(engine)
    assert engine.errors == 1 and len(engine.results) == 4
    assert engine.message_queue[-1]["type"] == "completed"


def test_stop_ends_the_sweep_at_the_next_channel(loop_in_thread):
    engine = SpectrumSweepEngine(_daemon(_FakeRadio()), loop_in_thread)
    engine.start_sweep(
        {
            "start_hz": 902 * MHZ,
            "stop_hz": 928 * MHZ,
            "step_hz": 200_000,
            "dwell_s": 0.05,
            "confirm_mesh_suspend": True,
        }
    )
    time.sleep(0.15)
    engine.stop_sweep()
    _finish(engine)
    assert engine.message_queue[-1]["message"] == "Sweep stopped"
    assert 0 < len(engine.results) < 130


def test_fabric_spare_radio_needs_no_confirmation(loop_in_thread):
    mesh, spare = _FakeRadio(), _FakeRadio()
    root = SimpleNamespace(fabric=_FakeFabric({"radio0": mesh, "radio1": spare}, "radio0"))
    engine = SpectrumSweepEngine(_daemon(root), loop_in_thread)
    started, status = engine.start_sweep(
        {
            "start_hz": 902 * MHZ,
            "stop_hz": 902_400_000,
            "step_hz": 200_000,
            "dwell_s": 0.05,
            "radio_id": "radio1",
        }
    )
    assert started is True and status["mesh_suspended"] is False
    _finish(engine)
    assert spare.calls == [902_000_000, 902_200_000] and mesh.calls == []


def test_cad_is_asked_for_per_step_only_when_requested(loop_in_thread):
    radio = _FakeRadio()
    engine = SpectrumSweepEngine(_daemon(radio), loop_in_thread)
    started, status = engine.start_sweep(
        _plan_1mhz(confirm_mesh_suspend=True, cad_symbols=8, cad_count=4)
    )
    assert started, status
    engine.sweep_thread.join(timeout=10)
    assert status["session"]["cad_symbols"] == 8 and status["session"]["cad_count"] == 4
    assert radio.kwargs and all(kw == {"cad_symbols": 8, "cad_count": 4} for kw in radio.kwargs)

    plain = _OldCoreRadio()
    engine = SpectrumSweepEngine(_daemon(plain), loop_in_thread)
    engine.start_sweep(_plan_1mhz(confirm_mesh_suspend=True))
    engine.sweep_thread.join(timeout=10)
    assert engine.errors == 0 and len(engine.results) == 5, (
        "an older core never sees the CAD arguments"
    )
    assert engine.session_config["cad_count"] == 0 and engine.session_config["cad_symbols"] is None


def test_cad_symbols_the_silicon_lacks_are_refused_before_the_sweep(loop_in_thread):
    engine = SpectrumSweepEngine(_daemon(_FakeRadio()), loop_in_thread)
    started, reason = engine.start_sweep(
        _plan_1mhz(confirm_mesh_suspend=True, cad_symbols=3, cad_count=4)
    )
    assert not started and "cad_symbols" in reason


def test_cad_without_symbols_leaves_the_count_to_the_radio(loop_in_thread):
    radio = _FakeRadio()
    engine = SpectrumSweepEngine(_daemon(radio), loop_in_thread)
    started, status = engine.start_sweep(_plan_1mhz(confirm_mesh_suspend=True, cad_count=4))
    assert started, status
    engine.sweep_thread.join(timeout=10)
    assert status["session"]["cad_symbols"] is None
    assert all(kw == {"cad_count": 4} for kw in radio.kwargs)


def test_cad_symbols_are_normalised_like_every_other_field(loop_in_thread):
    engine = SpectrumSweepEngine(_daemon(_FakeRadio()), loop_in_thread)
    started, status = engine.start_sweep(
        _plan_1mhz(confirm_mesh_suspend=True, cad_symbols="8", cad_count=1)
    )
    assert started, status
    assert status["session"]["cad_symbols"] == 8
    engine.sweep_thread.join(timeout=10)
