import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.handler_helpers.room_server import (
    INACTIVE_CLIENT_TIMEOUT,
    MAX_POST_TEXT_LEN,
    MAX_UNSYNCED_POSTS,
    RETRY_BACKOFF_SCHEDULE,
    TXT_TYPE_PLAIN,
    TXT_TYPE_SIGNED_PLAIN,
    GlobalRateLimiter,
    RoomServer,
    _truncate_utf8,
)


class _FakeIdentity:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


class _FakeClient:
    def __init__(self, pubkey: bytes, shared_secret=b"s" * 32, out_path=b"", out_path_len=-1):
        self.id = _FakeIdentity(pubkey)
        self.shared_secret = shared_secret
        self.out_path = bytearray(out_path)
        self.out_path_len = out_path_len
        self.sync_since = 0
        # Logged in: the sync loop skips entries that have not been active.
        self.last_activity = int(time.time())
        self.permissions = 0

    def is_admin(self):
        return self.permissions & 3 == 3


class _FakeACL:
    def __init__(self, clients=None):
        self._clients = list(clients or [])
        self.remove_client = MagicMock(return_value=True)

    def get_all_clients(self):
        return list(self._clients)

    def get_client(self, pub_key):
        return next((c for c in self._clients if c.id.get_public_key() == pub_key), None)


class _FakeDB:
    def __init__(self):
        self.insert_room_message = MagicMock(return_value=1)
        self.upsert_client_sync = MagicMock()
        self.get_client_sync = MagicMock(return_value=None)
        self.get_unsynced_count = MagicMock(return_value=0)
        self.get_all_room_clients = MagicMock(return_value=[])
        self.get_unsynced_messages = MagicMock(return_value=[])
        self.cleanup_old_messages = MagicMock(return_value=0)
        self.get_room_messages = MagicMock(return_value=[])


def _make_room_server(db=None, acl=None, injector=None, max_posts=8):
    return RoomServer(
        room_hash=0x34,
        room_name="room-alpha",
        local_identity=_FakeIdentity(b"R" * 32),
        sqlite_handler=db or _FakeDB(),
        packet_injector=injector or AsyncMock(return_value=True),
        acl=acl or _FakeACL(),
        max_posts=max_posts,
    )


@pytest.mark.asyncio
async def test_room_server_add_post_stores_message_and_updates_sync_state():
    db = _FakeDB()
    rs = _make_room_server(db=db, acl=_FakeACL([_FakeClient(b"C" * 32)]))

    ok = await rs.add_post(
        client_pubkey=b"A" * 32,
        message_text="hello room",
        sender_timestamp=111,
        txt_type=TXT_TYPE_PLAIN,
    )

    assert ok is True
    db.insert_room_message.assert_called_once()
    db.upsert_client_sync.assert_called_once()
    kwargs = db.upsert_client_sync.call_args.kwargs
    assert kwargs["room_hash"] == "0x34"
    assert kwargs["client_pubkey"] == (b"A" * 32).hex()


@pytest.mark.asyncio
async def test_room_server_add_post_does_not_advance_author_sync_watermark():
    """Posting must not move the author's sync_since watermark: it advances only
    when a push is ACKed. Self-echo is prevented by the author exclusion in the
    unsynced-message query, so an on-post advance would only skip OTHER authors'
    older, still-unsynced messages for this client. Activity is still refreshed."""
    db = _FakeDB()
    rs = _make_room_server(db=db, acl=_FakeACL([_FakeClient(b"C" * 32)]))

    ok = await rs.add_post(
        client_pubkey=b"A" * 32,
        message_text="newer post",
        sender_timestamp=222,
        txt_type=TXT_TYPE_PLAIN,
    )

    assert ok is True
    kwargs = db.upsert_client_sync.call_args.kwargs
    assert "sync_since" not in kwargs
    assert kwargs["last_activity"] > 0


@pytest.mark.asyncio
async def test_room_server_add_post_truncates_and_rate_limits_client():
    db = _FakeDB()
    rs = _make_room_server(db=db)
    client_key = b"B" * 32

    long_msg = "x" * 500
    first_ok = await rs.add_post(client_key, long_msg, sender_timestamp=5)
    assert first_ok is True
    args = db.insert_room_message.call_args.kwargs
    assert len(args["message_text"].encode("utf-8")) == MAX_POST_TEXT_LEN

    # Force client to appear at post-per-minute limit.
    rs.client_post_times[client_key.hex()] = [time.time() - 1] * 10
    second_ok = await rs.add_post(client_key, "blocked", sender_timestamp=6)
    assert second_ok is False


