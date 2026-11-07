"""Channel send responses stay JSON serialisable across Core return types."""

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from repeater.web.companion_endpoints import CompanionAPIEndpoints


@dataclass
class _SentResult:
    # Installed Core versions may have SentResult without PR #174's __bool__.
    success: bool

    def __bool__(self):
        return self.success


@pytest.mark.parametrize("success", [True, False], ids=["success", "failure"])
@pytest.mark.parametrize("result_type", [bool, _SentResult], ids=["bool", "sent-result"])
def test_send_channel_message_sent_is_json_bool(success, result_type):
    bridge = SimpleNamespace(send_channel_message=AsyncMock(return_value=result_type(success)))
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._require_post = lambda: None
    ep._get_json_body = lambda: {"channel_idx": 2, "text": "hello"}
    ep._get_bridge = lambda **kwargs: bridge
    ep._run_async = asyncio.run

    response = CompanionAPIEndpoints.send_channel_message.__wrapped__(ep)

    assert response["success"] is True
    assert response["data"]["sent"] is success
    assert json.loads(json.dumps(response)) == {"success": True, "data": {"sent": success}}
    bridge.send_channel_message.assert_awaited_once_with(2, "hello")
