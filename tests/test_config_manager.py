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


def test_live_budget_scope_change_is_saved_for_restart_without_regrouping_runtime():
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
                "id": "north",
                "radio_type": "sx1262",
                "radio": {"frequency": 910100000, "bandwidth": 500000},
            },
            {
                "id": "south",
                "radio_type": "sx1262",
                "radio": {"frequency": 910100000, "bandwidth": 500000},
            },
        ],
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
    }
    budgets = AirtimeBudgets(config)
    budgets.for_radio("north").record_tx(900)
    handler = _DummyRepeaterHandler(config)
    handler.airtime_budgets = budgets
    handler.airtime_mgr = budgets.default
    daemon = _DummyDaemon(config, _DummySX1262Radio())
    daemon.repeater_handler = handler
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    config["duty_cycle"]["budget_scope"] = "radio"

    assert manager.live_update_daemon(["duty_cycle"]) is False
    assert budgets.budget_scope == "channel"
    assert budgets.shares_budget("north", "south")
    assert budgets.for_radio("south").get_stats()["current_airtime_ms"] == 900


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
