"""A companion that opts in keeps what it receives and sends readable after a frame sync."""

import asyncio
import io
import json
import sqlite3
import struct
import time
from types import SimpleNamespace

import cherrypy
import pytest
from openhop_core import LocalIdentity
from openhop_core.companion.constants import (
    CMD_SEND_CHANNEL_TXT_MSG,
    CMD_SEND_TXT_MSG,
    CMD_SYNC_NEXT_MESSAGE,
    RESP_CODE_NO_MORE_MESSAGES,
    RESP_CODE_SENT,
    TXT_TYPE_CLI_COMMAND,
    TXT_TYPE_CLI_DATA,
)
from openhop_core.companion.models import (
    Channel,
    ChannelDataEvent,
    ChannelMessageEvent,
    Contact,
    MessageEvent,
)

from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.companion_endpoints import CompanionAPIEndpoints

_HASH = "0x01"
_PUBLIC_KEY = b"\x01" + b"\x11" * 31
_PEER_KEY = LocalIdentity(seed=bytes.fromhex("bb" * 32)).get_public_key()


@pytest.fixture
def db(tmp_path):
    return SQLiteHandler(tmp_path)


async def _inject(packet, **kwargs):
    return True


def _bridge(db, injector=_inject, **kwargs):
    kwargs.setdefault("message_history", True)
    bridge = RepeaterCompanionBridge(
        LocalIdentity(), injector, sqlite_handler=db, companion_hash=_HASH, **kwargs
    )
    bridge.contacts.add(Contact(public_key=_PEER_KEY, name="Peer"))
    bridge.channels.set(0, Channel(name="Public", secret=bytes(range(16))))
    return bridge


def _endpoints(db, bridge):
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._get_bridge = lambda **kw: bridge
    ep.daemon_instance = SimpleNamespace(
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=db))
    )
    return ep


def _messages(ep, **kwargs):
    return CompanionAPIEndpoints.messages.__wrapped__(ep, **kwargs)["data"]


async def _post(ep, endpoint, body):
    """POST from a server thread, as CherryPy does, onto the running daemon loop."""
    ep.event_loop = asyncio.get_running_loop()

    def call():
        cherrypy.request.method = "POST"
        cherrypy.request.body = io.BytesIO(json.dumps(body).encode())
        return getattr(CompanionAPIEndpoints, endpoint).__wrapped__(ep)["data"]

    return await asyncio.to_thread(call)


def _frame_dm(txt_type=0):
    header = bytes([CMD_SEND_TXT_MSG, txt_type, 0]) + struct.pack("<I", 1700000000)
    return header + _PEER_KEY[:6] + b"hi"


def _frame_server(db, bridge):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = db
    fs.companion_hash = _HASH
    fs.bridge = bridge
    fs._app_target_ver = 3
    fs._cmd_handlers = {
        CMD_SYNC_NEXT_MESSAGE: fs._cmd_sync_next_message,
        CMD_SEND_TXT_MSG: fs._cmd_send_txt_msg,
        CMD_SEND_CHANNEL_TXT_MSG: fs._cmd_send_channel_txt_msg,
    }
    fs.frames = []
    fs._write_frame = lambda data: fs.frames.append(bytes(data))
    fs._enqueue_frame = fs.frames.append
    return fs


