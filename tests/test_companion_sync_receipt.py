"""A synced companion message is removed on the client's next command, not on read.

``SYNC_NEXT_MESSAGE`` used to delete the SQLite row before the frame reached
the client, so a connection that dropped in between lost the message. The
row is now deleted when the same client sends its next command; a client that
disconnects, or is evicted, first is sent the message again.
"""

from __future__ import annotations

import pytest
from openhop_core.companion.constants import CMD_SYNC_NEXT_MESSAGE, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.message_queue import MessageQueue

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"


class _Bridge:
    def __init__(self):
        self.message_queue = MessageQueue(max_size=100)

    def sync_next_message(self):
        return self.message_queue.pop()


def _server(handler, writer=None):
    fs = CompanionFrameServer(_Bridge(), _HASH, port=0, sqlite_handler=handler)
    fs._app_target_ver = 3
    fs._client_writer = writer or object()
    fs.frames = []
    fs._write_frame = lambda data: fs.frames.append(bytes(data))
    return fs


def _push(handler, text, packet_hash):
    assert handler.companion_push_message(
        _HASH,
        {
            "sender_key": b"\x11" * 32,
            "text": text,
            "timestamp": 1000,
            "txt_type": 0,
            "is_channel": False,
            "channel_idx": 0,
            "path_len": 0xFF,
            "packet_hash": packet_hash,
        },
        100,
    )


@pytest.mark.asyncio
async def test_message_stays_queued_until_the_next_command(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "first", "p1")
    fs = _server(handler)

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] != RESP_CODE_NO_MORE_MESSAGES
    assert handler.companion_count_messages(_HASH) == 1

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] == RESP_CODE_NO_MORE_MESSAGES
    assert handler.companion_count_messages(_HASH) == 0


@pytest.mark.asyncio
async def test_a_client_that_drops_first_is_sent_the_message_again(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "only", "p1")
    fs = _server(handler)
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

    # Disconnect or eviction: the base replaces the writer before any further
    # command. The next client's first command is not the old client's receipt.
    fs._client_writer = object()
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] != RESP_CODE_NO_MORE_MESSAGES
    assert handler.companion_count_messages(_HASH) == 1


@pytest.mark.asyncio
async def test_a_client_evicted_during_the_read_is_not_served(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "only", "p1")
    fs = _server(handler)
    original = handler.companion_peek_message

    def swap_then_read(companion_hash):
        fs._client_writer = object()
        return original(companion_hash)

    handler.companion_peek_message = swap_then_read
    await fs._cmd_sync_next_message(b"")
    assert fs.frames == []
    assert fs._pending_delete is None
    assert handler.companion_count_messages(_HASH) == 1


@pytest.mark.asyncio
async def test_a_message_too_large_for_a_frame_is_dropped_not_stuck(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "x" * 170, "p1")
    _push(handler, "fits", "p2")
    fs = _server(handler)

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert len(fs.frames) == 1 and b"fits" in fs.frames[0]
    assert handler.companion_count_messages(_HASH) == 1