@pytest.mark.asyncio
async def test_room_server_add_post_ascii_over_limit_stores_exactly_max_bytes():
    db = _FakeDB()
    rs = _make_room_server(db=db)

    long_msg = "a" * 200  # ASCII: 1 byte per char, well over MAX_POST_TEXT_LEN
    ok = await rs.add_post(b"G" * 32, long_msg, sender_timestamp=1)
    assert ok is True

    stored = db.insert_room_message.call_args.kwargs["message_text"]
    assert len(stored.encode("utf-8")) == MAX_POST_TEXT_LEN
    assert stored == "a" * MAX_POST_TEXT_LEN


@pytest.mark.asyncio
async def test_room_server_add_post_multibyte_utf8_truncates_on_codepoint_boundary():
    db = _FakeDB()
    rs = _make_room_server(db=db)

    # Each emoji is 4 bytes in UTF-8; padding forces the cut to land mid-emoji
    # if truncation were byte-naive instead of codepoint-aware.
    padding = "a" * (MAX_POST_TEXT_LEN - 2)
    msg = padding + "\U0001f600\U0001f600\U0001f600"  # grinning face emoji x3
    assert len(msg.encode("utf-8")) > MAX_POST_TEXT_LEN

    ok = await rs.add_post(b"H" * 32, msg, sender_timestamp=2)
    assert ok is True

    stored = db.insert_room_message.call_args.kwargs["message_text"]
    encoded = stored.encode("utf-8")
    assert len(encoded) <= MAX_POST_TEXT_LEN
    # Must decode cleanly (no partial multi-byte sequence) and round-trip.
    assert encoded.decode("utf-8") == stored


@pytest.mark.asyncio
async def test_room_server_add_post_exact_limit_text_untouched():
    db = _FakeDB()
    rs = _make_room_server(db=db)

    msg = "y" * MAX_POST_TEXT_LEN
    ok = await rs.add_post(b"I" * 32, msg, sender_timestamp=3)
    assert ok is True

    stored = db.insert_room_message.call_args.kwargs["message_text"]
    assert stored == msg
    assert len(stored.encode("utf-8")) == MAX_POST_TEXT_LEN


def test_truncate_utf8_helper_boundary_cases():
    # Under the limit: untouched.
    assert _truncate_utf8("short", 151) == "short"

    # Exactly at the limit: untouched.
    exact = "z" * 151
    assert _truncate_utf8(exact, 151) == exact

    # Multi-byte straddling the boundary: cuts cleanly, decodes, stays <= limit.
    text = ("b" * 149) + "\U0001f600\U0001f600"  # 149 + 4 + 4 = 157 bytes
    result = _truncate_utf8(text, 151)
    encoded = result.encode("utf-8")
    assert len(encoded) <= 151
    assert encoded.decode("utf-8") == result


@pytest.mark.asyncio
async def test_room_server_add_post_returns_false_on_db_insert_failure():
    db = _FakeDB()
    db.insert_room_message.return_value = 0
    rs = _make_room_server(db=db)

    ok = await rs.add_post(b"D" * 32, "msg", sender_timestamp=9)
    assert ok is False


@pytest.mark.asyncio
async def test_room_server_send_advert_callback_returns_false_on_inject_failure():
    config = {
        "identities": {
            "room_servers": [
                {
                    "name": "room-alpha",
                    "settings": {"node_name": "Room Alpha", "latitude": 1.0, "longitude": 2.0},
                }
            ]
        }
    }

    injector = AsyncMock(return_value=False)
    rs = RoomServer(
        room_hash=0x34,
        room_name="room-alpha",
        local_identity=_FakeIdentity(b"R" * 32),
        sqlite_handler=_FakeDB(),
        packet_injector=injector,
        acl=_FakeACL(),
        config_path="/tmp/room.yaml",
        config=config,
        config_manager=SimpleNamespace(),
    )

    packet = SimpleNamespace()
    with patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet):
        ok = await rs.cli.send_advert_callback()

    assert ok is False
    injector.assert_awaited_once()


