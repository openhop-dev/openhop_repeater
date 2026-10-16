"""A companion that opts in keeps what it receives readable after a frame sync."""

import time
from types import SimpleNamespace

import cherrypy
import pytest
from openhop_core import LocalIdentity
from openhop_core.companion.constants import CMD_SYNC_NEXT_MESSAGE, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.models import ChannelDataEvent, ChannelMessageEvent, MessageEvent

from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.companion_endpoints import CompanionAPIEndpoints

_HASH = "0x01"
_PUBLIC_KEY = b"\x01" + b"\x11" * 31


@pytest.fixture
def db(tmp_path):
    return SQLiteHandler(tmp_path)


async def _inject(packet, **kwargs):
    return True


def _bridge(db, **kwargs):
    kwargs.setdefault("message_history", True)
    return RepeaterCompanionBridge(
        LocalIdentity(), _inject, sqlite_handler=db, companion_hash=_HASH, **kwargs
    )


def _endpoints(db, bridge):
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._get_bridge = lambda **kw: bridge
    ep.daemon_instance = SimpleNamespace(
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=db))
    )
    return ep


def _messages(ep, **kwargs):
    return CompanionAPIEndpoints.messages.__wrapped__(ep, **kwargs)["data"]


def _frame_server(db, bridge):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = db
    fs.companion_hash = _HASH
    fs.bridge = bridge
    fs._app_target_ver = 3
    fs._cmd_handlers = {CMD_SYNC_NEXT_MESSAGE: fs._cmd_sync_next_message}
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

    (row,) = _messages(_endpoints(db, bridge))
    assert {key: row[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_history_is_off_by_default(db):
    bridge = _bridge(db, message_history=False)
    await bridge._fire_callbacks("message_event", MessageEvent(b"a" * 32, "x", 1, 0))

    assert db.companion_load_history(bridge.get_public_key(), 0, 10) == []
    with pytest.raises(cherrypy.HTTPError) as error:
        _messages(_endpoints(db, bridge))
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


def test_retention_expires_by_receipt_time_and_never_reissues_an_id(db):
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
