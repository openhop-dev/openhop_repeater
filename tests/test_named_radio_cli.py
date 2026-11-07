"""Coverage for direct OpenHop ``radio.<id>.<command>`` controls."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from repeater.handler_helpers.mesh_cli import MeshCLI


class _FakeRadio:
    def __init__(self, frequency=915_000_000, bandwidth=125_000, sf=7, cr=5, tx_power=22):
        self.primary = {
            "frequency": frequency,
            "bandwidth": bandwidth,
            "spreading_factor": sf,
            "coding_rate": cr,
        }
        self.tx_power = tx_power
        self.configure_calls = []
        self.tx_calls = []
        self.radio2_calls = []
        self.tempradio2_calls = []
        self.radio2 = {
            "frequency": 909_500_000,
            "bandwidth": 62_500,
            "spreading_factor": 8,
            "coding_rate": 5,
            "mode": 2,
            "preamble_length": 96,
        }
        self.temporary_radio2 = {
            "frequency": 0,
            "bandwidth": 0,
            "spreading_factor": 0,
            "coding_rate": 0,
            "mode": 0,
            "preamble_length": 0,
            "remaining_minutes": 0,
            "active": False,
        }

    def configure_radio(self, frequency, bandwidth, spreading_factor, coding_rate):
        self.configure_calls.append((frequency, bandwidth, spreading_factor, coding_rate))
        self.primary = {
            "frequency": frequency,
            "bandwidth": bandwidth,
            "spreading_factor": spreading_factor,
            "coding_rate": coding_rate,
        }
        return True

    def get_radio_config(self):
        return dict(self.primary)

    def get_tx_power(self):
        return self.tx_power

    def set_tx_power(self, power):
        self.tx_calls.append(power)
        self.tx_power = power
        return True

    def get_radio2_config(self):
        return dict(self.radio2)

    def set_radio2_config(
        self, frequency, bandwidth, spreading_factor, coding_rate, *, mode, preamble_length
    ):
        self.radio2_calls.append(
            (frequency, bandwidth, spreading_factor, coding_rate, mode, preamble_length)
        )
        self.radio2 = {
            "frequency": frequency,
            "bandwidth": bandwidth,
            "spreading_factor": spreading_factor,
            "coding_rate": coding_rate,
            "mode": mode,
            "preamble_length": preamble_length,
        }
        return True

    def get_temporary_radio2_config(self):
        return dict(self.temporary_radio2)

    def set_temporary_radio2_config(
        self,
        frequency,
        bandwidth,
        spreading_factor,
        coding_rate,
        duration_minutes,
        *,
        mode,
        preamble_length,
    ):
        self.tempradio2_calls.append(
            (
                frequency,
                bandwidth,
                spreading_factor,
                coding_rate,
                duration_minutes,
                mode,
                preamble_length,
            )
        )
        self.temporary_radio2 = {
            "frequency": frequency,
            "bandwidth": bandwidth,
            "spreading_factor": spreading_factor,
            "coding_rate": coding_rate,
            "mode": mode,
            "preamble_length": preamble_length,
            "remaining_minutes": duration_minutes,
            "active": mode != 0 and duration_minutes != 0,
        }
        return True


def _config():
    return {
        "repeater": {"name": "named-radio-test"},
        "mesh": {},
        "radio": {
            "frequency": 915_000_000,
            "bandwidth": 125_000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "tx_power": 22,
        },
    }


def _cli_with_fabric():
    local = _FakeRadio()
    dotted = _FakeRadio(frequency=909_500_000, bandwidth=62_500)
    fabric = SimpleNamespace(radios={"local": local, "local.link": dotted})
    daemon = SimpleNamespace(
        radio=SimpleNamespace(fabric=fabric),
        radio_stack_meta={"radio_ids": ["local", "local.link"]},
    )
    manager = SimpleNamespace(
        daemon=daemon,
        save_to_file=MagicMock(return_value=True),
        live_update_daemon=MagicMock(),
    )
    return MeshCLI("/tmp/config.yaml", _config(), manager), local, dotted, manager


def test_named_radio_uses_longest_exact_dotted_id_and_never_saves_config():
    cli, local, dotted, manager = _cli_with_fabric()

    assert cli._route_command("radio.local.link.get") == "> 909.5,62.5,7,5"
    assert cli._route_command("radio.local.link.set 909.5 62.5 9 6") == "OK"
    assert dotted.configure_calls == [(909_500_000, 62_500, 9, 6)]
    assert local.configure_calls == []
    manager.save_to_file.assert_not_called()
    manager.live_update_daemon.assert_not_called()


def test_named_radio_unknown_id_cannot_fall_back_to_default_endpoint():
    cli, local, dotted, _ = _cli_with_fabric()

    result = cli._route_command("radio.missing.tx 17")

    assert result.startswith("Error: Unknown radio")
    assert local.tx_calls == []
    assert dotted.tx_calls == []


def test_named_radio_kiss_profiles_and_tx_use_the_selected_radio():
    cli, local, _, _ = _cli_with_fabric()

    assert cli._route_command("radio.local.tx") == "> 22"
    assert cli._route_command("radio.local.tx 17") == "OK"
    assert local.tx_calls == [17]

    assert cli._route_command("radio.local.radio2") == "> 909.5,62.5,8,5,rxtx,96"
    assert cli._route_command("radio.local.radio2 909.5 62.5 9 5 rx 32") == "OK"
    assert local.radio2_calls == [(909_500_000, 62_500, 9, 5, 1, 32)]

    command = "radio.local.tempradio2 909.5 62.5 8 5 rxtx 1 96"
    assert cli._route_command(command) == "OK"
    assert local.tempradio2_calls[-1] == (909_500_000, 62_500, 8, 5, 1, 2, 96)
    assert cli._route_command("radio.local.tempradio2") == "> 909.5,62.5,8,5,rxtx,1,96"

    assert cli._route_command("radio.local.tempradio2 off") == "OK"
    assert local.tempradio2_calls[-1] == (0, 0, 0, 0, 0, 0, 0)

    # The canonical radio2 temporary form retains the classic tempradio
    # tuple-plus-timeout order; mode/preamble are optional extensions.
    command = "radio.local.tempradio2 909.5 62.5 8 5 2 rx 80"
    assert cli._route_command(command) == "OK"
    assert local.tempradio2_calls[-1] == (909_500_000, 62_500, 8, 5, 2, 1, 80)


def test_named_radio_supports_unambiguous_single_radio_stack():
    radio = _FakeRadio()
    daemon = SimpleNamespace(radio=radio, radio_stack_meta={"radio_ids": ["radio0"]})
    manager = SimpleNamespace(
        daemon=daemon,
        save_to_file=MagicMock(return_value=True),
        live_update_daemon=MagicMock(),
    )
    cli = MeshCLI("/tmp/config.yaml", _config(), manager)

    assert cli._route_command("radio.radio0.tx 18") == "OK"
    assert radio.tx_calls == [18]
    assert "primary: 915,125,7,5" in cli._route_command("radio.radio0.status")


def test_named_radio_help_explains_direct_syntax_and_runtime_scope():
    cli, _, _, _ = _cli_with_fabric()

    help_text = cli._route_command("help")
    detail = cli._route_command("help radio")

    assert "radio.<id>.get" in help_text
    assert "do not save YAML" in help_text
    assert "radio.<id>.<command>" in detail
    assert "do not persist" in detail
