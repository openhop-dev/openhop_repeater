"""KISS RF front-end controls: ConfigManager status/apply/persist and /api/radio_frontend."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.config import agc_reset_interval_limits
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
        "radio_id": None,
        "available": True,
        "radio_type": None,
        "limits": {},
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
    assert status["radio_type"] is None
    assert status["limits"] == {}
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


def test_apply_persists_into_the_modem_section_without_copying_kiss():
    radio = FakeKissRadio()

    def keep(seconds):
        radio.agc = seconds
        return seconds

    radio.set_agc_reset_interval = keep
    config = {
        "radio_type": "modem_tcp",
        "modem_tcp": {"host": "modem.local"},
        "kiss": {"agc_reset_interval_seconds": 8, "fem_rx_gain": True},
        "repeater": {"agc_reset_interval": 60},
    }
    mgr, config = _manager(radio, config)

    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 30, "fem_tx_gain": True})

    assert result["applied"] == {"agc_reset_interval_seconds": 30, "fem_tx_gain": True}
    assert config["modem_tcp"]["agc_reset_interval_seconds"] == 30
    assert config["modem_tcp"]["fem_tx_gain"] is True
    assert config["kiss"] == {"agc_reset_interval_seconds": 8, "fem_rx_gain": True}
    assert "agc_reset_interval" not in config["repeater"]


def test_apply_offers_an_interval_above_1020():
    radio = FakeKissRadio()
    seen = {}

    def keep(seconds):
        seen["agc"] = seconds
        radio.agc = seconds
        return seconds

    radio.set_agc_reset_interval = keep
    mgr, config = _manager(radio)

    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 2000})

    assert seen["agc"] == 2000
    assert result["applied"] == {"agc_reset_interval_seconds": 2000}
    assert config["kiss"]["agc_reset_interval_seconds"] == 2000


def test_apply_maps_a_radio_range_error_to_not_applied():
    radio = FakeKissRadio()

    def reject(_seconds):
        raise ValueError("0-1020")

    radio.set_agc_reset_interval = reject
    mgr, config = _manager(radio)

    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 2000})

    assert result == {
        "applied": {},
        "errors": {"agc_reset_interval_seconds": "radio did not apply setting"},
    }
    assert "agc_reset_interval_seconds" not in config["kiss"]
    mgr.save_to_file.assert_not_called()


@pytest.mark.parametrize("maximum, interval", [(1020, 1200), (3600, 3601), (777, 778)])
def test_apply_reports_the_radio_range_without_sending_or_saving(maximum, interval):
    radio = FakeKissRadio()
    radio.effective_agc_reset_interval = lambda seconds: (
        seconds if 0 <= seconds <= maximum else None
    )
    radio.set_agc_reset_interval = MagicMock()
    mgr, config = _manager(radio)

    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": interval})

    assert result == {
        "applied": {},
        "errors": {
            "agc_reset_interval_seconds": (
                f"AGC reset interval {interval} is outside this radio's range (0-{maximum} seconds)"
            )
        },
    }
    radio.set_agc_reset_interval.assert_not_called()
    mgr.save_to_file.assert_not_called()
    assert "agc_reset_interval_seconds" not in config["kiss"]


def test_status_configured_comes_from_the_modem_section():
    radio = FakeKissRadio()
    mgr, _ = _manager(
        radio,
        {
            "radio_type": "modem_usb",
            "modem_usb": {"port": "/dev/ttyACM0", "agc_reset_interval_seconds": 30},
            "kiss": {"agc_reset_interval_seconds": 8},
        },
    )
    assert mgr.kiss_frontend_status()["configured"] == {"agc_reset_interval_seconds": 30}


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


@pytest.fixture(
    params=[
        ("kiss_modem_wrapper", "KissModemWrapper", "kiss", 1020, 4),
        ("tcp_radio", "TCPLoRaRadio", "modem_tcp", 3600, 1),
        ("usb_radio", "USBLoRaRadio", "modem_usb", 3600, 1),
    ]
)
def agc_radio(request):
    module_name, class_name, radio_type, maximum, step = request.param
    module = pytest.importorskip(f"openhop_core.hardware.{module_name}")
    radio = FakeKissRadio()
    radio.effective_agc_reset_interval = getattr(module, class_name).effective_agc_reset_interval
    return radio, radio_type, {"min": 0, "max": maximum, "step": step}


def test_endpoint_get(request_ctx, agc_radio):
    radio, radio_type, limits = agc_radio
    api = _api(radio, {"radio_type": radio_type})
    result = api.radio_frontend()
    assert result == {
        "success": True,
        "data": {
            "radio_id": None,
            "available": True,
            "radio_type": radio_type,
            "limits": {"agc_reset_interval_seconds": limits},
            "supports": {
                "agc_reset_interval_seconds": True,
                "fem_rx_gain": True,
                "fem_tx_gain": True,
                "rx_boosted_gain": True,
            },
            "running": {
                "agc_reset_interval_seconds": 32,
                "fem_rx_gain": False,
                "fem_tx_gain": False,
                "rx_boosted_gain": True,
            },
            "configured": {},
        },
    }


@pytest.mark.parametrize("case", ["missing", "not_callable", "unsupported", "range", "step"])
def test_endpoint_get_omits_unknown_limits(request_ctx, case):
    radio = FakeKissRadio()
    if case == "not_callable":
        radio.effective_agc_reset_interval = None
    elif case != "missing":

        def probe(seconds):
            if case != "step" or seconds == 1:
                raise RuntimeError("probe failed")
            return seconds

        radio.effective_agc_reset_interval = MagicMock(side_effect=probe)
    if case == "unsupported":
        radio.caps.remove("agc")

    result = _api(radio).radio_frontend()

    assert result["success"] is True
    assert result["data"]["limits"] == {}
    if case == "unsupported":
        radio.effective_agc_reset_interval.assert_not_called()


@pytest.mark.parametrize(
    "maximum, step, expected",
    [
        (1020, 4, {"min": 0, "max": 1020, "step": 4}),
        (3600, 1, {"min": 0, "max": 3600, "step": 1}),
        (777, 7, {"min": 0, "max": 777, "step": 7}),
        (5000, 1, {"min": 0, "max": 3600, "step": 1}),
        (0, 4, {"min": 0, "max": 0, "step": 1}),
        (3, 4, {"min": 0, "max": 3, "step": 1}),
    ],
)
def test_agc_limits_probe_range_and_smallest_step(maximum, step, expected):
    radio = SimpleNamespace(
        effective_agc_reset_interval=lambda s: s - s % step if 0 <= s <= maximum else None
    )
    assert agc_reset_interval_limits(radio) == expected


def test_status_refreshes_limits_after_radio_or_method_changes():
    radio = FakeKissRadio()
    radio.effective_agc_reset_interval = lambda s: s - s % 4 if 0 <= s <= 1020 else None
    mgr, _ = _manager(radio)
    assert mgr.kiss_frontend_status()["limits"] == {
        "agc_reset_interval_seconds": {"min": 0, "max": 1020, "step": 4}
    }

    radio.effective_agc_reset_interval = lambda s: s if 0 <= s <= 3600 else None
    assert mgr.kiss_frontend_status()["limits"] == {
        "agc_reset_interval_seconds": {"min": 0, "max": 3600, "step": 1}
    }

    mgr.daemon.radio = FakeKissRadio()
    assert mgr.kiss_frontend_status()["limits"] == {}


def test_endpoint_post_applies_without_restart(request_ctx, agc_radio):
    radio, radio_type, limits = agc_radio
    api = _api(radio, {"radio_type": radio_type})
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 4, "fem_tx_gain": True}

    result = api.radio_frontend()

    assert result["success"] is True
    assert result["restart_required"] is False
    assert result["data"]["applied"] == {"agc_reset_interval_seconds": 4, "fem_tx_gain": True}
    assert result["data"]["running"]["fem_tx_gain"] is True
    assert result["data"]["radio_type"] == radio_type
    assert result["data"]["limits"] == {"agc_reset_interval_seconds": limits}
    assert radio.agc == 4


def test_endpoint_post_offers_intervals_above_1020(request_ctx):
    radio = FakeKissRadio()

    def keep(seconds):
        radio.agc = seconds
        return seconds

    radio.set_agc_reset_interval = keep
    api = _api(radio, {"radio_type": "modem_tcp", "modem_tcp": {"host": "modem.local"}})
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 2000}

    result = api.radio_frontend()

    assert result["success"] is True
    assert radio.agc == 2000
    assert api.config["modem_tcp"]["agc_reset_interval_seconds"] == 2000
    assert "kiss" not in api.config


def test_endpoint_post_partial_failure_still_returns_status(request_ctx, agc_radio):
    radio, radio_type, limits = agc_radio
    radio.caps = {"agc"}
    api = _api(radio, {"radio_type": radio_type})
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 4, "fem_rx_gain": True}

    result = api.radio_frontend()

    assert result["success"] is False
    assert "fem_rx_gain: unsupported" in result["error"]
    assert result["data"]["applied"] == {"agc_reset_interval_seconds": 4}
    assert result["data"]["supports"]["fem_rx_gain"] is False
    assert result["data"]["radio_type"] == radio_type
    assert result["data"]["limits"] == {"agc_reset_interval_seconds": limits}


def test_endpoint_post_reports_kiss_range_and_still_applies_other_keys(request_ctx):
    from openhop_core.hardware.kiss_modem_wrapper import KissModemWrapper

    radio = FakeKissRadio()
    radio.effective_agc_reset_interval = KissModemWrapper.effective_agc_reset_interval
    radio.set_agc_reset_interval = MagicMock()
    api = _api(radio)
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 1200, "fem_rx_gain": True}

    result = api.radio_frontend()

    error = "AGC reset interval 1200 is outside this radio's range (0-1020 seconds)"
    assert result["success"] is False
    assert error in result["error"]
    assert result["data"]["errors"] == {"agc_reset_interval_seconds": error}
    assert result["data"]["applied"] == {"fem_rx_gain": True}
    radio.set_agc_reset_interval.assert_not_called()
    assert "agc_reset_interval_seconds" not in api.config["kiss"]
    assert api.config["kiss"]["fem_rx_gain"] is True
    api.config_manager.save_to_file.assert_called_once()


@pytest.mark.parametrize(
    "body, message",
    [
        ({"agc_reset_interval_seconds": 3601}, "0-3600"),
        ({"agc_reset_interval_seconds": -1}, "0-3600"),
        ({"agc_reset_interval_seconds": "4"}, "0-3600"),
        ({"agc_reset_interval_seconds": True}, "0-3600"),
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


# ─── Per-radio controls on a multi-radio node ───────────────────────────


class _Fabric:
    def __init__(self, radios, default_radio_id):
        self.radios = radios
        self.default_radio_id = default_radio_id


def _fabric_daemon(radios, default_radio_id):
    """A daemon whose radio is a FabricRadio behind an adapter, as the repeater builds it."""
    fabric_radio = SimpleNamespace(fabric=_Fabric(radios, default_radio_id))
    return SimpleNamespace(radio=SimpleNamespace(_radio=fabric_radio))


def _two_radio_config():
    return {
        "radio_type": "kiss",
        "kiss": {"port": "/dev/ttyACM0", "fem_rx_gain": True},
        "modem_tcp": {"host": "shared", "port": 5055},
        "repeater": {"agc_reset_interval": 8},
        "radios": [
            {"id": "narrow", "radio_type": "kiss"},
            {"id": "g2", "radio_type": "modem_tcp", "modem_tcp": {"host": "g2", "port": 5055}},
        ],
        "fabric": {"default_radio": "narrow"},
    }


def _two_radio_manager(save_ok=True):
    narrow, g2 = FakeKissRadio(), FakeKissRadio(caps=("agc", "rx", "tx"))
    config = _two_radio_config()
    daemon = _fabric_daemon({"narrow": narrow, "g2": g2}, "narrow")
    mgr = ConfigManager("/tmp/cfg.yaml", config, daemon)
    mgr.save_to_file = MagicMock(return_value=save_ok)
    return mgr, config, narrow, g2


def test_status_of_a_non_default_radio_reads_that_radio():
    mgr, _, narrow, g2 = _two_radio_manager()
    g2.agc, g2.fem["tx_gain"] = 60, True

    status = mgr.kiss_frontend_status("g2")

    assert status["radio_id"] == "g2"
    assert status["radio_type"] == "modem_tcp"
    assert status["supports"]["rx_boosted_gain"] is False
    assert status["running"] == {
        "agc_reset_interval_seconds": 60,
        "fem_rx_gain": False,
        "fem_tx_gain": True,
    }
    # g2's own modem_tcp section has no front-end keys; kiss values are not
    # copied onto a modem, and the legacy repeater key still applies.
    assert status["configured"] == {"agc_reset_interval_seconds": 8}


def test_status_without_a_radio_id_is_the_default_radio():
    mgr, _, narrow, _ = _two_radio_manager()
    status = mgr.kiss_frontend_status()
    assert status["radio_id"] == "narrow"
    assert status["radio_type"] == "kiss"
    assert status["running"]["agc_reset_interval_seconds"] == narrow.agc


def test_apply_to_a_non_default_radio_sends_and_saves_only_there():
    mgr, config, narrow, g2 = _two_radio_manager()
    before_narrow = (narrow.agc, dict(narrow.fem))

    result = mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 20, "fem_tx_gain": True}, "g2")

    assert result == {
        "applied": {"agc_reset_interval_seconds": 20, "fem_tx_gain": True},
        "errors": {},
    }
    assert (g2.agc, g2.fem["tx_gain"]) == (20, True)
    assert (narrow.agc, narrow.fem) == before_narrow
    assert config["radios"][1]["modem_tcp"] == {
        "host": "g2",
        "port": 5055,
        "agc_reset_interval_seconds": 20,
        "fem_tx_gain": True,
    }
    assert "modem_tcp" not in config["radios"][0] and "kiss" not in config["radios"][0]
    assert config["modem_tcp"] == {"host": "shared", "port": 5055}
    # The legacy key belongs to the default radio; another radio's save keeps it.
    assert config["repeater"] == {"agc_reset_interval": 8}
    mgr.save_to_file.assert_called_once()


def test_apply_to_a_radio_that_inherits_its_section_copies_it_first():
    mgr, config, _, g2 = _two_radio_manager()
    del config["radios"][1]["modem_tcp"]

    mgr.apply_kiss_frontend({"fem_rx_gain": True}, "g2")

    assert config["radios"][1]["modem_tcp"] == {
        "host": "shared",
        "port": 5055,
        "fem_rx_gain": True,
    }
    assert config["modem_tcp"] == {"host": "shared", "port": 5055}


def test_apply_save_failure_restores_the_radio_entry():
    mgr, config, _, _ = _two_radio_manager(save_ok=False)
    before = copy.deepcopy(config)

    result = mgr.apply_kiss_frontend({"fem_rx_gain": True}, "g2")

    assert result["errors"] == {"save": "applied to radio but failed to save config"}
    assert config == before


def test_unknown_radio_id_is_refused_before_any_radio_is_touched():
    mgr, config, narrow, g2 = _two_radio_manager()
    before = copy.deepcopy(config)
    with pytest.raises(KeyError):
        mgr.kiss_frontend_status("nope")
    with pytest.raises(KeyError):
        mgr.apply_kiss_frontend({"fem_rx_gain": True}, "nope")
    assert config == before
    assert narrow.fem["rx_gain"] is False and g2.fem["rx_gain"] is False


def test_live_update_applies_each_radios_own_settings():
    mgr, config, narrow, g2 = _two_radio_manager()
    config["radios"][1]["modem_tcp"]["fem_tx_gain"] = True

    assert mgr._apply_live_kiss_hardware_config()

    assert g2.fem["tx_gain"] is True
    assert narrow.fem["tx_gain"] is False
    assert narrow.fem["rx_gain"] is True  # narrow's own kiss section


def _two_radio_api():
    mgr, config, narrow, g2 = _two_radio_manager()
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api.config_manager = mgr
    return api, config, narrow, g2


@pytest.fixture(params=["missing", "empty_fabric", "deferred", "stopped"])
def unavailable_radio_api(request):
    api, config, narrow, g2 = _two_radio_api()
    mgr = api.config_manager
    if request.param == "missing":
        mgr.daemon = _fabric_daemon({"narrow": narrow}, "narrow")
    elif request.param == "empty_fabric":
        mgr.daemon = _fabric_daemon({}, "narrow")
    elif request.param == "deferred":
        mgr.daemon = SimpleNamespace(radio=None)
    else:
        mgr.daemon = None
    return api, config, narrow, g2


_G2_UNAVAILABLE = "radio 'g2' is configured but not running (restart to apply radios[] changes)"


def test_endpoint_get_explains_a_configured_radio_that_is_not_running(
    request_ctx, unavailable_radio_api
):
    api, config, narrow, g2 = unavailable_radio_api
    before = copy.deepcopy(config)
    result = api.radio_frontend(radio_id="g2")

    assert result["success"] is True
    status = result["data"]
    assert status["radio_id"] == "g2"
    assert status["available"] is False
    assert status["unavailable_reason"] == _G2_UNAVAILABLE
    assert status["configured"] == {"agc_reset_interval_seconds": 8}
    assert status["running"] == {}
    assert not any(status["supports"].values())
    assert config == before
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize("id_in_body", [True, False])
def test_endpoint_post_to_a_configured_radio_that_is_not_running_is_409(
    request_ctx, unavailable_radio_api, id_in_body
):
    api, config, narrow, g2 = unavailable_radio_api
    before = copy.deepcopy(config)
    radios_before = copy.deepcopy((vars(narrow), vars(g2)))
    api.config_manager.apply_kiss_frontend = MagicMock()
    request_ctx.method = "POST"
    request_ctx.json = {"agc_reset_interval_seconds": 20, "fem_tx_gain": True}
    if id_in_body:
        request_ctx.json["radio_id"] = "g2"

    with pytest.raises(cherrypy.HTTPError) as exc:
        api.radio_frontend(radio_id=None if id_in_body else "g2")

    assert exc.value.status == 409
    assert exc.value._message == _G2_UNAVAILABLE
    assert config == before
    assert (vars(narrow), vars(g2)) == radios_before
    api.config_manager.apply_kiss_frontend.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


def test_running_radio_without_frontend_controls_keeps_unsupported_behaviour(request_ctx):
    api, config, narrow, g2 = _two_radio_api()
    api.config_manager.daemon = _fabric_daemon({"narrow": narrow, "g2": object()}, "narrow")
    before = copy.deepcopy(config)

    status = api.radio_frontend(radio_id="g2")["data"]
    assert status["available"] is False
    assert "unavailable_reason" not in status

    request_ctx.method = "POST"
    request_ctx.json = {"radio_id": "g2", "fem_tx_gain": True}
    result = api.radio_frontend()
    assert result["success"] is False
    assert result["data"]["errors"] == {"fem_tx_gain": "unsupported"}
    assert config == before
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize("multi_radio", [False, True])
def test_missing_default_radio_without_an_id_keeps_unsupported_behaviour(request_ctx, multi_radio):
    api = _two_radio_api()[0] if multi_radio else _api(None)
    api.config_manager.daemon = None
    before = copy.deepcopy(api.config)

    status = api.radio_frontend()["data"]
    assert status["available"] is False
    assert "unavailable_reason" not in status

    request_ctx.method = "POST"
    request_ctx.json = {"fem_tx_gain": True}
    result = api.radio_frontend()
    assert result["success"] is False
    assert result["data"]["errors"] == {"fem_tx_gain": "unsupported"}
    assert "unavailable_reason" not in result["data"]
    assert api.config == before
    api.config_manager.save_to_file.assert_not_called()


def test_endpoint_get_takes_a_radio_id(request_ctx):
    api, _, _, g2 = _two_radio_api()
    g2.agc = 60
    result = api.radio_frontend(radio_id="g2")
    assert result["success"] is True
    assert result["data"]["radio_id"] == "g2"
    assert result["data"]["running"]["agc_reset_interval_seconds"] == 60


def test_endpoint_post_takes_a_radio_id_in_the_body(request_ctx):
    api, config, narrow, g2 = _two_radio_api()
    request_ctx.method = "POST"
    request_ctx.json = {"radio_id": "g2", "fem_tx_gain": True}

    result = api.radio_frontend()

    assert result["success"] is True
    assert result["data"]["radio_id"] == "g2"
    assert g2.fem["tx_gain"] is True and narrow.fem["tx_gain"] is False
    assert config["radios"][1]["modem_tcp"]["fem_tx_gain"] is True


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_endpoint_unknown_radio_id_is_404(request_ctx, method):
    api, _, _, _ = _two_radio_api()
    request_ctx.method = method
    request_ctx.json = {"radio_id": "nope", "fem_tx_gain": True}
    with pytest.raises(cherrypy.HTTPError) as exc:
        api.radio_frontend(radio_id="nope" if method == "GET" else None)
    assert exc.value.status == 404


def test_endpoint_radio_id_on_a_single_radio_node_is_404(request_ctx):
    api = _api(FakeKissRadio())
    with pytest.raises(cherrypy.HTTPError) as exc:
        api.radio_frontend(radio_id="narrow")
    assert exc.value.status == 404


def test_default_radio_is_the_running_fabrics_when_the_config_names_another():
    """A default_radio edited without a restart must not split the read from the save."""
    mgr, config, narrow, g2 = _two_radio_manager()
    config["fabric"]["default_radio"] = "g2"  # running fabric still defaults to narrow
    narrow.agc, g2.agc = 8, 60

    status = mgr.kiss_frontend_status()
    assert status["radio_id"] == "narrow"
    assert status["running"]["agc_reset_interval_seconds"] == 8

    mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 20})
    assert (narrow.agc, g2.agc) == (20, 60)
    assert config["radios"][0]["kiss"]["agc_reset_interval_seconds"] == 20
    assert "agc_reset_interval_seconds" not in config["radios"][1]["modem_tcp"]


def test_mesh_cli_save_follows_the_running_default_radio():
    mgr, config, _, _ = _two_radio_manager()
    config["fabric"]["default_radio"] = "g2"
    assert mgr.persist_default_kiss_settings({"fem_rx_gain": False})
    assert config["radios"][0]["kiss"]["fem_rx_gain"] is False
    assert "fem_rx_gain" not in config["radios"][1]["modem_tcp"]


def test_saving_the_default_radios_agc_keeps_the_legacy_key_other_radios_inherit():
    mgr, config, _, _ = _two_radio_manager()

    mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 20})

    assert config["repeater"] == {"agc_reset_interval": 8}
    assert mgr.kiss_frontend_status("g2")["configured"] == {"agc_reset_interval_seconds": 8}
    assert mgr.kiss_frontend_status()["configured"]["agc_reset_interval_seconds"] == 20


def test_single_radio_agc_save_still_retires_the_legacy_key():
    radio = FakeKissRadio()
    mgr, config = _manager(
        radio, {"kiss": {"port": "/dev/x"}, "repeater": {"agc_reset_interval": 8}}
    )
    mgr.apply_kiss_frontend({"agc_reset_interval_seconds": 20})
    assert "agc_reset_interval" not in config["repeater"]


def test_live_update_is_not_failed_by_another_radio_that_cannot_take_an_inherited_setting():
    mgr, config, narrow, g2 = _two_radio_manager()
    g2.caps = {"rx", "tx"}  # no AGC control, yet it inherits repeater.agc_reset_interval

    assert mgr._apply_live_kiss_hardware_config() is True
    assert narrow.agc == 8  # the default radio still took the inherited interval
