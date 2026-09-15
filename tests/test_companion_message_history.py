"""History remains independently readable after frame delivery and queue pressure."""

import sqlite3
import time
from types import SimpleNamespace

import cherrypy
import pytest
from openhop_core.companion import CompanionBridge
from openhop_core.companion.constants import CMD_SYNC_NEXT_MESSAGE, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.models import ChannelDataEvent, ChannelMessageEvent, MessageEvent
from openhop_core.protocol import LocalIdentity

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.companion_endpoints import CompanionAPIEndpoints

_HASH = "0x01"
_PUBLIC_KEY = b"\x01" + b"\x11" * 31


@pytest.fixture
def db(tmp_path):
    return SQLiteHandler(tmp_path)


def _push(db, text, cap=100, **fields):
    return db.companion_push_message(
        _HASH,
        {"text": text, "packet_hash": text, "companion_public_key": _PUBLIC_KEY, **fields},
        cap,
    )


def _texts(rows):
    return [row["text"] for row in rows]


def _endpoints(db, bridge=None):
    bridge = bridge or SimpleNamespace(get_public_key=lambda: _PUBLIC_KEY)
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._get_bridge = lambda **kw: bridge
    ep._sse_callbacks = []
    ep.daemon_instance = SimpleNamespace(
        companion_bridges={bridge.get_public_key()[0]: bridge},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=db)),
    )
    ep.broadcasts = []
    ep._broadcast_sse = ep.broadcasts.append
    return ep


def _messages(ep, **kwargs):
    return CompanionAPIEndpoints.messages.__wrapped__(ep, **kwargs)["data"]


def _frame_server(db, bridge):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = db
    fs.companion_hash = f"0x{bridge.get_public_key()[0]:02x}"
    fs.bridge = bridge
    fs._app_target_ver = 3
    fs._client_writer = object()
    fs._cmd_handlers = {CMD_SYNC_NEXT_MESSAGE: fs._cmd_sync_next_message}
    fs.frames = []
    fs._write_frame = lambda data: fs.frames.append(bytes(data))
    fs._enqueue_frame = fs.frames.append
    return fs


@pytest.mark.asyncio
async def test_frame_delivery_preserves_api_history_and_wakes_clients(db):
    async def inject(packet, **kwargs):
        return True

    bridge = CompanionBridge(LocalIdentity(), inject)
    ep = _endpoints(db, bridge)
    ep._ensure_callbacks()
    fs = _frame_server(db, bridge)
    bridge.on_message_event(fs._on_message_event)
    event = MessageEvent(
        b"\x11" * 32, "hello", 1, 0, sender_prefix=b"\xaa\xbb\xcc\xdd", packet_hash="p1"
    )
    await bridge._fire_callbacks("message_event", event)
    assert [b["event"] for b in ep.broadcasts] == ["message_received"]
    page = _messages(ep)
    assert [(m["id"], m["text"]) for m in page] == [(1, "hello")]
    assert page[0]["sender_key"] == "11" * 32
    assert page[0]["sender_prefix"] == "aabbccdd"
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] != RESP_CODE_NO_MORE_MESSAGES
    assert db.companion_count_messages(fs.companion_hash) == 0
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] == RESP_CODE_NO_MORE_MESSAGES
    assert _messages(ep) == page
    assert _messages(ep, since=page[-1]["id"]) == []


def test_clients_replay_independent_pages_after_frame_consumption(db):
    for n in range(8):
        assert _push(db, str(n), is_channel=True, channel_idx=2)
    while db.companion_pop_message(_HASH):
        pass
    ep = _endpoints(db)
    rows = _messages(ep)
    assert _texts(rows) == [str(n) for n in range(8)]
    assert rows[0]["is_channel"] is True and rows[0]["channel_idx"] == 2
    for start in (0, rows[2]["id"], rows[5]["id"]):
        cursor, seen = start, []
        while page := _messages(ep, since=cursor, limit=2):
            assert page == _messages(ep, since=cursor, limit=2)
            seen.extend(page)
            cursor = page[-1]["id"]
        assert seen == [row for row in rows if row["id"] > start]


@pytest.mark.parametrize(
    "params",
    # A cursor past SQLite's 64-bit range is a bad request, not a storage
    # failure: without the upper bound it reached the query and surfaced as 503.
    [{"since": "abc"}, {"since": -1}, {"limit": 0}, {"since": str(2**63)}],
)
def test_invalid_cursor_returns_400(db, params):
    with pytest.raises(cherrypy.HTTPError) as error:
        _messages(_endpoints(db), **params)
    assert error.value.status == 400


