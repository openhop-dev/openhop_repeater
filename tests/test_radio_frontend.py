"""KISS RF front-end controls: ConfigManager status/apply/persist and /api/radio_frontend."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.config_manager import ConfigManager
from repeater.web.api_endpoints import APIEndpoints


class FakeKissRadio:
    """The KissModemWrapper AGC/FEM surface."""

    def __init__(self, caps=("agc", "rx", "tx", "boost"), responsive=True, fem_sticks=True):
        self.caps = set(caps)
        self.responsive = responsive
        self.fem_sticks = fem_sticks
        self.boosted = True
        self.agc = 32
        self.fem = {"rx_gain": False, "tx_gain": False}
        self.applied_hardware_config = {}

    def supports_agc_reset_control(self):
        return "agc" in self.caps

    def supports_fem_rx_gain(self):
        return "rx" in self.caps

    def supports_fem_tx_gain(self):
        return "tx" in self.caps

    def supports_rx_boosted_gain(self):
        return "boost" in self.caps

    def get_rx_boosted_gain(self):
        return self.boosted if self.responsive else None

    def set_rx_boosted_gain(self, enabled):
        if not self.responsive:
            return None
        if self.fem_sticks:
            self.boosted = enabled
        return self.boosted

    def get_agc_reset_interval(self):
        return self.agc if self.responsive else None

    def set_agc_reset_interval(self, seconds):
        if not self.responsive:
            return None
        self.agc = seconds - seconds % 4
        return self.agc

    def get_fem_state(self):
        if not self.responsive:
            return None
        return {
            "rx_gain": self.fem["rx_gain"] if "rx" in self.caps else None,
            "tx_gain": self.fem["tx_gain"] if "tx" in self.caps else None,
        }

    def set_fem_state(self, rx_gain=None, tx_gain=None):
        if not self.responsive:
            return None
        for key, value in (("rx_gain", rx_gain), ("tx_gain", tx_gain)):
            if value is not None and self.fem_sticks:
                self.fem[key] = value
        return self.get_fem_state()


def _manager(radio, config=None, save_ok=True):
    config = config if config is not None else {"kiss": {"port": "/dev/ttyACM0"}}
    mgr = ConfigManager("/tmp/cfg.yaml", config, SimpleNamespace(radio=radio))
    mgr.save_to_file = MagicMock(return_value=save_ok)
    return mgr, config


# ─── ConfigManager ──────────────────────────────────────────────────────


def test_status_reads_modem_and_config():
    radio = FakeKissRadio(caps=("agc", "rx", "boost"))
    radio.agc, radio.fem["rx_gain"] = 8, True
    mgr, _ = _manager(radio, {"kiss": {"port": "/dev/x", "fem_rx_gain": True}})

    assert mgr.kiss_frontend_status() == {
        "available": True,
        "supports": {
            "agc_reset_interval_seconds": True,
            "fem_rx_gain": True,
            "fem_tx_gain": False,
            "rx_boosted_gain": True,
        },
        "running": {
            "agc_reset_interval_seconds": 8,
            "fem_rx_gain": True,
            "rx_boosted_gain": True,
        },
        "configured": {"fem_rx_gain": True},
    }


def test_status_on_non_kiss_radio():
    mgr, _ = _manager(object(), {"repeater": {"agc_reset_interval": 8}})
    status = mgr.kiss_frontend_status()
    assert status["available"] is False
    assert not any(status["supports"].values())
    assert status["running"] == {}
    assert status["configured"] == {"agc_reset_interval_seconds": 8}  # legacy key shown


def test_apply_persists_only_confirmed_values():
    radio = FakeKissRadio(caps=("agc", "rx"))
    mgr, config = _manager(
        radio, {"kiss": {"port": "/dev/x"}, "repeater": {"agc_reset_interval": 60}}
    )

    result = mgr.apply_kiss_frontend(
        {"agc_reset_interval_seconds": 10, "fem_rx_gain": True, "fem_tx_gain": True}
    )

    assert result == {
        "applied": {"agc_reset_interval_seconds": 8, "fem_rx_gain": True},
        "errors": {"fem_tx_gain": "unsupported"},
    }
    assert config["kiss"] == {
        "port": "/dev/x",
        "agc_reset_interval_seconds": 8,
        "fem_rx_gain": True,
    }
    assert "agc_reset_interval" not in config["repeater"]
    mgr.save_to_file.assert_called_once()


def test_apply_reports_fem_state_the_modem_did_not_take():
    mgr, config = _manager(FakeKissRadio(fem_sticks=False))
    result = mgr.apply_kiss_frontend({"fem_tx_gain": True})
    assert result == {"applied": {}, "errors": {"fem_tx_gain": "radio did not apply setting"}}
    assert "fem_tx_gain" not in config["kiss"]
    mgr.save_to_file.assert_not_called()


def test_apply_save_failure_rolls_back():
    mgr, config = _manager(FakeKissRadio(), save_ok=False)
    result = mgr.apply_kiss_frontend({"fem_rx_gain": True})
    assert result["errors"] == {"save": "applied to radio but failed to save config"}
    assert config["kiss"] == {"port": "/dev/ttyACM0"}


def test_apply_on_non_kiss_radio_is_unsupported():
    mgr, config = _manager(None)
    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 4})
    assert result == {"applied": {}, "errors": {"agc_reset_interval_seconds": "unsupported"}}


# ─── /api/radio_frontend ────────────────────────────────────────────────


@pytest.fixture
def request_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request


def _api(radio, config=None, save_ok=True):
    mgr, config = _manager(radio, config, save_ok)
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api.config_manager = mgr
    return api


def test_endpoint_get(request_ctx):
    api = _api(FakeKissRadio())
    result = api.radio_frontend()
    assert result["success"] is True
    assert result["data"]["running"]["agc_reset_interval_seconds"] == 32


def test_endpoint_post_applies_without_restart(request_ctx):
    radio = FakeKissRadio()
    api = _api(radio)
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 4, "fem_tx_gain": True}

    result = api.radio_frontend()

    assert result["success"] is True
    assert result["restart_required"] is False
    assert result["data"]["applied"] == {"agc_reset_interval_seconds": 4, "fem_tx_gain": True}
    assert result["data"]["running"]["fem_tx_gain"] is True
    assert radio.agc == 4


def test_endpoint_post_partial_failure_still_returns_status(request_ctx):
    api = _api(FakeKissRadio(caps=("agc",)))
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 4, "fem_rx_gain": True}

    result = api.radio_frontend()

    assert result["success"] is False
    assert "fem_rx_gain: unsupported" in result["error"]
    assert result["data"]["applied"] == {"agc_reset_interval_seconds": 4}
    assert result["data"]["supports"]["fem_rx_gain"] is False


@pytest.mark.parametrize(
    "body, message",
    [
        ({"agc_reset_interval_seconds": 1021}, "0-1020"),
        ({"agc_reset_interval_seconds": -1}, "0-1020"),
        ({"agc_reset_interval_seconds": "4"}, "0-1020"),
        ({"agc_reset_interval_seconds": True}, "0-1020"),
        ({"fem_rx_gain": "on"}, "true or false"),
        ({"rx_boosted_gain": 1}, "true or false"),
        ({}, "No valid settings"),
        ({"port": "/dev/evil"}, "No valid settings"),
    ],
)
def test_endpoint_post_validation(request_ctx, body, message):
    radio = FakeKissRadio()
    api = _api(radio)
    request_ctx.method = "POST"
    request_ctx.json = body

    result = api.radio_frontend()

    assert result["success"] is False
    assert message in result["error"]
    assert radio.agc == 32
    api.config_manager.save_to_file.assert_not_called()


def test_apply_rx_boosted_gain():
    radio = FakeKissRadio()
    mgr, config = _manager(radio)
    assert mgr.apply_kiss_frontend({"rx_boosted_gain": False}) == {
        "applied": {"rx_boosted_gain": False},
        "errors": {},
    }
    assert radio.boosted is False
    assert config["kiss"]["rx_boosted_gain"] is False


def test_apply_rx_boosted_gain_refused_is_reported():
    mgr, config = _manager(FakeKissRadio(fem_sticks=False))  # radio keeps its state
    result = mgr.apply_kiss_frontend({"rx_boosted_gain": False})
    assert result["errors"] == {"rx_boosted_gain": "radio did not apply setting"}
    assert "rx_boosted_gain" not in config["kiss"]


def test_status_with_core_that_predates_boosted_gain():
    """An older openhop-core has no rx-boosted methods; report it as unsupported."""

    class OldCoreRadio(FakeKissRadio):
        supports_rx_boosted_gain = None

    mgr, _ = _manager(OldCoreRadio())
    status = mgr.kiss_frontend_status()
    assert status["supports"]["rx_boosted_gain"] is False
    assert "rx_boosted_gain" not in status["running"]
    assert mgr.apply_kiss_frontend({"rx_boosted_gain": True})["errors"] == {
        "rx_boosted_gain": "unsupported"
    }