@pytest.mark.asyncio
async def test_room_server_cli_advert_uses_node_name():
    """CLI advert must advertise settings.node_name, matching the scheduler.

    The CLI path read only settings.room_name while the daemon scheduler and
    the HTTP endpoint read settings.node_name -- so a single `advert` from
    the room CLI renamed the room on the mesh to the identity-name fallback
    when the two keys differed.
    """
    config = {
        "identities": {
            "room_servers": [
                {
                    "name": "room-alpha",
                    "settings": {"node_name": "Room Alpha", "latitude": 1.0, "longitude": 2.0},
                }
            ]
        }
    }

    injector = AsyncMock(return_value=True)
    rs = RoomServer(
        room_hash=0x34,
        room_name="room-alpha",
        local_identity=_FakeIdentity(b"R" * 32),
        sqlite_handler=_FakeDB(),
        packet_injector=injector,
        acl=_FakeACL(),
        config_path="/tmp/room.yaml",
        config=config,
        config_manager=SimpleNamespace(),
    )

    packet = SimpleNamespace()
    with patch(
        "openhop_core.protocol.PacketBuilder.create_advert", return_value=packet
    ) as create_advert:
        ok = await rs.cli.send_advert_callback()

    assert ok is True
    assert create_advert.call_args.kwargs.get("name") == "Room Alpha"


def test_room_server_init_caps_max_posts_to_hard_limit():
    rs = _make_room_server(max_posts=MAX_UNSYNCED_POSTS + 50)
    assert rs.max_posts == MAX_UNSYNCED_POSTS


@pytest.mark.asyncio
async def test_room_server_push_post_to_client_success_direct_route_sets_path_and_ack():
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32, out_path=b"\xaa\xbb", out_path_len=2)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 1234.5,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ),
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    assert bytes(packet.path) == b"\xaa\xbb"
    assert packet.path_len == 2
    injector.assert_awaited_once_with(
        packet,
        wait_for_ack=True,
        expected_crc=67305985,
        ack_timeout_s=10.0,
    )
    rs._handle_ack_received.assert_awaited_once_with(
        client.id.get_public_key(), post["post_timestamp"]
    )
    rs.global_limiter.release.assert_called_once()


@pytest.mark.asyncio
async def test_room_server_push_post_to_client_clamps_oversized_legacy_stored_text():
    """A post stored before the write-time limit was enforced (or otherwise
    over MAX_POST_TEXT_LEN bytes) must still be clamped at push time so the
    outgoing frame's text portion never exceeds the firmware's budget."""
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32, out_path=b"\xaa\xbb", out_path_len=2)
    oversized_text = "q" * (MAX_POST_TEXT_LEN + 50)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": oversized_text,
        "post_timestamp": 1234.5,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ) as create_datagram,
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    plaintext = create_datagram.call_args.kwargs["plaintext"]
    # Prefix is timestamp(4) + flags(1) + author_prefix(4) = 9 bytes.
    text_portion = plaintext[9:]
    assert len(text_portion) == MAX_POST_TEXT_LEN
    assert text_portion.decode("utf-8") == "q" * MAX_POST_TEXT_LEN


@pytest.mark.asyncio
async def test_room_server_push_ends_the_text_at_an_embedded_nul():
    """[fails pre-fix] A stored post with an interior NUL is cut where firmware cuts it.

    pushPostToClient sizes the body with `strlen(post.text)`, for the payload
    and the expected ACK alike. The web API will store "a\0b", so pushing it
    whole sends text the receiver cannot see -- it stops at the NUL -- and,
    worse, hashes the pending ACK over a span the receiver never reproduces.
    The push would then never be acknowledged and would retry into the
    failure backoff.
    """
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32, out_path=b"\xaa\xbb", out_path_len=2)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "a\x00b",
        "post_timestamp": 1234.5,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ) as create_datagram,
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    # Prefix is timestamp(4) + flags(1) + author_prefix(4) = 9 bytes.
    assert create_datagram.call_args.kwargs["plaintext"][9:] == b"a"