def test_api_caps_pages_and_reports_storage_failure(db, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "companion_load_history", lambda *args: calls.append(args))
    with pytest.raises(cherrypy.HTTPError) as error:
        _messages(_endpoints(db), limit=501)
    assert error.value.status == 503
    assert calls == [(_HASH, _PUBLIC_KEY, 0, 500)]


def test_replacement_identity_cannot_read_or_deduplicate_prior_history(db, tmp_path):
    replacement_key = b"\x01" + b"\x22" * 31
    for key in (None, _PUBLIC_KEY, replacement_key):
        assert _push(db, "same packet", companion_public_key=key)
        assert not _push(db, "same packet", companion_public_key=key)
    assert db.companion_push_message("0x02", {"text": "other prefix"})
    while db.companion_pop_message(_HASH):
        pass
    original = _messages(_endpoints(db))
    replacement = _messages(_endpoints(db, SimpleNamespace(get_public_key=lambda: replacement_key)))
    assert len(original) == len(replacement) == 1
    assert original[0]["id"] != replacement[0]["id"]
    assert db.companion_load_history(_HASH, None) == []
    reopened = SQLiteHandler(tmp_path)
    assert not _push(reopened, "same packet", companion_public_key=replacement_key)
    assert _messages(_endpoints(reopened)) == original
    assert reopened.companion_count_messages(_HASH) == 0


@pytest.mark.parametrize(
    ("cap", "channel", "queued", "third_is_queued"),
    [
        # A queue full of direct messages has nothing it may evict, so the
        # arrival is kept as history and the frame queue refuses it — and says
        # so, because the caller's in-memory copy is then the only one that can
        # still be delivered.
        (2, False, ["0", "1"], False),
        (2, True, ["0", "2"], True),
        (0, False, [], True),
    ],
)
def test_queue_pressure_retains_all_history(db, cap, channel, queued, third_is_queued):
    assert _push(db, "0", cap)
    assert _push(db, "1", cap, is_channel=channel)
    assert _push(db, "2", cap) is third_is_queued
    assert _texts(db.companion_load_messages(_HASH)) == queued
    assert _texts(db.companion_load_history(_HASH, _PUBLIC_KEY)) == ["0", "1", "2"]
    assert not _push(db, "2", 100)
    assert db.companion_count_messages(_HASH) == len(queued)
    if queued:
        assert db.companion_pop_message(_HASH)["text"] == "0"
        assert _push(db, "3", cap)
        assert _texts(db.companion_load_messages(_HASH)) == [queued[1], "3"]


@pytest.mark.asyncio
async def test_full_direct_queue_keeps_the_in_memory_copy(db):
    """A queue with no room must not release the only deliverable copy.

    History still gains the row, but the frame queue refused it, so the
    bridge's in-memory entry is what has to reach the client — dropping it
    loses the message on the primary delivery path.
    """
    removed = []
    bridge = SimpleNamespace(
        message_queue=SimpleNamespace(max_size=1, remove=removed.append),
        get_public_key=lambda: _PUBLIC_KEY,
    )
    fs = _frame_server(db, bridge)
    assert _push(db, "occupant", 1)
    entry = object()
    await fs._persist_companion_message(
        {"text": "arrival", "packet_hash": "arrival", "is_channel": False}, entry
    )
    assert _texts(db.companion_load_history(_HASH, _PUBLIC_KEY)) == ["occupant", "arrival"]
    assert db.companion_count_messages(_HASH) == 1
    assert removed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dm", "channel", "data"])
async def test_rejected_memory_events_persist_with_zero_queue(db, kind):
    bridge = SimpleNamespace(
        message_queue=SimpleNamespace(max_size=0), get_public_key=lambda: _PUBLIC_KEY
    )
    fs = _frame_server(db, bridge)
    fs._client_writer = None
    events = {
        "dm": (fs._on_message_event, MessageEvent(b"a" * 32, "dm", 7, 0, queued=False)),
        "channel": (
            fs._on_channel_message_event,
            ChannelMessageEvent("Public", "Peer", "ch", 8, queued=False),
        ),
        "data": (fs._on_channel_data_event, ChannelDataEvent(2, 0, 1, b"payload", queued=False)),
    }
    callback, event = events[kind]
    await callback(event)
    rows = _messages(_endpoints(db))
    assert len(rows) == 1
    assert db.companion_count_messages(_HASH) == 0
    assert db.companion_pop_message(_HASH) is None
    assert fs.frames
    if kind == "data":
        assert rows[0]["channel_data_payload"] == b"payload".hex()
    else:
        assert rows[0]["text"] == event.text


