import math

from repeater.airtime import AirtimeBudgets, AirtimeManager
from repeater.config_manager import ConfigManager


class _DummyRepeaterHandler:
    def __init__(self, config=None):
        self.radio_config = {}
        self.airtime_mgr = AirtimeManager(
            config
            or {
                "radio": {
                    "frequency": 868000000,
                    "bandwidth": 125000,
                    "spreading_factor": 7,
                    "coding_rate": 5,
                    "tx_power": 14,
                    "preamble_length": 8,
                }
            }
        )


class _DummySX1262Radio:
    def __init__(self, apply_ok=True):
        self.frequency = 868000000
        self.bandwidth = 125000
        self.spreading_factor = 7
        self.coding_rate = 5
        self.tx_power = 14
        self.calls = []
        self.apply_ok = apply_ok

    def set_frequency(self, frequency):
        self.calls.append(("set_frequency", frequency))
        if not self.apply_ok:
            return False
        self.frequency = frequency
        return True

    def set_tx_power(self, power):
        self.calls.append(("set_tx_power", power))
        if not self.apply_ok:
            return False
        self.tx_power = power
        return True

    def set_spreading_factor(self, spreading_factor):
        self.calls.append(("set_spreading_factor", spreading_factor))
        if not self.apply_ok:
            return False
        self.spreading_factor = spreading_factor
        return True

    def set_bandwidth(self, bandwidth):
        self.calls.append(("set_bandwidth", bandwidth))
        if not self.apply_ok:
            return False
        self.bandwidth = bandwidth
        return True


class _DummyKissRadio:
    def __init__(self):
        self.radio_config = {
            "frequency": 869618000,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
            "tx_power": 20,
        }
        self.calls = []

    def configure_radio(self, **kwargs):
        self.calls.append(("configure_radio", kwargs))
        self.frequency = kwargs["frequency"]
        self.bandwidth = kwargs["bandwidth"]
        self.spreading_factor = kwargs["spreading_factor"]
        self.coding_rate = kwargs["coding_rate"]
        self.tx_power = self.radio_config["tx_power"]
        return True


class _DummyDaemon:
    def __init__(self, config, radio):
        self.config = {
            "radio": dict(config.get("radio", {})),
            "kiss": dict(config.get("kiss", {})),
        }
        self.radio = radio
        self.repeater_handler = _DummyRepeaterHandler(config)
        self.advert_helper = None
        self.dispatcher = None


def test_live_update_daemon_applies_sx1262_radio_config():
    config = {
        "radio": {
            "frequency": 915000000,
            "bandwidth": 250000,
            "spreading_factor": 10,
            "coding_rate": 6,
            "tx_power": 20,
        }
    }
    radio = _DummySX1262Radio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        ("set_frequency", 915000000),
        ("set_tx_power", 20),
        ("set_spreading_factor", 10),
        ("set_bandwidth", 250000),
    ]
    assert radio.coding_rate == 6
    assert daemon.repeater_handler.radio_config == config["radio"]


def test_live_update_daemon_applies_kiss_radio_config():
    config = {
        "radio": {
            "frequency": 915500000,
            "bandwidth": 125000,
            "spreading_factor": 9,
            "coding_rate": 7,
            "tx_power": 22,
        },
        "kiss": {
            "port": "/dev/ttyUSB0",
            "baud_rate": 115200,
        },
    }
    radio = _DummyKissRadio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        (
            "configure_radio",
            {
                "frequency": 915500000,
                "bandwidth": 125000,
                "spreading_factor": 9,
                "coding_rate": 7,
            },
        )
    ]
    assert radio.radio_config == config["radio"]
    assert daemon.repeater_handler.radio_config == config["radio"]


