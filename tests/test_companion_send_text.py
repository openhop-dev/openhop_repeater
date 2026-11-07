"""``POST /api/companion/send_text`` passes a retry's attempt and timestamp to Core.

A client retrying a direct message sends the message's timestamp and the next
attempt, as CMD_SEND_TXT_MSG carries them. These pin what reaches
``send_text_message``; the packet itself is Core's.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cherrypy
import pytest

KEY = bytes(range(32))


def _endpoint(body):
    """The endpoint with its request and bridge stubbed, and the bridge's recorded send."""
    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    sent = SimpleNamespace(success=True, is_flood=False, expected_ack=0x1234)
    bridge = SimpleNamespace(send_text_message=AsyncMock(return_value=sent))
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._require_post = lambda: None
    ep._get_json_body = lambda: {"pub_key": KEY.hex(), "text": "hi", **body}
    ep._resolve_bridge_params = lambda b: {}
    ep._get_bridge = lambda **kw: bridge
    ep._run_async = lambda coro, **kw: asyncio.run(coro)
    return ep, bridge.send_text_message


def _invoke(ep):
    """Call the endpoint past the @require_auth wrapper (no auth context in tests)."""
    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    return CompanionAPIEndpoints.send_text.__wrapped__(ep)


def test_attempt_and_timestamp_reach_core():
    ep, send = _endpoint({"attempt": 5, "timestamp": 1_700_000_000})
    _invoke(ep)
    send.assert_awaited_once_with(KEY, "hi", txt_type=0, attempt=5, timestamp=1_700_000_000)


def test_without_the_fields_the_call_is_unchanged():
    ep, send = _endpoint({})
    _invoke(ep)
    send.assert_awaited_once_with(KEY, "hi", txt_type=0)


@pytest.mark.parametrize(
    ("txt_type", "timestamp"), [(1, 1_700_000_000), (3, 1_700_000_000), (0, 0)]
)
def test_cli_types_and_zero_take_a_fresh_timestamp(txt_type, timestamp):
    ep, send = _endpoint({"txt_type": txt_type, "attempt": 2, "timestamp": timestamp})
    _invoke(ep)
    send.assert_awaited_once_with(KEY, "hi", txt_type=txt_type, attempt=2)


@pytest.mark.parametrize("field", [{"attempt": 256}, {"timestamp": "x"}])
def test_a_bad_attempt_or_timestamp_is_rejected(field):
    ep, send = _endpoint(field)
    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke(ep)
    assert exc_info.value.code == 400
    send.assert_not_awaited()


def test_wait_for_ack_false_reaches_core():
    ep, send = _endpoint({"wait_for_ack": False})
    _invoke(ep)
    send.assert_awaited_once_with(KEY, "hi", txt_type=0, wait_for_ack=False)


@pytest.mark.parametrize("body", [{}, {"wait_for_ack": True}])
def test_waiting_for_the_ack_is_the_default_and_answers_as_before(body):
    ep, send = _endpoint(body)
    answer = _invoke(ep)
    send.assert_awaited_once_with(KEY, "hi", txt_type=0)
    assert answer["data"] == {"sent": True, "is_flood": False, "expected_ack": 0x1234}