def test_retention_uses_received_time_and_preserves_pending_rows(db, tmp_path):
    for text in ("consumed", "pending"):
        assert _push(db, text, timestamp=1)
    assert db.companion_pop_message(_HASH)["text"] == "consumed"
    for text in ("history only", "recent"):
        assert _push(db, text, cap=0, timestamp=1)
    with db._connect() as conn:
        conn.execute(
            "UPDATE companion_messages SET created_at = ? WHERE text != 'recent'",
            (time.time() - 40 * 86400,),
        )
    reopened = SQLiteHandler(tmp_path)
    assert _texts(reopened.companion_load_messages(_HASH)) == ["pending"]
    assert len(reopened.companion_load_history(_HASH, _PUBLIC_KEY)) == 4
    reopened.cleanup_old_data(companion_events_days=31)
    assert _texts(reopened.companion_load_history(_HASH, _PUBLIC_KEY)) == ["pending", "recent"]


@pytest.mark.parametrize("existing_columns", [False, True])
def test_migration_preserves_ids_queue_and_unknown_ownership(db, tmp_path, existing_columns):
    assert _push(db, "first", companion_public_key=None)
    assert _push(db, "second", companion_public_key=None)
    if existing_columns:
        assert db.companion_pop_message(_HASH)["text"] == "first"
    with db._connect() as conn:
        if not existing_columns:
            # Restore the upstream schema, including its original dedup index.
            conn.execute("DROP INDEX idx_companion_messages_pending")
            conn.execute("DROP INDEX idx_companion_messages_dedup")
            conn.execute("ALTER TABLE companion_messages DROP COLUMN pending_delivery")
            conn.execute("ALTER TABLE companion_messages DROP COLUMN companion_public_key")
            conn.execute(
                "CREATE UNIQUE INDEX idx_companion_messages_dedup "
                "ON companion_messages(companion_hash, packet_hash) WHERE packet_hash IS NOT NULL"
            )
        conn.execute(
            "DELETE FROM migrations WHERE migration_name = 'retain_companion_message_history'"
        )
    upgraded = SQLiteHandler(tmp_path)
    assert _messages(_endpoints(upgraded)) == []
    assert [row[0] for row in upgraded._connect().execute("SELECT id FROM companion_messages")] == [
        1,
        2,
    ]
    expected = ["second"] if existing_columns else ["first", "second"]
    assert _texts(upgraded.companion_load_messages(_HASH)) == expected
    assert upgraded.companion_pop_message(_HASH)["text"] == expected[0]
    assert not _push(upgraded, "first", companion_public_key=None)


def test_pop_survives_competing_write_between_select_and_update(db):
    assert _push(db, "waiting")
    assert db.companion_push_message("0x02", {"text": "unrelated"})
    conn = db._connect()
    competitor = sqlite3.connect(db.sqlite_path, timeout=0)
    attempted, errors = [], []

    def interleave(sql):
        if not attempted and sql.startswith("UPDATE companion_messages SET pending_delivery = 0"):
            attempted.append(True)
            try:
                with competitor:
                    competitor.execute(
                        "UPDATE companion_messages SET timestamp = 1 WHERE companion_hash = ?",
                        ("0x02",),
                    )
            except sqlite3.OperationalError as exc:
                errors.append(exc)

    conn.set_trace_callback(interleave)
    try:
        message = db.companion_pop_message(_HASH)
    finally:
        conn.set_trace_callback(None)
        competitor.close()
    assert attempted
    # `all` over an empty list is vacuously true: without this the test passed
    # with the BEGIN IMMEDIATE it exists to pin removed entirely.
    assert errors
    assert all("locked" in str(exc) for exc in errors)
    assert message is not None and message["text"] == "waiting"
    assert db.companion_count_messages(_HASH) == 0
    assert db.companion_push_message("0x02", {"text": "after pop"})


def test_pending_queue_reads_use_the_partial_index(db):
    """Pin what the reads execute, not a copy of it.

    Retained history shares this table with the frame queue, so a pending read
    that misses idx_companion_messages_pending walks a month of delivered rows
    on every sync. Asserting the SQL as written — inline or hoisted into a
    constant — does not catch that: a call site can go back to the slower form
    with the whole suite still green. Trace the connection and explain what ran.
    """
    assert _push(db, "queued")
    conn = db._connect()
    traced = []
    conn.set_trace_callback(traced.append)
    try:
        db.companion_count_messages(_HASH)
        db.companion_load_messages(_HASH)
        db.companion_pop_message(_HASH)
    finally:
        conn.set_trace_callback(None)

    reads = [sql for sql in traced if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 3, reads
    for sql in reads:
        # The trace expands bound parameters, so this explains the real statement.
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        assert any("idx_companion_messages_pending" in row[3] for row in plan), sql