@pytest.mark.asyncio
async def test_room_server_push_expected_ack_matches_firmware_signed_ack():
    """The pending ACK CRC must match what a signed-plain receiver sends back.

    Firmware clients (BaseChatMesh::onPeerDataRecv) and openhop_core's text
    handler ack TXT_TYPE_SIGNED_PLAIN with
    ``sha256(decrypted[0 : 9 + strlen(text)] || client_pubkey)[:4]``.
    Regression test for the room server expecting a plain-DM style hash
    (timestamp + attempt + text) instead: every push timed out and posts were
    re-pushed forever (issue #286).
    """
    from openhop_core import LocalIdentity
    from openhop_core.protocol import CryptoUtils, Identity

    room_id = LocalIdentity()
    client_id = LocalIdentity()
    client_pk = client_id.get_public_key()
    shared = Identity(client_pk).calc_shared_secret(room_id.get_private_key())

    db = _FakeDB()
    sent = []
    injector_kwargs = []

    async def injector(packet, wait_for_ack=False, **kwargs):
        sent.append(packet)
        injector_kwargs.append(kwargs)
        return False  # no ACK: leaves the pending upsert as the only db write

    rs = RoomServer(
        room_hash=room_id.get_public_key()[0],
        room_name="parity",
        local_identity=room_id,
        sqlite_handler=db,
        packet_injector=injector,
        acl=_FakeACL(),
    )
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())

    author = LocalIdentity().get_public_key()
    client = _FakeClient(pubkey=client_pk, shared_secret=shared, out_path=b"\x01", out_path_len=1)
    post = {
        "author_pubkey": author.hex(),
        "message_text": "parity check",
        "post_timestamp": 1234.5,
    }

    ok = await rs.push_post_to_client(client, post)
    assert ok is False
    assert len(sent) == 1

    upserts = [
        c.kwargs for c in db.upsert_client_sync.call_args_list if "pending_ack_crc" in c.kwargs
    ]
    assert len(upserts) == 1
    expected_ack_crc = upserts[0]["pending_ack_crc"]

    # The injector must be told the crypto CRC (and the computed timeout) so
    # dispatcher.wait_for_ack matches the client's actual ACK.
    assert injector_kwargs[0]["expected_crc"] == expected_ack_crc
    assert injector_kwargs[0]["ack_timeout_s"] > 0

    # Decrypt the pushed datagram and verify the signed-plain layout.
    pkt = sent[0]
    encrypted = bytes(pkt.payload[2 : pkt.payload_len])
    decrypted = CryptoUtils.mac_then_decrypt(shared[:16], shared, encrypted)
    assert decrypted is not None
    flags = decrypted[4]
    assert (flags >> 2) & 0x3F == TXT_TYPE_SIGNED_PLAIN
    assert bytes(decrypted[5:9]) == author[:4]
    body = bytes(decrypted[9:])
    nul = body.find(b"\x00")
    text_len = nul if nul >= 0 else len(body)
    assert body[:text_len] == b"parity check"

    # Recompute the ACK exactly as the receiving client does.
    client_ack_crc = int.from_bytes(
        CryptoUtils.sha256(bytes(decrypted[: 9 + text_len]) + client_pk)[:4], "little"
    )
    assert client_ack_crc == expected_ack_crc


@pytest.mark.asyncio
async def test_push_serializes_stored_post_timestamp_not_walltime():
    """The pushed frame's timestamp must be the post's own stored timestamp,
    not the delivery wall-clock time (MyMesh.cpp:56 serializes the stored
    post's timestamp, not `now`)."""
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 1000.0,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ) as create_datagram,
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    plaintext = create_datagram.call_args.kwargs["plaintext"]
    assert int.from_bytes(plaintext[0:4], "little") == 1000


@pytest.mark.asyncio
async def test_push_attempt_bits_randomized_across_retries():
    """Each push must OR a fresh random byte's low 2 bits into flags so a
    retried post gets a different packet hash (and ACK), per MyMesh.cpp:59-60
    ("so packet hash (and ACK) will be different")."""
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 1234.5,
    }

    plaintexts = []
    acks = []
    with patch("repeater.handler_helpers.room_server.secrets.randbits", side_effect=[1, 2]):
        for _ in range(2):
            packet = SimpleNamespace(path=bytearray(), path_len=0)
            with patch(
                "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
                return_value=packet,
            ) as create_datagram:
                ok = await rs.push_post_to_client(client, post)
            assert ok is True
            plaintexts.append(create_datagram.call_args.kwargs["plaintext"])
            acks.append(db.upsert_client_sync.call_args.kwargs["pending_ack_crc"])

    flags = [pt[4] for pt in plaintexts]
    assert (flags[0] >> 2) & 0x3F == TXT_TYPE_SIGNED_PLAIN
    assert (flags[1] >> 2) & 0x3F == TXT_TYPE_SIGNED_PLAIN
    assert flags[0] & 0x03 != flags[1] & 0x03
    assert plaintexts[0][0:4] == plaintexts[1][0:4]  # timestamp unchanged
    assert plaintexts[0][5:] == plaintexts[1][5:]  # author prefix + text unchanged
    assert acks[0] != acks[1]


