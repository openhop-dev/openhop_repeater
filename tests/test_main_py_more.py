import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.identity_manager import IdentityManager
from repeater.main import RepeaterDaemon


class _FakeLocalIdentity:
    def __init__(self, seed: bytes):
        self._seed = seed

    def get_public_key(self):
        # Keep deterministic first-byte hash behavior.
        return bytes([self._seed[0]]) + (b"P" * 31)

    def get_address_bytes(self):
        return b"\xab\xcd"


def _base_config():
    return {
        "repeater": {"node_name": "n1", "mode": "forward", "identity_key": b"k" * 32},
        "logging": {"level": "INFO"},
        "http": {"host": "127.0.0.1", "port": 8123},
    }


@pytest.mark.asyncio
async def test_remove_companion_stops_only_its_runtime_and_releases_identity():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    identity = _FakeLocalIdentity(b"\x10" * 32)
    other_identity = _FakeLocalIdentity(b"\x20" * 32)
    daemon.identity_manager.register_identity("deleted", identity, {}, "companion")
    daemon.identity_manager.register_identity("kept", other_identity, {}, "companion")
    daemon.identity_manager.register_identity("server", identity, {}, "room_server")
    bridge = SimpleNamespace(
        get_public_key=identity.get_public_key, stop=AsyncMock(), note_flood_copy=MagicMock()
    )
    other_bridge = SimpleNamespace(stop=AsyncMock())
    server = SimpleNamespace(bridge=bridge, stop=AsyncMock())
    other_server = SimpleNamespace(bridge=other_bridge, stop=AsyncMock())
    daemon.companion_bridges = {0x10: bridge, 0x20: other_bridge}
    daemon.companion_frame_servers = [server, other_server]
    daemon.dispatcher = SimpleNamespace(remove_raw_packet_subscriber=MagicMock())

    await daemon.remove_companion(identity.get_public_key().hex())

    server.stop.assert_awaited_once()
    bridge.stop.assert_awaited_once()
    other_server.stop.assert_not_awaited()
    other_bridge.stop.assert_not_awaited()
    daemon.dispatcher.remove_raw_packet_subscriber.assert_called_once_with(bridge.note_flood_copy)
    assert daemon.companion_bridges == {0x20: other_bridge}
    assert daemon.companion_frame_servers == [other_server]
    assert "deleted" not in daemon.identity_manager.named_identities
    assert (0x10, "companion") not in daemon.identity_manager.identities
    assert (0x10, "companion") not in daemon.identity_manager.registered_hashes
    assert (0x10, "server") in daemon.identity_manager.identities
    assert daemon.identity_manager.register_identity("replacement", identity, {}, "companion")


@pytest.mark.asyncio
async def test_remove_companion_rejects_a_different_key_with_the_same_prefix():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    identity = _FakeLocalIdentity(b"\x10" * 32)
    bridge = SimpleNamespace(get_public_key=identity.get_public_key, stop=AsyncMock())
    daemon.companion_bridges = {0x10: bridge}

    with pytest.raises(ValueError, match="public key does not match"):
        await daemon.remove_companion((b"\x10" + b"X" * 31).hex())

    bridge.stop.assert_not_awaited()
    assert daemon.companion_bridges == {0x10: bridge}


@pytest.mark.asyncio
async def test_load_additional_identities_valid_and_invalid_entries():
    cfg = _base_config()
    cfg["identities"] = {
        "room_servers": [
            {},  # missing fields
            {"name": "bad-hex", "identity_key": "zz-not-hex"},
            {"name": "bad-len", "identity_key": "aa"},
            {"name": "bad-type", "identity_key": 12345},
            {"name": "good-bytes", "identity_key": b"\x10" * 32},
            {"name": "good-hex", "identity_key": ("11" * 32)},
            {"name": "good-hex-64", "identity_key": ("22" * 64)},
        ]
    }

    daemon = RepeaterDaemon(cfg, radio=object())
    daemon.identity_manager = IdentityManager({})
    daemon._register_identity_everywhere = MagicMock(return_value=True)

    with patch("openhop_core.LocalIdentity", _FakeLocalIdentity):
        await daemon._load_additional_identities()

    # Only valid entries should be registered (including 64-byte firmware keys).
    assert daemon._register_identity_everywhere.call_count == 3
    names = [c.kwargs["name"] for c in daemon._register_identity_everywhere.call_args_list]
    assert names == ["good-bytes", "good-hex", "good-hex-64"]


@pytest.mark.asyncio
async def test_run_starts_http_and_handles_dispatcher_cancelled_gracefully():
    daemon = RepeaterDaemon(_base_config(), radio=SimpleNamespace(cleanup=MagicMock()))

    async def _init_stub():
        daemon.local_identity = SimpleNamespace(get_public_key=lambda: b"\x22" * 32)
        daemon.dispatcher = SimpleNamespace(
            run_forever=AsyncMock(side_effect=asyncio.CancelledError())
        )

    daemon.initialize = _init_stub

    fake_http_instance = SimpleNamespace(start=MagicMock(), stop=MagicMock())

    fake_loop_for_signals = SimpleNamespace(add_signal_handler=MagicMock())

    with (
        patch("asyncio.get_running_loop", return_value=fake_loop_for_signals),
        patch("repeater.main.HTTPStatsServer", return_value=fake_http_instance),
        patch("os.path.exists", return_value=False),
        # run()'s finally reaches _shutdown(), which arms the real exit
        # watchdog: a threading.Timer that calls os._exit(0) after
        # SHUTDOWN_EXIT_GRACE_S (5 s). It would fire long after this test
        # returns and take the whole pytest process down. Same guard the
        # shutdown tests in test_main_py_coverage.py already use.
        patch.object(daemon, "_arm_exit_watchdog"),
    ):
        await daemon.run()

    fake_http_instance.start.assert_called_once()
    daemon.dispatcher.run_forever.assert_awaited_once()
