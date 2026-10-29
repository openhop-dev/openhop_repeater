from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cherrypy
import pytest

from openhop_core.protocol.constants import ROUTE_TYPE_DIRECT

from repeater.handler_helpers.trace import TraceHelper
from repeater.web.api_endpoints import APIEndpoints


def _make_api(config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api.daemon_instance = None
    api.event_loop = None
    return api


@pytest.fixture
def post_request(monkeypatch):
    request = SimpleNamespace(method="POST", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request


@pytest.mark.parametrize(
    "body, error",
    [
        ({"path": []}, "non-empty"),
        ({"path": ["d"]}, "expected 2 hex"),
        ({"path": ["d0"], "path_hash_mode": 1}, "expected 4 hex"),
        ({"path": ["zz"]}, "expected 2 hex"),
        ({"path": [1.5]}, "Invalid path hop"),
        ({"path": [True]}, "Invalid path hop"),
        ({"path": [256]}, "1-byte hash"),
        ({"path": ["d0"] * 63}, "more than 62 hops"),
        ({"path": ["d0d0d0d0"] * 43, "path_hash_mode": 2}, "more than 42 hops"),
        ({"path": ["d0"], "timeout": 0}, "between 1 and 120"),
        ({"path": ["d0"], "path_hash_mode": 3}, "0, 1, or 2"),
    ],
)
def test_trace_rejects_invalid_input(post_request, body, error):
    post_request.json = body
    result = _make_api().trace()
    assert result["success"] is False
    assert error in result["error"]


def test_trace_accepts_max_hops_and_prefixed_hex(post_request):
    post_request.json = {"path": ["0xD0"] * 62}
    result = _make_api().trace()
    # Validation passed; fails later only because no daemon is attached.
    assert result["error"] == "Local repeater identity not available"


class _Packet:
    def __init__(self, path=b"", rssi=-70):
        self.header = ROUTE_TYPE_DIRECT
        self.path = bytearray(path)
        self.path_len = len(self.path)
        self.rssi = rssi
        self.payload = b"\x00"

    def is_route_direct(self):
        return True

    def get_snr(self):
        return 5.0


class _Identity:
    def get_public_key(self):
        return bytes([0x42]) + bytes(31)


def _helper(tag, trace_bytes):
    repeater_handler = MagicMock()
    repeater_handler.is_duplicate.return_value = False
    repeater_handler.config = {"repeater": {"mode": "forward"}}
    helper = TraceHelper(
        local_hash=0x42, local_identity=_Identity(), repeater_handler=repeater_handler
    )
    helper._forward_trace_packet = AsyncMock()
    helper._create_trace_record = MagicMock(return_value={"is_trace": True})
    helper._extract_path_info = MagicMock(return_value=([], []))
    helper.trace_handler._format_trace_response = MagicMock(return_value="trace")
    helper.trace_handler._parse_trace_payload = MagicMock(
        return_value={
            "valid": True,
            "trace_path_bytes": trace_bytes,
            "flags": 0,
            "trace_hops": [bytes([b]) for b in trace_bytes],
            "trace_path": list(trace_bytes),
            "tag": tag,
        }
    )
    return helper, repeater_handler


@pytest.mark.asyncio
async def test_register_trace_requires_local_tail():
    helper, _ = _helper(1, b"\xd0\x42")
    with pytest.raises(ValueError):
        helper.register_trace(1, b"\xd0\x99", 0, 15)
    with pytest.raises(ValueError):
        helper.register_trace(1, b"\x42", 0, 15)


@pytest.mark.asyncio
async def test_completed_trace_is_consumed_and_logged():
    tag = 10
    helper, repeater_handler = _helper(tag, b"\xd0\x42")
    event = helper.register_trace(tag, b"\xd0\x42", 0, 15)

    await helper.process_trace_packet(_Packet(path=b"\x14"))

    assert event.is_set()
    assert helper.pending_pings[tag]["result"]["path_snrs"] == [0x14, 20]
    repeater_handler.log_trace_record.assert_called_once()
    helper._forward_trace_packet.assert_not_awaited()


@pytest.mark.asyncio
async def test_overheard_intermediate_hop_does_not_complete_trace():
    tag = 11
    helper, _ = _helper(tag, b"\xd0\xe1\x42")
    event = helper.register_trace(tag, b"\xd0\xe1\x42", 0, 15)

    await helper.process_trace_packet(_Packet(path=b"\x14"))

    assert not event.is_set()
    assert helper.pending_pings[tag]["result"] is None