@pytest.mark.asyncio
async def test_push_watermark_equals_serialized_timestamp():
    """The db sync watermark update must use the same timestamp value that
    was serialized into the pushed plaintext."""
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 555.0,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ) as create_datagram,
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    plaintext = create_datagram.call_args.kwargs["plaintext"]
    serialized_ts = int.from_bytes(plaintext[0:4], "little")
    watermark = db.upsert_client_sync.call_args.kwargs["push_post_timestamp"]
    assert int(watermark) == serialized_ts


@pytest.mark.asyncio
async def test_room_server_push_post_to_client_backoff_skip_and_timeout_path():
    db = _FakeDB()
    injector = AsyncMock(return_value=False)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_timeout = AsyncMock()

    client = _FakeClient(pubkey=b"G" * 32)
    post = {
        "author_pubkey": (b"H" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 88.0,
    }

    # In backoff window: skip send.
    db.get_client_sync.return_value = {"push_failures": 2, "updated_at": time.time()}
    skip_ok = await rs.push_post_to_client(client, post)
    assert skip_ok is False
    injector.assert_not_awaited()

    # Out of backoff and send fails -> timeout handler called.
    db.get_client_sync.return_value = {"push_failures": 1, "updated_at": time.time() - 9999}
    with (
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder._pack_timestamp_data",
            return_value=b"pk",
        ),
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=SimpleNamespace(path=bytearray(), path_len=0),
        ),
    ):
        fail_ok = await rs.push_post_to_client(client, post)

    assert fail_ok is False
    rs._handle_ack_timeout.assert_awaited_once_with(client.id.get_public_key())


@pytest.mark.asyncio
async def test_room_server_ack_helpers_and_unsynced_count_fallbacks():
    db = _FakeDB()
    rs = _make_room_server(db=db)

    await rs._handle_ack_received(b"I" * 32, post_timestamp=123.0)
    db.upsert_client_sync.assert_called()

    db.get_client_sync.return_value = {"push_failures": 2}
    await rs._handle_ack_timeout(b"I" * 32)
    # last call should have incremented failures and cleared pending ack
    timeout_kwargs = db.upsert_client_sync.call_args.kwargs
    assert timeout_kwargs["push_failures"] == 3
    assert timeout_kwargs["pending_ack_crc"] == 0

    db.get_client_sync.side_effect = RuntimeError("db down")
    assert rs.get_unsynced_count(b"I" * 32) == 0


@pytest.mark.asyncio
async def test_room_server_evict_failed_clients_and_check_ack_timeouts():
    db = _FakeDB()
    acl = _FakeACL([_FakeClient(b"J" * 32)])
    rs = _make_room_server(db=db, acl=acl)

    now = time.time()
    db.get_all_room_clients.return_value = [
        # Failed pushes alone do not evict: the client stays in backoff.
        {
            "client_pubkey": (b"J" * 32).hex(),
            "push_failures": 3,
            "last_activity": now,
            "pending_ack_crc": 0,
            "ack_timeout_time": 0,
        },
        # A few hours idle is normal for a quiet room (clients send no keep-alives).
        {
            "client_pubkey": (b"K" * 32).hex(),
            "push_failures": 0,
            "last_activity": now - 5 * 3600,
            "pending_ack_crc": 0,
            "ack_timeout_time": 0,
        },
        # Past the inactivity timeout: evicted.
        {
            "client_pubkey": (b"N" * 32).hex(),
            "push_failures": 0,
            "last_activity": now - INACTIVE_CLIENT_TIMEOUT - 60,
            "pending_ack_crc": 0,
            "ack_timeout_time": 0,
        },
    ]

    await rs._evict_failed_clients()
    db.upsert_client_sync.assert_called_once()
    assert db.upsert_client_sync.call_args.kwargs["client_pubkey"] == (b"N" * 32).hex()
    assert db.upsert_client_sync.call_args.kwargs["last_activity"] == 0
    acl.remove_client.assert_called_once_with(b"N" * 32)

    rs._handle_ack_timeout = AsyncMock()
    db.get_all_room_clients.return_value = [
        {
            "client_pubkey": (b"L" * 32).hex(),
            "pending_ack_crc": 123,
            "ack_timeout_time": now - 1,
        },
        {
            "client_pubkey": (b"M" * 32).hex(),
            "pending_ack_crc": 0,
            "ack_timeout_time": now - 1,
        },
    ]
    await rs._check_ack_timeouts()
    rs._handle_ack_timeout.assert_awaited_once_with(b"L" * 32)


