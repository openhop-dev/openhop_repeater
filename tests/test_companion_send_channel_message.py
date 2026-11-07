"""``POST /api/companion/send_channel_message`` passes a timestamp and a flood scope to Core.

A client sends the message's timestamp, as CMD_SEND_CHANNEL_TXT_MSG carries it, and may
scope the message to one region, as the OPENHOP_CHANNEL_TXT_SCOPED frame extension does.
These pin what reaches ``send_channel_message``; the packet itself is Core's.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cherrypy
import pytest

SCOPE = bytes(range(1, 17))


def _endpoint(body):
    """The endpoint with its request and bridge stubbed, and the bridge's recorded send."""
    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    bridge = SimpleNamespace(send_channel_message=AsyncMock(return_value=True))
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._require_post = lambda: None
    ep._get_json_body = lambda: {"channel_idx": 2, "text": "hi", **body}
    ep._resolve_bridge_params = lambda b: {}
    ep._get_bridge = lambda **kw: bridge
    ep._run_async = lambda coro, **kw: asyncio.run(coro)
    return ep, bridge.send_channel_message


def _invoke(ep):
    """Call the endpoint past the @require_auth wrapper (no auth context in tests)."""
    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    return CompanionAPIEndpoints.send_channel_message.__wrapped__(ep)


def test_the_flood_scope_key_reaches_core_as_bytes():
    ep, send = _endpoint({"flood_scope_key": SCOPE.hex()})
    _invoke(ep)
    send.assert_awaited_once_with(2, "hi", flood_scope_key=SCOPE)


@pytest.mark.parametrize("timestamp", [1_700_000_000, 0])
def test_the_timestamp_reaches_core_as_given(timestamp):
    ep, send = _endpoint({"timestamp": timestamp})
    _invoke(ep)
    send.assert_awaited_once_with(2, "hi", timestamp=timestamp)


def test_without_the_fields_the_call_is_unchanged():
    ep, send = _endpoint({})
    assert _invoke(ep)["data"] == {"sent": True}
    send.assert_awaited_once_with(2, "hi")


@pytest.mark.parametrize(
    "field",
    [
        {"timestamp": "x"},
        {"timestamp": 2**32},
        {"flood_scope_key": "zz"},
        {"flood_scope_key": "00" * 16},
        {"flood_scope_key": SCOPE.hex()[:30]},
        {"flood_scope_key": 7},
    ],
)
def test_a_bad_timestamp_or_key_is_rejected(field):
    ep, send = _endpoint(field)
    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke(ep)
    assert exc_info.value.code == 400
    send.assert_not_awaited()