@pytest.mark.asyncio
async def test_a_frame_sync_does_not_remove_history(db):
    bridge = _bridge(db)
    fs = _frame_server(db, bridge)
    bridge.on_message_event(fs._on_message_event)
    event = MessageEvent(
        b"\x11" * 32, "hello", 1, 0, sender_prefix=b"\xaa\xbb\xcc\xdd", packet_hash="p1"
    )
    await bridge._fire_callbacks("message_event", event)

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] != RESP_CODE_NO_MORE_MESSAGES
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] == RESP_CODE_NO_MORE_MESSAGES

    await bridge.stop()
    ep = _endpoints(db, bridge)
    (row,) = _messages(ep)
    assert (row["text"], row["sender_key"], row["sender_prefix"], row["packet_hash"]) == (
        "hello",
        "11" * 32,
        "aabbccdd",
        "p1",
    )
    assert _messages(ep, since=row["id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, event, expected",
    [
        (
            "message_event",
            MessageEvent(b"a" * 32, "dm", 7, 0, packet_hash="d", queued=False),
            {"is_channel": False, "text": "dm", "sender_key": (b"a" * 32).hex()},
        ),
        (
            "channel_message_event",
            ChannelMessageEvent("Public", "Peer", "Peer: hi", 8, 0, 2, "c", queued=False),
            {"is_channel": True, "text": "Peer: hi", "channel_idx": 2, "sender_key": ""},
        ),
        (
            "channel_data_event",
            ChannelDataEvent(2, 0, 1, b"payload", packet_hash="x", queued=False),
            {"is_channel": True, "channel_data_type": 1, "channel_data_payload": b"payload".hex()},
        ),
    ],
    ids=["dm", "channel", "data"],
)
async def test_history_records_what_the_frame_queue_refuses(db, name, event, expected):
    bridge = _bridge(db, offline_queue_size=0)
    await bridge._fire_callbacks(name, event)

    await bridge.stop()
    (row,) = _messages(_endpoints(db, bridge))
    assert {key: row[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_history_is_off_by_default(db):
    bridge = _bridge(db, message_history=False)
    ep = _endpoints(db, bridge)
    await bridge._fire_callbacks("message_event", MessageEvent(b"a" * 32, "x", 1, 0))
    await _post(ep, "send_text", {"pub_key": _PEER_KEY.hex(), "text": "y"})

    await bridge.stop()
    assert db.companion_load_history(bridge.get_public_key(), 0, 10) == []
    with pytest.raises(cherrypy.HTTPError) as error:
        _messages(ep)
    assert error.value.status == 404


def test_the_cursor_pages_oldest_first_and_bounds_its_input(db):
    bridge = _bridge(db)
    for n in range(5):
        db.companion_record_history(bridge.get_public_key(), {"text": f"m{n}"})
    ep = _endpoints(db, bridge)

    first = _messages(ep, limit=2)
    assert [m["text"] for m in first] == ["m0", "m1"]
    rest = _messages(ep, since=first[-1]["id"], limit=10)
    assert [m["text"] for m in rest] == ["m2", "m3", "m4"]
    assert _messages(ep, since=rest[-1]["id"]) == []

    # SQLite reads a negative LIMIT as no limit at all.
    assert len(_messages(ep, limit=-1)) == 1
    with pytest.raises(cherrypy.HTTPError) as error:
        _messages(ep, since="x")
    assert error.value.status == 400


def test_history_belongs_to_the_full_public_key(db):
    mine, other = _PUBLIC_KEY, b"\x01" + b"\x22" * 31
    db.companion_record_history(mine, {"text": "mine"})
    db.companion_record_history(other, {"text": "other"})

    assert [m["text"] for m in db.companion_load_history(mine, 0, 10)] == ["mine"]
    assert [m["text"] for m in db.companion_load_history(other, 0, 10)] == ["other"]


def _age(db, text):
    with db._connect() as conn:
        conn.execute(
            "UPDATE companion_message_history SET created_at = ? WHERE text = ?",
            (time.time() - 40 * 86400, text),
        )
        conn.commit()


def test_retention_expires_by_record_time_and_never_reissues_an_id(db):
    for text in ("old", "recent"):
        db.companion_record_history(_PUBLIC_KEY, {"text": text})
    _age(db, "old")

    db.cleanup_old_data()
    assert len(db.companion_load_history(_PUBLIC_KEY, 0, 10)) == 2
    db.cleanup_old_data(companion_events_days=31)
    assert [m["text"] for m in db.companion_load_history(_PUBLIC_KEY, 0, 10)] == ["recent"]

    # Once retention empties the table, a new row must still land after a
    # client's cursor (id 2), not restart at 1.
    _age(db, "recent")
    db.cleanup_old_data(companion_events_days=31)
    db.companion_record_history(_PUBLIC_KEY, {"text": "new"})
    assert [m["id"] for m in db.companion_load_history(_PUBLIC_KEY, 2, 10)] == [3]


def test_purging_companion_messages_clears_their_history(db):
    db.companion_record_history(_PUBLIC_KEY, {"text": "private"})

    db.purge_table("companion_messages")
    assert db.companion_load_history(_PUBLIC_KEY, 0, 10) == []


# The pin is openhop_core@dev, which reports sends once the Core change lands.
_reports_sends = pytest.mark.skipif(
    not hasattr(RepeaterCompanionBridge, "on_message_sent"),
    reason="installed openhop_core does not report the messages a companion sends",
)


@_reports_sends
@pytest.mark.asyncio
@pytest.mark.parametrize("to", ["dm", "channel"])
@pytest.mark.parametrize("client", ["rest", "frame"])
async def test_a_send_from_either_client_is_recorded(db, client, to):
    bridge = _bridge(db)
    ep = _endpoints(db, bridge)
    fs = _frame_server(db, bridge)
    if client == "rest" and to == "dm":
        reply = await _post(ep, "send_text", {"pub_key": _PEER_KEY.hex(), "text": "hi"})
        expected_ack = reply["expected_ack"]
    elif client == "rest":
        await _post(ep, "send_channel_message", {"channel_idx": 0, "text": "hi"})
    elif to == "dm":
        await fs._handle_cmd(_frame_dm())
        assert fs.frames[-1][0] == RESP_CODE_SENT
        expected_ack = int.from_bytes(fs.frames[-1][2:6], "little")
    else:
        header = bytes([CMD_SEND_CHANNEL_TXT_MSG, 0, 0]) + struct.pack("<I", 1700000000)
        await fs._handle_cmd(header + b"hi")

    await bridge.stop()
    (row,) = _messages(ep)
    assert (row["sender_key"], row["text"]) == (bridge.get_public_key().hex(), "hi")
    if client == "frame":
        assert row["timestamp"] == 1700000000
    if to == "dm":
        assert (row["is_channel"], row["recipient_key"], row["expected_ack"]) == (
            False,
            _PEER_KEY.hex(),
            expected_ack,
        )
    else:
        assert (row["is_channel"], row["channel_idx"], row["recipient_key"]) == (True, 0, "")


@pytest.mark.asyncio
async def test_cli_traffic_stays_out_of_the_history(db):
    bridge = _bridge(db)
    ep = _endpoints(db, bridge)
    for txt_type in (TXT_TYPE_CLI_DATA, TXT_TYPE_CLI_COMMAND):
        reply = MessageEvent(_PEER_KEY, "password now: secret", 1, txt_type)
        await bridge._fire_callbacks("message_event", reply)
    cli = {"pub_key": _PEER_KEY.hex(), "text": "password secret", "txt_type": TXT_TYPE_CLI_DATA}
    await _post(ep, "send_text", cli)
    await _frame_server(db, bridge)._handle_cmd(_frame_dm(TXT_TYPE_CLI_DATA))

    await bridge.stop()
    assert _messages(ep) == []


def _lock_writes(db):
    """Hold SQLite's write lock from another connection."""
    lock = sqlite3.connect(db.sqlite_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    return lock


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["receive", pytest.param("send", marks=_reports_sends)])
async def test_a_locked_database_holds_up_neither_a_send_nor_a_delivery(db, path):
    sent = []

    async def inject(packet, **kwargs):
        sent.append(packet)
        return True

    bridge = _bridge(db, injector=inject)
    delivered = []
    bridge.on_message_event(delivered.append)
    lock = _lock_writes(db)
    if path == "send":
        await asyncio.wait_for(bridge.send_text_message(_PEER_KEY, "kept"), 1)
        assert sent
    else:
        event = MessageEvent(_PEER_KEY, "kept", 1, 0)
        await asyncio.wait_for(bridge._fire_callbacks("message_event", event), 1)
        assert delivered

    # The row waited for SQLite rather than being lost.
    lock.close()
    await bridge.stop()
    assert [m["text"] for m in db.companion_load_history(bridge.get_public_key(), 0, 10)] == [
        "kept"
    ]


@pytest.mark.asyncio
async def test_a_database_that_stays_locked_bounds_the_queue_and_the_shutdown(
    db, monkeypatch, caplog
):
    monkeypatch.setattr("repeater.companion.bridge._HISTORY_FLUSH_TIMEOUT_SEC", 0.1)
    caplog.set_level("WARNING", logger="RepeaterCompanionBridge")
    bridge = _bridge(db)
    lock = _lock_writes(db)
    # The writer takes the first row and waits on the lock, the queue holds the
    # next 512, and one more is too many.
    for n in range(514):
        await bridge._fire_callbacks("message_event", MessageEvent(_PEER_KEY, f"m{n}", 1, 0))
        await asyncio.sleep(0)
    assert caplog.text.count("message history queue full (512), dropping a message") == 1

    await bridge.stop()
    assert "513 message(s) not written to the history before stopping" in caplog.text
    lock.close()


@pytest.mark.asyncio
async def test_a_message_the_router_delivers_after_stop_is_still_recorded(db):
    bridge = _bridge(db)
    await bridge._fire_callbacks("message_event", MessageEvent(_PEER_KEY, "before", 1, 0))
    await bridge.stop()
    # The daemon stops the router, which drains its in-flight packets, after the bridges.
    await bridge._fire_callbacks("message_event", MessageEvent(_PEER_KEY, "after", 1, 0))

    await bridge.stop()
    assert [m["text"] for m in db.companion_load_history(bridge.get_public_key(), 0, 10)] == [
        "before",
        "after",
    ]