def test_live_update_daemon_refreshes_airtime_manager_modulation():
    startup_radio = {
        "frequency": 868000000,
        "bandwidth": 125000,
        "spreading_factor": 7,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    updated_radio = {
        "frequency": 915000000,
        "bandwidth": 125000,
        "spreading_factor": 12,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    config = {"radio": dict(startup_radio)}
    radio = _DummySX1262Radio()
    daemon = _DummyDaemon(config, radio)
    airtime_mgr = daemon.repeater_handler.airtime_mgr
    before = airtime_mgr.calculate_airtime(50)
    assert math.isclose(before, 97.536, rel_tol=1e-9)

    config["radio"] = dict(updated_radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)
    assert manager.live_update_daemon(["radio"])

    assert airtime_mgr.spreading_factor == 12
    assert airtime_mgr.bandwidth == 125000
    assert airtime_mgr.preamble_length == 8
    assert math.isclose(airtime_mgr.calculate_airtime(50), 2301.952, rel_tol=1e-9)


def test_failed_live_radio_apply_leaves_airtime_manager_unchanged():
    startup_radio = {
        "frequency": 868000000,
        "bandwidth": 125000,
        "spreading_factor": 7,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    config = {"radio": dict(startup_radio)}
    radio = _DummySX1262Radio(apply_ok=False)
    daemon = _DummyDaemon(config, radio)
    airtime_mgr = daemon.repeater_handler.airtime_mgr

    config["radio"] = {
        "frequency": 915000000,
        "bandwidth": 125000,
        "spreading_factor": 12,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    manager = ConfigManager("/tmp/config.yaml", config, daemon)
    assert manager.live_update_daemon(["radio"]) is False

    assert airtime_mgr.spreading_factor == 7
    assert math.isclose(airtime_mgr.calculate_airtime(50), 97.536, rel_tol=1e-9)


def test_live_duty_cycle_update_refreshes_the_cached_limit():
    config = {
        "radio": {
            "frequency": 868000000,
            "bandwidth": 125000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "preamble_length": 8,
        },
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
    }
    budgets = AirtimeBudgets(config)
    handler = _DummyRepeaterHandler(config)
    handler.airtime_budgets = budgets
    handler.airtime_mgr = budgets.default
    daemon = _DummyDaemon(config, _DummySX1262Radio())
    daemon.repeater_handler = handler
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    config["duty_cycle"]["max_airtime_per_minute"] = 600
    assert manager.live_update_daemon(["duty_cycle"])

    assert budgets.default.max_airtime_per_minute == 600


def test_non_default_radio_change_requires_restart_and_keeps_runtime_metering():
    config = {
        "radio": {
            "frequency": 910100000,
            "bandwidth": 500000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "preamble_length": 8,
        },
        "radios": [
            {
                "id": "local",
                "radio_type": "sx1262",
                "radio": {"frequency": 910100000, "bandwidth": 500000},
            },
            {
                "id": "link",
                "radio_type": "sx1262",
                "radio": {"frequency": 910525000, "bandwidth": 62500},
            },
        ],
        "fabric": {"use_fabric": True, "default_radio": "local"},
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
    }
    budgets = AirtimeBudgets(config)
    old_airtime = budgets.for_radio("link").calculate_airtime(50)
    handler = _DummyRepeaterHandler(config)
    handler.airtime_budgets = budgets
    handler.airtime_mgr = budgets.default
    daemon = _DummyDaemon(config, _DummySX1262Radio())
    daemon.repeater_handler = handler
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    config["radios"][1]["radio"]["bandwidth"] = 500000
    assert manager.live_update_daemon(["radios"]) is False

    assert budgets.for_radio("link").calculate_airtime(50) == old_airtime


class _DummyKissHwRadio:
    """KissModemWrapper AGC/FEM surface; records commands sent to the modem."""

    def __init__(self, caps=("agc", "rx", "tx"), applied=None, fem_sticks=True):
        self.caps = set(caps)
        self.applied_hardware_config = dict(applied or {})
        self.fem_sticks = fem_sticks  # False: modem reports a state other than requested
        self.calls = []

    def supports_agc_reset_control(self):
        return "agc" in self.caps

    def supports_fem_rx_gain(self):
        return "rx" in self.caps

    def supports_fem_tx_gain(self):
        return "tx" in self.caps

    def set_agc_reset_interval(self, seconds):
        self.calls.append(("agc", seconds))
        effective = seconds - seconds % 4
        self.applied_hardware_config["agc_reset_interval_seconds"] = effective
        return effective

    def supports_rx_boosted_gain(self):
        return "boost" in self.caps

    def set_rx_boosted_gain(self, enabled):
        self.calls.append(("boost", enabled))
        self.applied_hardware_config["rx_boosted_gain"] = enabled
        return enabled

    def set_fem_state(self, rx_gain=None, tx_gain=None):
        self.calls.append(("fem", rx_gain, tx_gain))
        state = {}
        for key, value in (("rx_gain", rx_gain), ("tx_gain", tx_gain)):
            if value is not None:
                state[key] = value if self.fem_sticks else not value
                self.applied_hardware_config[f"fem_{key}"] = state[key]
        return state


def _kiss_manager(config, radio):
    daemon = _DummyDaemon(config, radio)
    return ConfigManager("/tmp/config.yaml", config, daemon), daemon


def test_live_kiss_update_applies_changed_agc_and_fem():
    config = {
        "kiss": {"port": "/dev/ttyACM0", "agc_reset_interval_seconds": 4, "fem_rx_gain": True}
    }
    radio = _DummyKissHwRadio(applied={"agc_reset_interval_seconds": 30})
    manager, _ = _kiss_manager(config, radio)

    assert manager.live_update_daemon(["kiss"])
    assert radio.calls == [("agc", 4), ("fem", True, None)]


def test_live_kiss_update_skips_settings_already_running():
    config = {"kiss": {"port": "/dev/ttyACM0", "agc_reset_interval_seconds": 10}}
    # The wrapper records the effective (rounded) value, which must count as a match.
    radio = _DummyKissHwRadio(applied={"agc_reset_interval_seconds": 8})
    manager, _ = _kiss_manager(config, radio)

    assert manager.live_update_daemon(["repeater"])
    assert radio.calls == []


def test_live_kiss_update_reports_unsupported_settings():
    config = {"kiss": {"port": "/dev/ttyACM0", "fem_rx_gain": True, "fem_tx_gain": True}}
    radio = _DummyKissHwRadio(caps=("agc", "rx"))
    manager, _ = _kiss_manager(config, radio)

    assert manager.live_update_daemon(["kiss"]) is False
    assert radio.calls == [("fem", True, None)]


def test_live_kiss_update_ignores_legacy_key_on_non_kiss_radio():
    config = {
        "repeater": {"agc_reset_interval": 8},
        "radio": {
            "frequency": 915000000,
            "bandwidth": 250000,
            "spreading_factor": 10,
            "coding_rate": 6,
            "tx_power": 20,
        },
    }
    manager, _ = _kiss_manager(config, _DummySX1262Radio())
    assert manager.live_update_daemon(["repeater"])


class _DummyFabric:
    def __init__(self, radios, default_radio_id):
        self.radios = radios
        self.default_radio_id = default_radio_id


def test_default_physical_radio_unwraps_fabric_and_adapters():
    a, b = _DummyKissHwRadio(), _DummyKissHwRadio()
    fabric_radio = type("FabricRadio", (), {})()
    fabric_radio.fabric = _DummyFabric({"a": a, "b": b}, "b")
    adapter = type("Adapter", (), {})()
    adapter._radio = fabric_radio
    manager = ConfigManager("/tmp/config.yaml", {}, type("D", (), {"radio": adapter})())
    assert manager.default_physical_radio() is b


def test_default_kiss_section_is_scoped_to_the_default_radios_entry():
    shared = {"port": "/dev/ttyACM0"}
    config = {
        "kiss": shared,
        "radios": [{"id": "a", "radio_type": "kiss"}, {"id": "b", "radio_type": "kiss"}],
        "fabric": {"default_radio": "b"},
    }
    manager = ConfigManager("/tmp/config.yaml", config, None)

    section = manager.default_kiss_section()
    section["fem_rx_gain"] = True

    assert config["radios"][1]["kiss"] == {"port": "/dev/ttyACM0", "fem_rx_gain": True}
    assert shared == {"port": "/dev/ttyACM0"}  # radio "a" still inherits the untouched section
    assert "kiss" not in config["radios"][0]


def test_default_kiss_section_single_radio_uses_top_level():
    config = {}
    manager = ConfigManager("/tmp/config.yaml", config, None)
    manager.default_kiss_section()["agc_reset_interval_seconds"] = 4
    assert config == {"kiss": {"agc_reset_interval_seconds": 4}}


def test_live_kiss_update_retries_setting_that_never_reached_the_modem():
    """Desired values sit in the wrapper's radio_config from boot even when the
    startup apply failed; only confirmed state may cause a skip."""
    config = {
        "kiss": {"port": "/dev/ttyACM0", "agc_reset_interval_seconds": 4, "fem_rx_gain": True}
    }
    radio = _DummyKissHwRadio()  # nothing confirmed on this link
    radio.radio_config = {"agc_reset_interval_seconds": 4, "fem_rx_gain": True}
    manager, _ = _kiss_manager(config, radio)

    assert manager.live_update_daemon(["kiss"])
    assert radio.calls == [("agc", 4), ("fem", True, None)]


def test_live_kiss_update_fails_when_modem_reports_other_fem_state():
    config = {"kiss": {"port": "/dev/ttyACM0", "fem_tx_gain": True}}
    radio = _DummyKissHwRadio(fem_sticks=False)
    manager, _ = _kiss_manager(config, radio)
    assert manager.live_update_daemon(["kiss"]) is False


def test_live_kiss_update_applies_rx_boosted_gain_once():
    config = {"kiss": {"port": "/dev/ttyACM0", "rx_boosted_gain": False}}
    radio = _DummyKissHwRadio(caps=("agc", "boost"))
    manager, _ = _kiss_manager(config, radio)

    assert manager.live_update_daemon(["kiss"])
    assert manager.live_update_daemon(["kiss"])
    assert radio.calls == [("boost", False)]


def test_live_kiss_update_rx_boosted_gain_unsupported():
    config = {"kiss": {"port": "/dev/ttyACM0", "rx_boosted_gain": True}}
    manager, _ = _kiss_manager(config, _DummyKissHwRadio(caps=("agc",)))
    assert manager.live_update_daemon(["kiss"]) is False
