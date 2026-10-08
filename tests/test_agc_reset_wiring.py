"""Wiring for the AGC reset interval (MeshCore "set agc.reset.interval").

The reset itself lives in the core Dispatcher; the repeater's job is to feed
``repeater.agc_reset_interval`` into ``dispatcher.agc_reset_interval`` at
startup and on live config updates, and to accept the same values through the
mesh CLI as firmware does.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from repeater.config_manager import ConfigManager
from repeater.handler_helpers.mesh_cli import MeshCLI


def _daemon_with_dispatcher(config):
    return SimpleNamespace(
        config=config,
        radio=None,
        repeater_handler=None,
        advert_helper=None,
        dispatcher=SimpleNamespace(agc_reset_interval=0),
    )


def _cfg_mgr():
    return SimpleNamespace(
        save_to_file=MagicMock(return_value=(True, None)),
        live_update_daemon=MagicMock(),
    )


def test_live_update_applies_agc_reset_interval_to_dispatcher():
    config = {"repeater": {"agc_reset_interval": 8}}
    daemon = _daemon_with_dispatcher(config)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["repeater"])
    assert daemon.dispatcher.agc_reset_interval == 8


def test_live_update_clears_agc_reset_interval_when_removed():
    config = {"repeater": {}}
    daemon = _daemon_with_dispatcher(config)
    daemon.dispatcher.agc_reset_interval = 8
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["repeater"])
    assert daemon.dispatcher.agc_reset_interval == 0


def test_cli_set_agc_reset_interval_matches_firmware():
    # MeshCore clamps to 0-1020 s, then stores a multiple of 4 (MeshCore #3469).
    config = {"repeater": {}, "mesh": {}}
    cli = MeshCLI("/tmp/cfg.yaml", config, _cfg_mgr())

    for sent, stored in ((256, 256), (1000, 1000), (5000, 1020), (-5, 0), (17, 16)):
        assert cli._cmd_set(f"agc.reset.interval {sent}") == f"OK - interval rounded to {stored}"
        assert config["repeater"]["agc_reset_interval"] == stored
    cli.config_manager.live_update_daemon.assert_called_with(["repeater"])