def _push_patches(packet=None):
    return (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet or SimpleNamespace(path=bytearray(), path_len=0),
        ),
    )


def _post():
    return {
        "author_pubkey": (b"P" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 99.0,
    }


@pytest.mark.asyncio
async def test_push_attempt_does_not_refresh_client_activity():
    """Only contact (login, post, ACK) counts as activity. A push attempt that
    refreshed last_activity kept an unreachable client active forever."""
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    rs = _make_room_server(db=db, injector=AsyncMock(return_value=False))
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_timeout = AsyncMock()

    p1, p2 = _push_patches()
    with p1, p2:
        await rs.push_post_to_client(_FakeClient(b"Q" * 32), _post())

    pending_kwargs = db.upsert_client_sync.call_args_list[0].kwargs
    assert "pending_ack_crc" in pending_kwargs
    assert "last_activity" not in pending_kwargs


@pytest.mark.asyncio
async def test_failed_direct_push_forgets_path_so_retry_floods():
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    rs = _make_room_server(db=db, injector=AsyncMock(return_value=False))
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_timeout = AsyncMock()
    client = _FakeClient(b"S" * 32, out_path=b"\xaa\xbb", out_path_len=2)

    p1, p2 = _push_patches()
    with p1, p2 as create_datagram:
        assert await rs.push_post_to_client(client, _post()) is False
        assert create_datagram.call_args.kwargs["route_type"] == "direct"

    assert client.out_path_len == -1
    assert client.out_path == bytearray()

    # The retry goes out as a flood.
    p1, p2 = _push_patches()
    with p1, p2 as create_datagram:
        await rs.push_post_to_client(client, _post())
        assert create_datagram.call_args.kwargs["route_type"] == "flood"


@pytest.mark.asyncio
async def test_successful_direct_push_keeps_path():
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    rs = _make_room_server(db=db, injector=AsyncMock(return_value=True))
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()
    client = _FakeClient(b"T" * 32, out_path=b"\xaa\xbb", out_path_len=2)

    p1, p2 = _push_patches()
    with p1, p2:
        assert await rs.push_post_to_client(client, _post()) is True

    assert client.out_path_len == 2
    assert bytes(client.out_path) == b"\xaa\xbb"


@pytest.mark.asyncio
async def test_client_past_max_failures_is_retried_after_last_backoff_step():
    """A client is never stopped for good: past MAX_PUSH_FAILURES it is retried
    at the last backoff interval, so it catches up once back in range."""
    db = _FakeDB()
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()
    client = _FakeClient(b"U" * 32)
    last_step = RETRY_BACKOFF_SCHEDULE[-1]

    # Still inside the last backoff step: no send.
    db.get_client_sync.return_value = {"push_failures": 7, "updated_at": time.time()}
    assert await rs.push_post_to_client(client, _post()) is False
    injector.assert_not_awaited()

    # Last step elapsed: retried.
    db.get_client_sync.return_value = {
        "push_failures": 7,
        "updated_at": time.time() - last_step - 1,
    }
    p1, p2 = _push_patches()
    with p1, p2:
        assert await rs.push_post_to_client(client, _post()) is True
    injector.assert_awaited_once()


def test_backoff_remaining_follows_schedule():
    now = time.time()
    assert RoomServer._backoff_remaining({"push_failures": 0, "updated_at": now}) == 0
    assert RoomServer._backoff_remaining({"push_failures": 1, "updated_at": now}) == pytest.approx(
        RETRY_BACKOFF_SCHEDULE[1], abs=1
    )
    assert RoomServer._backoff_remaining({"push_failures": 50, "updated_at": now}) == pytest.approx(
        RETRY_BACKOFF_SCHEDULE[-1], abs=1
    )
    assert RoomServer._backoff_remaining({"push_failures": 2, "updated_at": now - 9999}) == 0


@pytest.mark.asyncio
async def test_add_post_clears_author_push_failures():
    db = _FakeDB()
    rs = _make_room_server(db=db, acl=_FakeACL([_FakeClient(b"C" * 32)]))

    assert await rs.add_post(b"V" * 32, "back in range", sender_timestamp=1) is True

    assert db.upsert_client_sync.call_args.kwargs["push_failures"] == 0


@pytest.mark.asyncio
async def test_room_server_start_and_stop_are_idempotent():
    rs = _make_room_server()

    await rs.start()
    assert rs._running is True
    first_task = rs._sync_task

    # Second start should not replace task.
    await rs.start()
    assert rs._sync_task is first_task

    await rs.stop()
    assert rs._running is False

    # Stop again should be safe.
    await rs.stop()

    # Ensure task is cleaned up.
    if first_task:
        assert first_task.cancelled() or first_task.done()


def _push_post(author=b"F" * 32):
    return {
        "author_pubkey": author.hex(),
        "message_text": "payload",
        "post_timestamp": 1234.5,
    }


@pytest.mark.asyncio
async def test_global_rate_limiter_serializes_concurrent_room_pushes():
    """Two room loops sharing the limiter must not transmit concurrently, and
    the inter-message gap must be enforced between their sends.

    Regression: acquire() used to release the lock before returning, so
    both pushes ran their radio send with no lock held.
    """
    limiter = GlobalRateLimiter(min_gap_seconds=0.05)

    active = 0
    max_active = 0
    starts = []

    async def injector(packet, wait_for_ack=True, expected_crc=0, ack_timeout_s=0.0):
        nonlocal active, max_active
        starts.append(time.monotonic())
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)  # simulate airtime
        finally:
            active -= 1
        return True

    rooms = []
    for i in range(2):
        db = _FakeDB()
        db.get_client_sync.return_value = {"push_failures": 0}
        rs = _make_room_server(db=db, injector=injector)
        rs.global_limiter = limiter  # share one real limiter across both rooms
        rs._handle_ack_received = AsyncMock()
        rooms.append(rs)

    client = _FakeClient(pubkey=b"E" * 32)
    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ),
    ):
        results = await asyncio.gather(
            *(rs.push_post_to_client(client, _push_post()) for rs in rooms)
        )

    assert results == [True, True]
    assert max_active == 1  # never two transmissions in flight at once
    assert len(starts) == 2
    # The second send started at least min_gap after the first began.
    assert starts[1] - starts[0] >= 0.05
    assert not limiter.lock.locked()  # fully released at the end


