"""Companion sends answer the sent packet's hash in the form the packet log keys it by."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.protocol.identity import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.web.companion_endpoints import CompanionAPIEndpoints

PEER_KEY = LocalIdentity().get_public_key()


def _logged_hash(packet):
    """The hash the engine's packet record keys this packet by."""
    from repeater.engine import RepeaterHandler

    dispatcher = MagicMock()
    dispatcher.radio = MagicMock(
        spreading_factor=8, bandwidth=125000, coding_rate=8, preamble_length=17
    )
    dispatcher.send_packet = AsyncMock()
    config = {
        "repeater": {"mode": "forward", "cache_ttl": 3600, "node_name": "test-node"},
        "duty_cycle": {"max_airtime_per_minute": 3600},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 125000,
            "coding_rate": 8,
            "preamble_length": 17,
        },
    }
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        handler = RepeaterHandler(config, dispatcher, 0x42, local_hash_bytes=bytes([0x42]))
    record = handler._build_packet_record(
        packet,
        packet.get_payload_type(),
        packet.get_route_type(),
        -90,
        7.5,
        [],
        1,
        None,
        None,
        None,
    )
    return record["packet_hash"]


def _answer(endpoint, result):
    """The endpoint's answer for a send core reports as ``result``."""
    bridge = SimpleNamespace(
        send_text_message=AsyncMock(return_value=result),
        send_channel_message=AsyncMock(return_value=result),
    )
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._require_post = lambda: None
    ep._get_json_body = lambda: {"pub_key": PEER_KEY.hex(), "channel_idx": 0, "text": "hi"}
    ep._resolve_bridge_params = lambda body: {}
    ep._get_bridge = lambda **kwargs: bridge
    ep._run_async = lambda coro, **kwargs: asyncio.run(coro)
    return getattr(CompanionAPIEndpoints, endpoint).__wrapped__(ep)["data"]


@pytest.mark.parametrize("endpoint", ["send_text", "send_channel_message"])
def test_the_answer_carries_the_hash_the_packet_log_keys_the_packet_by(endpoint):
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="hi",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": bytes(range(16)).hex()}],
    )
    # As core reports it on its send result (openhop_core#174): the whole digest.
    result = SimpleNamespace(
        success=True, is_flood=True, expected_ack=1, packet_hash=packet.calculate_packet_hash()
    )

    assert _answer(endpoint, result)["packet_hash"] == _logged_hash(packet)


@pytest.mark.parametrize("endpoint", ["send_text", "send_channel_message"])
def test_without_a_hash_from_core_the_answer_is_unchanged(endpoint):
    result = SimpleNamespace(success=True, is_flood=False, expected_ack=1)
    assert "packet_hash" not in _answer(endpoint, result)
