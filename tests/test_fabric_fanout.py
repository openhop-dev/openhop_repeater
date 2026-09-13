"""Dual-radio Fabric fan-out: repeat on both sides of the bridge, and send
locally originated packets on every radio.

These drive the real openhop_core Dispatcher and FabricRadio over two fake
physical radios, so radio_id routing, the TX locks and the ACK waiters are the
production ones; only the hardware is faked.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_GRP_TXT,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_RESPONSE,
    PAYLOAD_TYPE_TXT_MSG,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)
from openhop_core.protocol.packet_filter import PacketFilter
from openhop_core.protocol.transport_keys import get_auto_key_for
from openhop_core.rf_fabric import FabricRadio

from repeater.engine import FanoutTxResult, RadioTxResult, RepeaterHandler
from repeater.packet_router import PacketRouter
from repeater.policy_engine import PolicyEngine

LOCAL_HASH = 0xAB


# ---------------------------------------------------------------------------
# Rig: two fake radios behind a real FabricRadio + Dispatcher + RepeaterHandler
# ---------------------------------------------------------------------------


class _Air:
    """Shared on-air log: every transmitted frame in order, plus overlap detection."""

    def __init__(self):
        self.frames = []
        self.overlapped = False
        self._keyed = 0

    @property
    def order(self):
        return [name for name, _ in self.frames]


class _FakeRadio:
    def __init__(self, name, air):
        self.name = name
        self.air = air
        self.rx_callback = None
        self.fail = False  # send() returns no confirmation
        self.airtime_s = 0.0
        self.is_connected = True
        self.is_degraded = False
        self.on_send = None
        self.sent_at = []

    def set_rx_callback(self, cb):
        self.rx_callback = cb

    async def send(self, data: bytes):
        if self.air._keyed:
            self.air.overlapped = True
        self.air._keyed += 1
        try:
            if self.airtime_s:
                await asyncio.sleep(self.airtime_s)
            if self.fail:
                return None
            self.sent_at.append(time.monotonic())
            self.air.frames.append((self.name, bytes(data)))
            if self.on_send is not None:
                await self.on_send(self.name, data)
            return {"ok": True}
        finally:
            self.air._keyed -= 1


def _config(fabric, repeater):
    return {
        "repeater": {
            "mode": "forward",
            "cache_ttl": 3600,
            "send_advert_interval_hours": 0,
            **repeater,
        },
        "mesh": {"unscoped_flood_allow": True, "loop_detect": "off"},
        "delays": {
            "tx_delay_factor": 1.0,
            "direct_tx_delay_factor": 0.5,
            "local_tx_link_wait_seconds": 0.2,
        },
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
        "radio": {
            "spreading_factor": 7,
            "bandwidth": 62500,
            "coding_rate": 5,
            "preamble_length": 17,
        },
        "fabric": {"default_radio": "local", "tx_mode": "bridge", **fabric},
    }


class _Rig:
    def __init__(self, fabric=None, **repeater):
        cfg = _config(fabric or {}, repeater)
        self.air = _Air()
        self.local = _FakeRadio("local", self.air)
        self.link = _FakeRadio("link", self.air)
        self.radio = FabricRadio(
            radios=[(self.local, "local"), (self.link, "link")],
            default_radio_id=cfg["fabric"]["default_radio"],
        )
        self.dispatcher = Dispatcher(radio=self.radio, packet_filter=PacketFilter())

        # Every Packet object handed to the dispatcher, with its target radio.
        self.sent_packets = []
        real_send = self.dispatcher.send_packet

        async def spy(packet, wait_for_ack=True, expected_crc=None, radio_id=None):
            self.sent_packets.append((radio_id, packet))
            return await real_send(
                packet, wait_for_ack=wait_for_ack, expected_crc=expected_crc, radio_id=radio_id
            )

        self.dispatcher.send_packet = spy

        with (
            patch("repeater.engine.StorageCollector"),
            patch("repeater.engine.RepeaterHandler._start_background_tasks"),
        ):
            self.handler = RepeaterHandler(
                cfg, self.dispatcher, LOCAL_HASH, local_hash_bytes=bytes([LOCAL_HASH])
            )
        # Deterministic, fast TX timing; delay randomisation is covered elsewhere.
        self.handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0

    @property
    def sent_radio_ids(self):
        return [radio_id for radio_id, _ in self.sent_packets]

    def records(self):
        return [c.args[0] for c in self.handler.storage.record_packet.call_args_list]


def _packet(route, payload_type, payload, path, rx=None):
    pkt = Packet()
    pkt.header = route | (payload_type << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    if rx is not None:
        pkt._rx_radio_id = rx
    return pkt


def _flood(payload=b"\x10\x20\x30\x40", path=b"", payload_type=PAYLOAD_TYPE_TXT_MSG, rx=None):
    return _packet(ROUTE_TYPE_FLOOD, payload_type, payload, path, rx)


def _direct(
    payload=b"\x10\x20\x30\x40",
    path=bytes([LOCAL_HASH, 0xCC, 0xDD]),
    payload_type=PAYLOAD_TYPE_TXT_MSG,
    rx=None,
    route=ROUTE_TYPE_DIRECT,
):
    return _packet(route, payload_type, payload, path, rx)


def _rx(radio_id):
    return {"rx_radio_id": radio_id, "snr": 5.0, "rssi": -80}


# ---------------------------------------------------------------------------
# RF relay fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ingress, expected",
    [("local", ["link", "local"]), ("link", ["local", "link"])],
)
async def test_rf_relay_repeats_on_both_radios_bridge_first(ingress, expected):
    rig = _Rig({"repeat_on_ingress": True})
    pkt = _flood(path=bytes([0x11, 0x22]), rx=ingress)

    assert await rig.handler(pkt, _rx(ingress)) is True

    assert rig.air.order == expected
    (_, first), (_, second) = rig.air.frames
    assert first == second  # one logical packet, identical on both radios
    # The path append ran once: our hash appears exactly once on each copy.
    assert [bytes(p.path) for _, p in rig.sent_packets] == [bytes([0x11, 0x22, LOCAL_HASH])] * 2

    # Each physical send has its own Packet, so its TX metadata is its own.
    (rid_a, pkt_a), (rid_b, pkt_b) = rig.sent_packets
    assert pkt_a is not pkt_b
    assert pkt_a._tx_metadata["radio_id"] == rid_a
    assert pkt_b._tx_metadata["radio_id"] == rid_b

    record = rig.records()[-1]
    assert record["rx_radio_id"] == ingress
    assert record["tx_radio_id"] == expected[0]
    assert record["tx_radio_ids"] == expected
    assert record["transmitted"] is True
    assert record["drop_reason"] is None

    # One logical RX and forward; two physical flood transmissions.
    assert rig.handler.rx_count == 1
    assert rig.handler.forwarded_count == 1
    assert rig.handler.sent_flood_count == 2
    assert rig.handler.dropped_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress, other", [("local", "link"), ("link", "local")])
async def test_bridge_without_repeat_on_ingress_is_unchanged(ingress, other):
    rig = _Rig()
    assert await rig.handler(_flood(rx=ingress), _rx(ingress)) is True
    assert rig.air.order == [other]
    assert rig.records()[-1]["tx_radio_ids"] == [other]
    assert rig.handler.sent_flood_count == 1


@pytest.mark.asyncio
async def test_flood_is_validated_and_marked_seen_once_for_both_radios():
    rig = _Rig({"repeat_on_ingress": True})
    with (
        patch.object(rig.handler, "flood_forward", wraps=rig.handler.flood_forward) as fwd,
        patch.object(rig.handler, "mark_seen", wraps=rig.handler.mark_seen) as seen,
    ):
        assert await rig.handler(_flood(rx="local"), _rx("local")) is True
    assert fwd.call_count == 1
    assert seen.call_count == 1
    assert len(rig.air.frames) == 2


@pytest.mark.asyncio
async def test_returned_copies_are_deduplicated_without_new_fanout():
    rig = _Rig({"repeat_on_ingress": True})
    payload = b"\x77\x66\x55"
    assert await rig.handler(_flood(payload=payload, path=b"\x11", rx="local"), _rx("local"))
    assert len(rig.air.frames) == 2

    # Neighbours on each side repeat our copy back to us.
    for ingress in ("link", "local"):
        echo = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH, 0x33]), rx=ingress)
        assert await rig.handler(echo, _rx(ingress)) is False
        assert rig.records()[-1]["drop_reason"] == "Duplicate"

    assert len(rig.air.frames) == 2  # no ping-pong between the domains
    assert rig.handler.forwarded_count == 1


@pytest.mark.asyncio
async def test_own_transmission_heard_on_the_other_radio_is_not_a_neighbour():
    """Same frequency: each radio hears the other send, bytes exactly as sent."""
    rig = _Rig({"repeat_on_ingress": True})
    payload = b"\x42\x43\x44"
    assert await rig.handler(_flood(payload=payload, path=b"\x11", rx="local"), _rx("local"))

    for ingress in ("link", "local"):
        echo = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH]), rx=ingress)
        assert await rig.handler(echo, _rx(ingress)) is False
    # A neighbour repeating our copy appends its own hash and is still a neighbour.
    relayed = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH, 0x33]), rx="link")
    assert await rig.handler(relayed, _rx("link")) is False

    links = rig.handler.neighbour_link_tracker.links
    assert set(links) == {"1:11", "1:33"}
    assert set(links["1:11"].radios) == {"local"}
    assert set(links["1:33"].radios) == {"link"}
    # The echoes are still stored as the receptions they were.
    assert [r["drop_reason"] for r in rig.records()[1:]] == ["Duplicate"] * 3


@pytest.mark.asyncio
async def test_a_copy_heard_on_the_radio_that_sent_it_is_not_our_echo():
    """A radio cannot hear its own transmission, so that copy came from a neighbour."""
    rig = _Rig()  # bridge only: heard on local, sent on link
    payload = b"\x51\x52\x53"
    assert await rig.handler(_flood(payload=payload, path=b"\x11", rx="local"), _rx("local"))
    assert rig.air.order == ["link"]

    collided = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH]), rx="link")
    assert await rig.handler(collided, _rx("link")) is False
    assert f"1:{LOCAL_HASH:02X}" in rig.handler.neighbour_link_tracker.links

    echo = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH]), rx="local")
    rig.handler.neighbour_link_tracker.links.clear()
    assert await rig.handler(echo, _rx("local")) is False
    assert f"1:{LOCAL_HASH:02X}" not in rig.handler.neighbour_link_tracker.links


@pytest.mark.asyncio
async def test_a_matching_copy_after_the_echo_window_is_a_neighbour():
    rig = _Rig({"repeat_on_ingress": True})
    rig.handler.OWN_ECHO_MARGIN_SECONDS = -1.0  # every finished send is already outside it
    payload = b"\x61\x62\x63"
    assert await rig.handler(_flood(payload=payload, path=b"\x11", rx="local"), _rx("local"))

    late = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH]), rx="link")
    assert await rig.handler(late, _rx("link")) is False

    assert f"1:{LOCAL_HASH:02X}" in rig.handler.neighbour_link_tracker.links
    assert not rig.handler._recent_own_tx  # expired sends are dropped


@pytest.mark.asyncio
async def test_a_send_refused_by_duty_cycle_leaves_no_echo_to_match():
    rig = _Rig({"repeat_on_ingress": True})
    rig.handler.airtime_mgr.can_transmit = MagicMock(return_value=(False, 5.0))
    payload = b"\x71\x72\x73"
    assert (
        await rig.handler(_flood(payload=payload, path=b"\x11", rx="local"), _rx("local")) is False
    )
    assert rig.air.frames == []

    collided = _flood(payload=payload, path=bytes([0x11, LOCAL_HASH]), rx="link")
    assert await rig.handler(collided, _rx("link")) is False

    assert not rig.handler._recent_own_tx
    assert f"1:{LOCAL_HASH:02X}" in rig.handler.neighbour_link_tracker.links


@pytest.mark.asyncio
async def test_a_failed_send_is_forgotten():
    rig = _Rig()
    rig.link.fail = True
    assert await rig.handler(_flood(path=b"\x11", rx="local"), _rx("local")) is False
    assert not rig.handler._recent_own_tx


@pytest.mark.asyncio
async def test_a_single_radio_node_keeps_no_echo_records():
    rig = _Rig()
    with patch.object(rig.handler, "_fabric_endpoints", return_value=(None, ["local"])):
        assert await rig.handler(_flood(path=b"\x11", rx="local"), _rx("local"))
    assert not rig.handler._recent_own_tx


@pytest.mark.asyncio
async def test_egress_follows_captured_ingress_not_the_latest_rx():
    rig = _Rig({"repeat_on_ingress": True})
    rig.handler._calculate_tx_delay = lambda packet, snr=0.0: 0.05
    first = _flood(payload=b"\xa1\xa2\xa3", rx="local")
    second = _flood(payload=b"\xb1\xb2\xb3", rx="link")

    task_first = asyncio.create_task(rig.handler(first, _rx("local")))
    await asyncio.sleep(0.01)
    # A later reception on the other radio moves the fabric's last-RX marker
    # while the first packet is still waiting out its TX delay.
    rig.radio.fabric._last_rx_radio_id = "link"
    task_second = asyncio.create_task(rig.handler(second, _rx("link")))
    await asyncio.gather(task_first, task_second)

    by_payload = {}
    for radio_id, pkt in rig.sent_packets:
        by_payload.setdefault(bytes(pkt.payload), []).append(radio_id)
    assert by_payload[b"\xa1\xa2\xa3"] == ["link", "local"]
    assert by_payload[b"\xb1\xb2\xb3"] == ["local", "link"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT])
async def test_direct_packet_removes_our_hop_once_for_both_radios(route):
    rig = _Rig({"repeat_on_ingress": True})
    pkt = _direct(path=bytes([LOCAL_HASH, 0xCC, 0xDD]), rx="link", route=route)
    if route == ROUTE_TYPE_TRANSPORT_DIRECT:
        pkt.transport_codes = [0x1234, 0x5678]

    assert await rig.handler(pkt, _rx("link")) is True

    assert rig.sent_radio_ids == ["local", "link"]
    assert [bytes(p.path) for _, p in rig.sent_packets] == [b"\xcc\xdd"] * 2
    assert rig.air.frames[0][1] == rig.air.frames[1][1]
    assert rig.handler.sent_direct_count == 2
    assert rig.handler.forwarded_count == 1


@pytest.mark.asyncio
async def test_multi_ack_redundancy_copies_share_the_egress_set():
    rig = _Rig({"repeat_on_ingress": True}, multi_acks=1)
    ack = _direct(
        payload=b"\x01\x02\x03\x04",
        path=bytes([LOCAL_HASH, 0xCC]),
        payload_type=PAYLOAD_TYPE_ACK,
        rx="local",
    )

    assert await rig.handler(ack, _rx("local")) is True

    for ptype in (PAYLOAD_TYPE_MULTIPART, PAYLOAD_TYPE_ACK):
        radios = [rid for rid, p in rig.sent_packets if p.get_payload_type() == ptype]
        assert radios == ["link", "local"], f"payload type {ptype}"
    assert len(rig.air.frames) == 4
    # The plain ACK and its redundancy copy each count as one logical forward.
    assert rig.handler.forwarded_count == 2


@pytest.mark.asyncio
async def test_policy_runs_once_per_rx_before_fanout():
    rig = _Rig({"repeat_on_ingress": True})
    rig.handler.policy_engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": "drop-link",
                    "if": {"all": [{"field": "rx_radio_id", "op": "eq", "value": "link"}]},
                    "then": "drop",
                }
            ],
        }
    )
    policy = rig.handler.policy_engine
    with patch.object(policy, "evaluate", wraps=policy.evaluate) as evaluate:
        assert await rig.handler(_flood(payload=b"\x01\x02", rx="link"), _rx("link")) is False
        assert rig.air.frames == []

        assert await rig.handler(_flood(payload=b"\x03\x04", rx="local"), _rx("local")) is True
        assert rig.air.order == ["link", "local"]

    assert evaluate.call_count == 2  # once per logical RX, never per egress


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link_ok, local_ok, expected",
    [
        (True, True, ["link", "local"]),
        (True, False, ["link"]),
        (False, True, ["local"]),
        (False, False, []),
    ],
    ids=["both", "link-only", "local-only", "neither"],
)
async def test_rf_relay_partial_failure(link_ok, local_ok, expected):
    rig = _Rig({"repeat_on_ingress": True})
    rig.link.fail = not link_ok
    rig.local.fail = not local_ok

    sent = await rig.handler(_flood(rx="local"), _rx("local"))

    assert sent is bool(expected)
    assert rig.sent_radio_ids == ["link", "local"]  # both attempted, bridge first
    assert rig.air.order == expected
    record = rig.records()[-1]
    assert record["transmitted"] is bool(expected)
    assert record["tx_radio_ids"] == (expected or None)
    assert record["tx_radio_id"] == (expected[0] if expected else None)
    assert rig.handler.sent_flood_count == len(expected)
    if expected:
        assert record["drop_reason"] is None
        assert (rig.handler.forwarded_count, rig.handler.dropped_count) == (1, 0)
    else:
        assert record["drop_reason"] == "TX failed"
        assert (rig.handler.forwarded_count, rig.handler.dropped_count) == (0, 1)


@pytest.mark.asyncio
async def test_send_error_on_one_radio_does_not_abort_the_other():
    rig = _Rig({"repeat_on_ingress": True})
    send = rig.dispatcher.send_packet

    async def flaky(packet, wait_for_ack=True, expected_crc=None, radio_id=None):
        if radio_id == "link":
            raise RuntimeError("modem_tcp connection reset")
        return await send(packet, wait_for_ack, expected_crc, radio_id)

    rig.dispatcher.send_packet = flaky

    assert await rig.handler(_flood(rx="local"), _rx("local")) is True
    assert rig.air.order == ["local"]
    assert rig.records()[-1]["tx_radio_ids"] == ["local"]


@pytest.mark.asyncio
async def test_duty_cycle_admits_the_bridge_egress_and_refuses_the_repeat():
    rig = _Rig({"repeat_on_ingress": True})
    # Advisory check in __call__, then the authoritative in-lock gate per egress.
    admissions = iter([(True, 0.0), (True, 0.0), (False, 4.0)])
    rig.handler.airtime_mgr.can_transmit = MagicMock(side_effect=lambda ms: next(admissions))

    assert await rig.handler(_flood(rx="local"), _rx("local")) is True

    assert rig.air.order == ["link"]
    record = rig.records()[-1]
    assert record["transmitted"] is True
    assert record["tx_radio_ids"] == ["link"]
    assert record["drop_reason"] is None
    assert rig.handler.forwarded_count == 1


@pytest.mark.asyncio
async def test_fanout_transmissions_never_overlap():
    rig = _Rig({"repeat_on_ingress": True})
    rig.local.airtime_s = rig.link.airtime_s = 0.03

    await asyncio.gather(
        rig.handler(_flood(payload=b"\x01\x01", rx="local"), _rx("local")),
        rig.handler(_flood(payload=b"\x02\x02", rx="link"), _rx("link")),
    )

    assert len(rig.air.frames) == 4
    assert rig.air.overlapped is False


@pytest.mark.asyncio
async def test_monitor_mode_blocks_relay_fanout_but_not_local_fanout():
    rig = _Rig({"repeat_on_ingress": True, "origin_tx": "all"}, mode="monitor")

    assert await rig.handler(_flood(rx="local"), _rx("local")) is False
    assert rig.air.frames == []
    assert rig.records()[-1]["drop_reason"] == "Repeat disabled"

    assert await rig.handler(_flood(payload=b"\x55\x66"), {}, local_transmission=True) is True
    assert rig.air.order == ["local", "link"]


# ---------------------------------------------------------------------------
# Locally originated fan-out (engine level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_origin_default_mode_keeps_single_fabric_send():
    rig = _Rig({"repeat_on_ingress": True})  # origin_tx defaults to "default"
    assert await rig.handler(_flood(), {}, local_transmission=True) is True
    assert rig.air.order == ["local"]
    assert rig.sent_radio_ids == [None]  # the fabric picks, as before
    assert rig.records()[-1]["tx_radio_ids"] == ["local"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "default_radio, expected", [("local", ["local", "link"]), ("link", ["link", "local"])]
)
async def test_local_origin_all_mode_sends_on_every_radio_default_first(default_radio, expected):
    rig = _Rig({"origin_tx": "all", "default_radio": default_radio})
    pkt = _direct(path=bytes([0x42, 0x43]))
    pkt._injected_origin_hash = "0x1a"

    assert await rig.handler(pkt, {}, local_transmission=True) is True

    assert rig.air.order == expected
    assert rig.air.frames[0][1] == rig.air.frames[1][1]
    # No forwarding transformation for local origins: the path is untouched.
    assert [bytes(p.path) for _, p in rig.sent_packets] == [b"\x42\x43"] * 2
    # The primary egress transmits the caller's own packet (the router echoes and
    # re-enqueues it); the other egress gets a copy that keeps the origin tag.
    assert rig.sent_packets[0][1] is pkt
    assert rig.sent_packets[1][1] is not pkt
    assert all(p._injected_origin_hash == "0x1a" for _, p in rig.sent_packets)

    record = rig.records()[-1]
    assert record["rx_radio_id"] is None
    assert record["tx_radio_id"] == expected[0]
    assert record["tx_radio_ids"] == expected
    assert rig.handler.rx_count == 0
    assert rig.handler.forwarded_count == 1
    assert rig.handler.sent_direct_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fabric, expected",
    [
        ({"local_tx_mode": "all"}, ["local", "link"]),  # the option's former name
        ({"origin_tx": "default", "local_tx_mode": "all"}, ["local"]),  # new name wins
    ],
)
async def test_origin_tx_old_name_still_selects_the_mode(fabric, expected):
    rig = _Rig(fabric)
    assert await rig.handler(_flood(), {}, local_transmission=True) is True
    assert rig.air.order == expected


@pytest.mark.asyncio
async def test_local_resend_of_a_received_packet_resolves_as_a_relay():
    """TRACE forwarding re-injects the received packet, still tagged with its ingress."""
    rig = _Rig({"repeat_on_ingress": True})
    pkt = _flood(rx="link")
    assert await rig.handler(pkt, {"rx_radio_id": "link"}, local_transmission=True) is True
    assert rig.air.order == ["local", "link"]


@pytest.mark.asyncio
async def test_local_fanout_evaluates_link_health_per_radio():
    rig = _Rig({"origin_tx": "all"})
    rig.handler.config["delays"]["local_tx_link_wait_seconds"] = 0.6
    rig.link.is_connected = False
    rig.link.is_degraded = True

    started = time.monotonic()
    assert await rig.handler(_flood(), {}, local_transmission=True) is True
    elapsed = time.monotonic() - started

    assert rig.air.order == ["local"]
    assert rig.records()[-1]["tx_radio_ids"] == ["local"]
    assert rig.local.sent_at[0] - started < 0.3  # never held up by the down link
    assert elapsed >= 0.6  # the down link got its bounded wait, then gave up


@pytest.mark.asyncio
async def test_down_primary_does_not_hold_up_the_healthy_radio():
    """Primary-first ordering applies when both radios are ready, never as a wait."""
    rig = _Rig({"origin_tx": "all"})  # "local" is the primary egress
    rig.handler.config["delays"]["local_tx_link_wait_seconds"] = 0.6
    rig.local.is_connected = False
    rig.local.is_degraded = True

    started = time.monotonic()
    assert await rig.handler(_flood(), {}, local_transmission=True) is True

    assert rig.air.order == ["link"]
    assert rig.link.sent_at[0] - started < 0.3
    record = rig.records()[-1]
    assert record["tx_radio_id"] == "link"  # the primary *successful* egress
    assert record["tx_radio_ids"] == ["link"]


@pytest.mark.asyncio
async def test_every_egress_carries_the_same_scope_and_hash_mode():
    """TX-time normalisation is decided once, so a live change mid-fan-out cannot split it."""
    rig = _Rig({"origin_tx": "all"})
    rig.dispatcher.default_flood_transport_key = get_auto_key_for("#fanout")
    rig.dispatcher.set_default_path_hash_mode(1)

    async def reconfigure(name, _data):
        if name == "local":  # the node's region / hash mode change between the sends
            rig.dispatcher.default_flood_transport_key = get_auto_key_for("#elsewhere")
            rig.dispatcher.set_default_path_hash_mode(2)

    rig.local.on_send = reconfigure
    pkt = _flood(payload=b"\x5a\x01\x02\x03", payload_type=PAYLOAD_TYPE_GRP_TXT)

    assert await rig.handler(pkt, {}, local_transmission=True) is True

    (_, first), (_, second) = rig.air.frames
    assert first == second
    assert pkt.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert pkt.get_path_hash_size() == 2
    assert pkt.write_to() == first  # what the router echoes is what went on air


@pytest.mark.asyncio
async def test_scoped_fanout_meters_the_normalised_length():
    """Scoping adds transport codes; every duty-cycle gate must see those bytes."""
    rig = _Rig({"origin_tx": "all"})
    rig.dispatcher.default_flood_transport_key = get_auto_key_for("#fanout")
    mgr = rig.handler.airtime_mgr
    pkt = _flood(payload=b"\x5a\x01\x02\x03", payload_type=PAYLOAD_TYPE_GRP_TXT)
    unscoped_len = pkt.get_raw_length()

    with patch.object(mgr, "can_transmit", wraps=mgr.can_transmit) as gate:
        assert await rig.handler(pkt, {}, local_transmission=True) is True

    on_air_len = len(rig.air.frames[0][1])
    assert on_air_len == unscoped_len + 4  # the two 16-bit transport codes
    on_air_ms = mgr.calculate_airtime(on_air_len)
    assert on_air_ms != mgr.calculate_airtime(unscoped_len)  # the test can tell them apart
    # The advisory gate in __call__ and the in-lock gate of each egress.
    assert [c.args[0] for c in gate.call_args_list] == [on_air_ms] * 3


@pytest.mark.asyncio
async def test_cancelling_a_fanout_stops_pending_egresses_and_frees_the_radio():
    rig = _Rig({"origin_tx": "all"})
    rig.local.airtime_s = 0.2
    task = await rig.handler.schedule_retransmit_fanout(
        _flood(), 0.0, 0.0, ("local", "link"), local_transmission=True
    )
    await _until(lambda: rig.air._keyed == 1)  # the primary is mid-transmission

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert rig.air.frames == []  # neither egress completed after the cancel

    # The TX lock was released: the next send goes straight out on both radios.
    rig.local.airtime_s = 0.0
    assert await rig.handler(_flood(payload=b"\x09\x09"), {}, local_transmission=True) is True
    assert rig.air.order == ["local", "link"]


# ---------------------------------------------------------------------------
# Companion / router injection and ACKs
# ---------------------------------------------------------------------------


def _router_for(rig):
    daemon = MagicMock()
    daemon.repeater_handler = rig.handler
    daemon.dispatcher = rig.dispatcher
    daemon.config = rig.handler.config
    daemon.local_hash = LOCAL_HASH
    daemon.companion_bridges = {}
    daemon.companion_frame_servers = []
    daemon._on_raw_rx_for_companions = AsyncMock()
    for helper in (
        "trace_helper",
        "discovery_helper",
        "advert_helper",
        "login_helper",
        "text_helper",
        "path_helper",
        "protocol_request_helper",
        "neighbor_scope_helper",
    ):
        setattr(daemon, helper, None)
    return PacketRouter(daemon)


def _bridge():
    bridge = MagicMock()
    bridge.process_received_packet = AsyncMock()
    return bridge


def _txt():
    return _direct(payload=b"\x42\x1a\x01\x02\x03\x04", path=bytes([0x42]))


def _ack(crc, rx):
    return _direct(
        payload=crc.to_bytes(4, "little"), path=b"", payload_type=PAYLOAD_TYPE_ACK, rx=rx
    )


async def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_companion_injection_fans_out_as_one_logical_packet():
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)
    pkt = _flood(payload=b"\x5a\x01\x02\x03", payload_type=PAYLOAD_TYPE_GRP_TXT)

    assert await router.inject_packet(pkt, origin_hash="0x1a") is True

    assert rig.air.order == ["local", "link"]
    assert all(p._injected_origin_hash == "0x1a" for _, p in rig.sent_packets)
    assert rig.handler.storage.record_packet.call_count == 1
    assert len(rig.handler.recent_packets) == 1
    assert rig.handler.rx_count == 0
    # Only the logical packet re-enters the router for companion delivery.
    assert router.queue.qsize() == 1
    assert router.queue.get_nowait() is pkt
    router.daemon._on_raw_rx_for_companions.assert_awaited_once()
    assert router.daemon._on_raw_rx_for_companions.await_args.kwargs["exclude_hash"] == "0x1a"


@pytest.mark.asyncio
async def test_originating_companion_never_receives_its_fanned_out_packet():
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)
    origin, other = _bridge(), _bridge()
    router.daemon.companion_bridges = {0x1A: origin, 0x2B: other}
    pkt = _flood(payload=b"\x5a\x01\x02\x03", payload_type=PAYLOAD_TYPE_GRP_TXT)

    assert await router.inject_packet(pkt, origin_hash="0x1a") is True
    await router._route_packet(router.queue.get_nowait())

    origin.process_received_packet.assert_not_awaited()
    other.process_received_packet.assert_awaited_once()
    assert len(rig.air.frames) == 2  # the re-routed packet is not transmitted again


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_type, route, path",
    [
        (PAYLOAD_TYPE_ADVERT, ROUTE_TYPE_FLOOD, b""),  # repeater advert
        (PAYLOAD_TYPE_RESPONSE, ROUTE_TYPE_DIRECT, bytes([0x42, 0x43])),  # server reply
    ],
    ids=["advert", "response"],
)
async def test_non_companion_local_origins_fan_out(payload_type, route, path):
    """Adverts and helper replies reach the air the way main.py and helpers send them."""
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)
    pkt = _packet(route, payload_type, bytes(range(1, 33)), path)

    assert await router.inject_packet(pkt, wait_for_ack=False) is True

    assert rig.air.order == ["local", "link"]
    assert [bytes(p.path) for _, p in rig.sent_packets] == [path] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_radio", ["local", "link"])
async def test_directed_message_waits_for_one_ack_from_either_radio(ack_radio):
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)
    crc = 0x1234ABCD
    registered = []
    expect_ack = rig.dispatcher.expect_ack

    def recording_expect_ack(ack_crc):
        event = expect_ack(ack_crc)
        registered.append(len(rig.dispatcher._waiting_acks.get(ack_crc, [])))
        return event

    rig.dispatcher.expect_ack = recording_expect_ack

    task = asyncio.create_task(
        router.inject_packet(_txt(), wait_for_ack=True, expected_crc=crc, ack_timeout_s=2.0)
    )
    await _until(lambda: len(rig.air.frames) == 2)
    await router._route_packet(_ack(crc, ack_radio))

    assert await asyncio.wait_for(task, 1.0) is True
    # One logical waiter for the CRC at any moment, never one per radio.
    assert registered and max(registered) == 1
    assert crc not in rig.dispatcher._waiting_acks


@pytest.mark.asyncio
async def test_ack_via_link_completes_send_when_local_tx_failed():
    rig = _Rig({"origin_tx": "all"})
    rig.local.fail = True
    router = _router_for(rig)
    crc = 0x0BADF00D

    task = asyncio.create_task(
        router.inject_packet(_txt(), wait_for_ack=True, expected_crc=crc, ack_timeout_s=2.0)
    )
    await _until(lambda: rig.air.order == ["link"])  # reply follows the link TX
    await router._route_packet(_ack(crc, "link"))

    assert await asyncio.wait_for(task, 3.0) is True
    assert rig.air.order == ["link"]
    assert crc not in rig.dispatcher._waiting_acks


@pytest.mark.asyncio
async def test_no_ack_wait_when_every_radio_failed():
    rig = _Rig({"origin_tx": "all"})
    rig.local.fail = rig.link.fail = True
    router = _router_for(rig)

    with patch.object(rig.dispatcher, "wait_for_ack", wraps=rig.dispatcher.wait_for_ack) as wait:
        started = time.monotonic()
        ok = await router.inject_packet(
            _txt(), wait_for_ack=True, expected_crc=0xFEEDBEEF, ack_timeout_s=5.0
        )
        elapsed = time.monotonic() - started

    assert ok is False
    wait.assert_not_called()
    assert elapsed < 2.5  # only the local-TX retry backoff, never the ACK timeout
    assert rig.air.frames == []
    assert 0xFEEDBEEF not in rig.dispatcher._waiting_acks  # reservation released


@pytest.mark.asyncio
async def test_ack_timeout_is_one_timeout_not_one_per_radio():
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)

    with patch.object(rig.dispatcher, "wait_for_ack", wraps=rig.dispatcher.wait_for_ack) as wait:
        started = time.monotonic()
        ok = await router.inject_packet(
            _txt(), wait_for_ack=True, expected_crc=0x5151A0A0, ack_timeout_s=0.5
        )
        elapsed = time.monotonic() - started

    assert ok is False
    assert len(rig.air.frames) == 2
    wait.assert_called_once_with(0x5151A0A0, timeout=0.5)
    assert 0.5 <= elapsed < 0.95  # two sequential timeouts would take 1.0 s
    assert 0x5151A0A0 not in rig.dispatcher._waiting_acks


@pytest.mark.asyncio
async def test_ack_heard_during_the_second_radio_send_is_not_lost():
    """The reply can land before the single ACK wait starts; the dispatcher cache holds it."""
    rig = _Rig({"origin_tx": "all"})
    router = _router_for(rig)
    crc = 0x600DCAFE

    async def ack_lands(name, _data):
        if name == "link":
            await router._route_packet(_ack(crc, "local"))

    rig.link.on_send = ack_lands

    ok = await asyncio.wait_for(
        router.inject_packet(_txt(), wait_for_ack=True, expected_crc=crc, ack_timeout_s=1.0),
        2.0,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_early_ack_outlives_the_dispatcher_ack_cache_prune():
    """An ACK heard while the other radio waits out a link outage is not lost.

    The link wait (5 s default, up to 30 s) can outlast the dispatcher's 5 s
    cache of unclaimed ACKs, so the send's waiter must exist before the TX.
    """
    rig = _Rig({"origin_tx": "all"})
    rig.handler.config["delays"]["local_tx_link_wait_seconds"] = 0.4
    rig.link.is_connected = False
    rig.link.is_degraded = True
    router = _router_for(rig)
    crc = 0xACEDF00D

    async def reply_then_prune(_name, _data):
        await router._route_packet(_ack(crc, "local"))
        # Stand-in for Dispatcher.run_forever expiring the cached ACK.
        rig.dispatcher._recent_acks.clear()

    rig.local.on_send = reply_then_prune

    ok = await asyncio.wait_for(
        router.inject_packet(_txt(), wait_for_ack=True, expected_crc=crc, ack_timeout_s=0.5),
        3.0,
    )
    assert ok is True
    assert rig.air.order == ["local"]
    assert crc not in rig.dispatcher._waiting_acks


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


def test_fanout_result_primary_is_first_successful_egress():
    result = FanoutTxResult(
        [RadioTxResult("link", False, error=RuntimeError("x")), RadioTxResult("local", True)]
    )
    assert result.any_success is True
    assert result.all_success is False
    assert result.primary.radio_id == "local"
    assert result.successful_radio_ids == ["local"]
    assert result.failed_radio_ids == ["link"]

    failed = FanoutTxResult([RadioTxResult("link", False), RadioTxResult("local", False)])
    assert failed.any_success is False
    assert failed.primary.radio_id == "link"
    assert FanoutTxResult().all_success is False