@pytest.mark.asyncio
async def test_global_rate_limiter_releases_lock_on_send_exception():
    """If the radio send raises, the limiter lock must still be released so the
    next push is not deadlocked."""
    limiter = GlobalRateLimiter(min_gap_seconds=0.0)

    async def failing_injector(packet, **kwargs):
        raise RuntimeError("radio boom")

    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    rs = _make_room_server(db=db, injector=failing_injector)
    rs.global_limiter = limiter

    client = _FakeClient(pubkey=b"E" * 32)
    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ),
    ):
        ok = await rs.push_post_to_client(client, _push_post())

    assert ok is False
    assert not limiter.lock.locked()
    # The lock is reusable immediately afterwards.
    await limiter.acquire()
    limiter.release()


@pytest.mark.asyncio
async def test_post_timestamps_are_unique_whole_seconds_after_the_last_post():
    """Firmware getCurrentTimeUnique(): whole seconds, strictly increasing, and
    after the newest stored post (which may be a legacy fractional value)."""
    db = _FakeDB()
    db.get_room_messages.return_value = [{"post_timestamp": 2_000_000_000.75}]
    rs = _make_room_server(db=db)

    with patch("repeater.handler_helpers.room_server.time.time", return_value=1_000.5):
        for author in (b"W", b"X", b"Y"):
            assert await rs.add_post(author * 32, "hi", sender_timestamp=1) is True

    stamps = [c.kwargs["post_timestamp"] for c in db.insert_room_message.call_args_list]
    assert stamps == [2_000_000_001, 2_000_000_002, 2_000_000_003]
    assert all(isinstance(t, int) for t in stamps)
    db.get_room_messages.assert_called_once()


@pytest.mark.asyncio
async def test_post_timestamp_follows_the_clock_when_it_is_ahead():
    db = _FakeDB()
    db.get_room_messages.return_value = [{"post_timestamp": 500.2}]
    rs = _make_room_server(db=db)

    with patch("repeater.handler_helpers.room_server.time.time", return_value=9_000.9):
        assert await rs.add_post(b"Z" * 32, "hi", sender_timestamp=1) is True

    assert db.insert_room_message.call_args.kwargs["post_timestamp"] == 9_000
