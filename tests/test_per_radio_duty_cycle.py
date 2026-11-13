"""Radio-aware duty-cycle metering with configurable budget grouping."""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_TXT_MSG,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_FLOOD,
)
from openhop_core.rf_fabric import RFFabric

from repeater.airtime import AirtimeBudgets, AirtimeManager
from repeater.config import (
    _apply_fabric_tx_mode,
    fabric_selects_by_ingress_radio,
    resolve_duty_cycle_budget_scope,
    validate_duty_cycle_config,
)
from repeater.engine import RepeaterHandler

WIDE = {
    "frequency": 910100000,
    "bandwidth": 500000,
    "spreading_factor": 7,
    "coding_rate": 5,
    "preamble_length": 17,
}
NARROW = dict(WIDE, frequency=910525000, bandwidth=62500)

LOCAL_HASH = 0xAB


def _config(radios=None, **duty_cycle) -> dict:
    config = {
        "repeater": {"mode": "forward", "cache_ttl": 3600, "send_advert_interval_hours": 0},
        "mesh": {"unscoped_flood_allow": True, "loop_detect": "off"},
        "delays": {"tx_delay_factor": 1.0, "direct_tx_delay_factor": 0.5},
        "duty_cycle": {
            "max_airtime_per_minute": 3600,
            "enforcement_enabled": True,
            **duty_cycle,
        },
        "radio": dict(WIDE),
    }
    if radios is not None:
        # Copied: a test that retunes a radio must not reshape the fixture for
        # every test after it.
        config["radios"] = copy.deepcopy(radios)
    return config


def _radio(radio_id: str, air: dict, **extra) -> dict:
    return {"id": radio_id, "radio_type": "sx1262", "radio": dict(air), **extra}


BRIDGE = [_radio("local", WIDE), _radio("link", NARROW)]


# ---------------------------------------------------------------------------
# Which budget a radio is metered against
# ---------------------------------------------------------------------------


def test_a_single_radio_node_meters_exactly_as_it_did():
    budgets = AirtimeBudgets(_config())
    legacy = AirtimeManager(_config())

    assert budgets.multi is False
    assert budgets.per_radio_stats() == []
    assert budgets.default.bandwidth == legacy.bandwidth
    assert budgets.default.calculate_airtime(50) == legacy.calculate_airtime(50)
    # A packet with no radio id still finds the one budget there is.
    assert budgets.for_radio("anything") is budgets.default


def test_each_radio_is_charged_at_its_own_modulation():
    budgets = AirtimeBudgets(_config(BRIDGE))

    wide = budgets.for_radio("local").calculate_airtime(50)
    narrow = budgets.for_radio("link").calculate_airtime(50)

    # 500 kHz against 62.5 kHz: the same bytes, eight times the time on air.
    assert narrow == pytest.approx(wide * 8, rel=0.05)


def test_spending_one_radio_budget_leaves_the_other_alone():
    """A busy local radio must not spend a backhaul's allowance on a band it
    never transmits on."""
    budgets = AirtimeBudgets(_config(BRIDGE))

    budgets.for_radio("local").record_tx(3600)

    assert budgets.for_radio("local").can_transmit(10)[0] is False
    assert budgets.for_radio("link").can_transmit(10)[0] is True


def test_radios_on_one_channel_share_a_budget():
    """Two transmitters on 869.618 MHz are one channel's worth of traffic
    however the node labels them."""
    same = [_radio("north", WIDE), _radio("south", WIDE)]
    budgets = AirtimeBudgets(_config(same))

    assert budgets.shares_budget("north", "south")

    budgets.for_radio("north").record_tx(3600)
    assert budgets.for_radio("south").can_transmit(10)[0] is False


def test_channel_scope_remains_the_compatibility_default():
    same = [_radio("north", WIDE), _radio("south", WIDE)]
    config = _config(same)

    budgets = AirtimeBudgets(config)

    assert resolve_duty_cycle_budget_scope(config) == "channel"
    assert budgets.budget_scope == "channel"
    assert budgets.shares_budget("north", "south")


def test_radio_scope_keeps_identical_channels_independent():
    same = [_radio("north", WIDE), _radio("south", WIDE)]
    budgets = AirtimeBudgets(_config(same, budget_scope="radio"))

    assert budgets.budget_scope == "radio"
    assert not budgets.shares_budget("north", "south")

    budgets.for_radio("north").record_tx(3600)
    assert budgets.for_radio("north").can_transmit(10)[0] is False
    assert budgets.for_radio("south").can_transmit(10)[0] is True


def test_radio_scope_keeps_a_radios_window_when_it_retunes():
    config = _config(BRIDGE, budget_scope="radio")
    budgets = AirtimeBudgets(config)
    budgets.for_radio("link").record_tx(3000)

    config["radios"][1]["radio"] = dict(WIDE)
    budgets.refresh()

    assert budgets.for_radio("link").get_stats()["current_airtime_ms"] == 3000
    assert budgets.for_radio("link").can_transmit(1000)[0] is False


def test_shared_scope_and_legacy_switch_select_one_node_ledger():
    explicit = AirtimeBudgets(_config(BRIDGE, budget_scope="shared"))
    legacy = AirtimeBudgets(_config(BRIDGE, shared_budget=True))

    assert explicit.budget_scope == "shared"
    assert legacy.budget_scope == "shared"
    assert explicit.shares_budget("local", "link")
    assert legacy.shares_budget("local", "link")


def test_explicit_scope_shares_only_named_groups():
    radios = [
        _radio("a", WIDE, duty_cycle={"budget_group": "site-a"}),
        _radio("b", NARROW, duty_cycle={"budget_group": "site-a"}),
        _radio("c", WIDE, duty_cycle={"budget_group": "site-c"}),
    ]
    budgets = AirtimeBudgets(_config(radios, budget_scope="explicit"))

    assert budgets.budget_scope == "explicit"
    assert budgets.shares_budget("a", "b")
    assert not budgets.shares_budget("a", "c")


def test_explicit_scope_uses_the_strictest_limit_in_a_group():
    radios = [
        _radio(
            "a",
            WIDE,
            duty_cycle={"budget_group": "shared-domain", "max_airtime_per_minute": 6000},
        ),
        _radio(
            "b",
            NARROW,
            duty_cycle={"budget_group": "shared-domain", "max_airtime_per_minute": 600},
        ),
    ]
    budgets = AirtimeBudgets(_config(radios, budget_scope="explicit"))

    assert budgets.for_radio("a").max_airtime_per_minute == 600
    assert budgets.for_radio("b").max_airtime_per_minute == 600


def test_stats_explain_the_resolved_scope_and_budget_id():
    radios = [
        _radio("a", WIDE, duty_cycle={"budget_group": "shared-domain"}),
        _radio("b", NARROW, duty_cycle={"budget_group": "shared-domain"}),
    ]
    budgets = AirtimeBudgets(_config(radios, budget_scope="explicit"))

    stats = {entry["radio_id"]: entry for entry in budgets.per_radio_stats()}

    assert stats["a"]["budget_scope"] == "explicit"
    assert stats["a"]["budget_id"] == "shared-domain"
    assert stats["b"]["budget_id"] == "shared-domain"


@pytest.mark.parametrize("scope", ["radio", "channel", "shared"])
def test_config_validation_accepts_each_non_explicit_scope(scope):
    assert validate_duty_cycle_config(_config(BRIDGE, budget_scope=scope)) == scope


def test_explicit_scope_requires_a_group_for_every_radio():
    config = _config(
        [
            _radio("local", WIDE, duty_cycle={"budget_group": "local"}),
            _radio("link", NARROW),
        ],
        budget_scope="explicit",
    )

    with pytest.raises(ValueError, match=r"radios\[1\].*budget_group is required"):
        validate_duty_cycle_config(config)


def test_explicit_scope_requires_each_radio_to_have_an_id():
    config = _config(
        [_radio("local", WIDE, duty_cycle={"budget_group": "local"}), {"radio": NARROW}],
        budget_scope="explicit",
    )

    with pytest.raises(ValueError, match=r"radios\[1\]\.id is required"):
        validate_duty_cycle_config(config)


def test_invalid_or_conflicting_scope_is_rejected():
    with pytest.raises(ValueError, match="budget_scope must be one of"):
        validate_duty_cycle_config(_config(BRIDGE, budget_scope="country-default"))

    with pytest.raises(ValueError, match="conflicts"):
        validate_duty_cycle_config(_config(BRIDGE, budget_scope="radio", shared_budget=True))


def test_live_scope_change_requires_restart_without_regrouping_the_window():
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(900)
    config["duty_cycle"]["budget_scope"] = "radio"

    with pytest.raises(ValueError, match="requires a service restart"):
        budgets.refresh()

    assert budgets.budget_scope == "channel"
    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 900


def test_radios_on_one_channel_keep_their_own_modulation():
    slow = dict(WIDE, spreading_factor=12)
    budgets = AirtimeBudgets(_config([_radio("fast", WIDE), _radio("slow", slow)]))

    assert budgets.shares_budget("fast", "slow")
    assert budgets.for_radio("slow").calculate_airtime(50) > budgets.for_radio(
        "fast"
    ).calculate_airtime(50)


def test_shared_budget_restores_one_allowance_for_the_whole_node():
    budgets = AirtimeBudgets(_config(BRIDGE, shared_budget=True))

    assert budgets.shares_budget("local", "link")
    assert budgets.for_radio("link").calculate_airtime(50) == pytest.approx(
        budgets.for_radio("local").calculate_airtime(50) * 8, rel=0.05
    )

    budgets.for_radio("link").record_tx(3600)
    assert budgets.for_radio("local").can_transmit(10)[0] is False


def test_a_radio_can_carry_its_own_band_limit():
    """868.0-868.6 MHz allows 1% where 869.4-869.65 allows 10%: one number
    cannot describe a bridge spanning both."""
    radios = [
        _radio("local", WIDE, duty_cycle={"max_airtime_per_minute": 600}),
        _radio("link", NARROW),
    ]

    budgets = AirtimeBudgets(_config(radios))

    assert budgets.for_radio("local").max_airtime_per_minute == 600
    assert budgets.for_radio("link").max_airtime_per_minute == 3600


def test_an_unknown_radio_is_metered_rather_than_unmetered():
    """An unrecognised label is a reason to be careful, not to transmit freely."""
    budgets = AirtimeBudgets(_config(BRIDGE))

    assert budgets.for_radio("renamed") is budgets.default


def test_per_radio_stats_report_each_budget():
    budgets = AirtimeBudgets(_config(BRIDGE))
    budgets.for_radio("link").record_tx(1800)

    stats = {entry["radio_id"]: entry for entry in budgets.per_radio_stats()}

    assert stats["local"]["current_airtime_ms"] == 0
    assert stats["link"]["current_airtime_ms"] == 1800
    assert stats["link"]["utilization_percent"] == pytest.approx(50.0)


def test_a_retune_keeps_what_is_already_on_the_air():
    """A retune is not a fresh minute; forgetting it would let a node transmit
    its whole budget twice in one window."""
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(3000)

    config["radios"][0]["radio"] = dict(WIDE, spreading_factor=9)
    budgets.refresh()

    assert budgets.for_radio("local").spreading_factor == 9
    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 3000
    assert budgets.for_radio("local").can_transmit(1000)[0] is False


# ---------------------------------------------------------------------------
# The engine charges the radio that transmits
# ---------------------------------------------------------------------------


CORE_SELECTS_BY_INGRESS = fabric_selects_by_ingress_radio(RFFabric())


class _ContextFabric:
    """The fabric contract these tests need, for a core that does not have it yet.

    Used only when the installed openhop_core predates passing the ingress radio
    to a TX selector; with a current core every test below runs against the real
    RFFabric. What it implements is core's rule and nothing more -- an explicit
    radio_id wins, then the selector, then default_radio -- so the engine is
    still measured against the contract rather than against a convenience.
    """

    def __init__(self):
        self.radios = OrderedDict()
        self._default_radio_id = None
        self._selector = None

    def register_radio(self, radio, *, radio_id):
        self.radios[radio_id] = radio
        if self._default_radio_id is None:
            self._default_radio_id = radio_id

    def set_default_radio(self, radio_id):
        self._default_radio_id = radio_id

    @property
    def default_radio_id(self):
        return self._default_radio_id

    def get_radio(self, radio_id):
        return self.radios[radio_id]

    def set_tx_selector(self, selector):
        self._selector = selector

    def resolve_tx_radio_id(self, data, radio_id=None, *, rx_radio_id=None):
        if radio_id is not None:
            return radio_id
        if self._selector is not None:
            selected = self._selector(data, rx_radio_id)
            if selected is not None:
                return selected
        return self._default_radio_id


def _fabric(radio_ids, default_radio_id=None, tx_mode="default"):
    """A fabric with the real tx_mode selectors bound to it.

    The real RFFabric where the installed core supports the ingress radio. Not a
    stub that restates the dispatch rules either way: the one review finding
    that survived two passes came from an engine that re-stated them and then
    diverged, and a fixture that also restates them cannot catch that.
    """
    fabric = RFFabric() if CORE_SELECTS_BY_INGRESS else _ContextFabric()
    for radio_id in radio_ids:
        fabric.register_radio(object(), radio_id=radio_id)
    if default_radio_id in radio_ids:
        fabric.set_default_radio(default_radio_id)
    _apply_fabric_tx_mode(fabric, tx_mode)
    return fabric


class _LegacyFabric:
    """A fabric from a core that predates passing the ingress radio to a selector.

    `resolve_tx_radio_id` has the old signature, which is how the repeater tells
    the two apart, and the selector it would run reads the node's most recent RX
    rather than this packet's ingress radio.
    """

    def __init__(self, radio_ids, default_radio_id=None, tx_mode="default"):
        self.radios = OrderedDict((radio_id, object()) for radio_id in radio_ids)
        self.default_radio_id = default_radio_id if default_radio_id in radio_ids else radio_ids[0]
        self.tx_mode = tx_mode
        self._last_rx_radio_id = None

    def resolve_tx_radio_id(self, data, radio_id=None):
        if radio_id is not None:
            return radio_id
        last = self._last_rx_radio_id
        if self.tx_mode == "sticky" and last in self.radios:
            return last
        if self.tx_mode == "bridge" and last in self.radios:
            for candidate in self.radios:
                if candidate != last:
                    return candidate
        return self.default_radio_id


def _make_handler(config, radio_ids=("local", "link"), legacy_core=False):
    radio = MagicMock()
    # Real air settings: the engine reads these off the radio for packet scoring.
    radio.spreading_factor = WIDE["spreading_factor"]
    radio.bandwidth = WIDE["bandwidth"]
    radio.coding_rate = WIDE["coding_rate"]
    radio.preamble_length = WIDE["preamble_length"]
    radio.frequency = WIDE["frequency"]
    fabric_cfg = config.get("fabric") if isinstance(config.get("fabric"), dict) else {}
    tx_mode = str(fabric_cfg.get("tx_mode", "default"))
    default_radio = fabric_cfg.get("default_radio")
    if not radio_ids:
        radio.fabric = None
    elif legacy_core:
        radio.fabric = _LegacyFabric(list(radio_ids), default_radio, tx_mode)
    else:
        radio.fabric = _fabric(list(radio_ids), default_radio, tx_mode)
    dispatcher = MagicMock()
    dispatcher.radio = radio
    dispatcher.local_identity = MagicMock()
    dispatcher.send_packet = AsyncMock(return_value=True)
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        handler = RepeaterHandler(
            config, dispatcher, LOCAL_HASH, local_hash_bytes=bytes([LOCAL_HASH])
        )
    handler.storage = MagicMock()
    return handler


def _real_flood_packet(payload: bytes = b"\x10\x20\x30\x40") -> Packet:
    """A packet the engine will actually process, for driving __call__."""
    packet = Packet()
    packet.header = ROUTE_TYPE_FLOOD | (PAYLOAD_TYPE_TXT_MSG << PH_TYPE_SHIFT)
    packet.payload = bytearray(payload)
    packet.payload_len = len(payload)
    packet.path = bytearray(b"\x11\x22")
    packet.path_len = 2
    return packet


def _packet(size: int = 50) -> MagicMock:
    packet = MagicMock()
    packet.get_raw_length.return_value = size
    packet.header = 0x00
    return packet


def test_the_default_budget_follows_fabric_default_radio_not_list_order():
    """radios[0] is `local`; the node transmits by default on `link`."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "link", "tx_mode": "default"}

    budgets = AirtimeBudgets(config)

    assert budgets.default is budgets.for_radio("link")
    assert budgets.for_radio(None) is budgets.for_radio("link")


def test_the_engine_default_manager_is_the_default_radio():
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "link", "tx_mode": "default"}

    handler = _make_handler(config)

    assert handler.airtime_mgr is handler.airtime_budgets.for_radio("link")


@pytest.mark.asyncio
async def test_a_send_is_charged_to_the_radio_it_goes_out_on():
    handler = _make_handler(_config(BRIDGE))
    packet = _packet()
    narrow_ms = handler.airtime_budgets.for_radio("link").calculate_airtime(50)

    task = await handler.schedule_retransmit(
        packet, delay=0.0, airtime_ms=1.0, preferred_tx_radio_id="link"
    )
    await task

    link = handler.airtime_budgets.for_radio("link").get_stats()
    local = handler.airtime_budgets.for_radio("local").get_stats()
    # Charged the narrow radio's real time on air, not the caller's figure.
    assert link["current_airtime_ms"] == pytest.approx(narrow_ms)
    assert local["current_airtime_ms"] == 0


@pytest.mark.asyncio
async def test_a_radio_over_budget_does_not_stop_the_other_sending():
    handler = _make_handler(_config(BRIDGE))
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    task = await handler.schedule_retransmit(
        _packet(), delay=0.0, airtime_ms=10.0, preferred_tx_radio_id="local"
    )

    assert await task is True
    assert handler.dispatcher.send_packet.await_count == 1


@pytest.mark.asyncio
async def test_a_send_on_an_exhausted_radio_is_refused_at_tx_time():
    handler = _make_handler(_config(BRIDGE))
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    task = await handler.schedule_retransmit(
        _packet(), delay=0.0, airtime_ms=10.0, preferred_tx_radio_id="link"
    )

    assert await task is False
    assert handler.dispatcher.send_packet.await_count == 0


@pytest.mark.asyncio
async def test_metering_off_stays_off():
    """airtime_ms=0 is the caller saying not to meter this send."""
    handler = _make_handler(_config(BRIDGE))
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    task = await handler.schedule_retransmit(
        _packet(), delay=0.0, airtime_ms=0.0, preferred_tx_radio_id="link"
    )

    assert await task is True
    assert handler.airtime_budgets.for_radio("link").get_stats()["current_airtime_ms"] == 3600


def test_the_advisory_gate_asks_the_radios_the_packet_is_headed_for():
    handler = _make_handler(_config(BRIDGE))
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    # Headed for both: the local radio can still carry it.
    assert handler._egress_can_transmit(_packet(), ("link", "local"))[0] is True
    # Headed only for the exhausted radio: it cannot.
    can_tx, wait = handler._egress_can_transmit(_packet(), ("link",))
    assert can_tx is False
    assert wait > 0


def test_stats_report_the_whole_node_and_add_each_channel():
    """The node-wide figures describe the node, not the default radio's channel.

    A bridge that reported one of its two radios would quietly halve figures
    people have been watching for months.
    """
    config = _config(
        [_radio("local", WIDE), _radio("link", NARROW, duty_cycle={"max_airtime_per_minute": 600})]
    )
    handler = _make_handler(config)
    handler.airtime_budgets.for_radio("link").record_tx(300)

    stats = handler.get_stats()

    assert stats["current_airtime_ms"] == 300
    assert stats["max_airtime_ms"] == 4200
    # Half of link's 600 is spent, so the node is half way to a legal limit --
    # not 300/4200, which reads as 7% for a radio that is about to have to stop.
    assert stats["utilization_percent"] == pytest.approx(50.0)
    assert {entry["radio_id"] for entry in stats["airtime_radios"]} == {"local", "link"}


def test_a_single_radio_nodes_stats_are_byte_for_byte_what_they_were():
    handler = _make_handler(_config(), radio_ids=None)
    handler.airtime_mgr.record_tx(100)
    handler.airtime_mgr.record_rx(40)

    assert handler.airtime_stats() == handler.airtime_mgr.get_stats()


def test_the_node_total_is_what_the_wire_field_reports():
    """total_air_time_secs is a MeshCore wire field other nodes read."""
    from repeater.handler_helpers.protocol_request import ProtocolRequestHelper

    handler = _make_handler(_config(BRIDGE))
    handler.airtime_budgets.for_radio("local").record_tx(4000)
    handler.airtime_budgets.for_radio("link").record_tx(5000)
    handler.airtime_budgets.for_radio("link").record_rx(2000)

    helper = ProtocolRequestHelper.__new__(ProtocolRequestHelper)
    helper.engine = handler
    helper.radio = None

    assert helper.engine.airtime_stats()["total_airtime_ms"] == 9000
    assert helper.engine.airtime_stats()["total_rx_airtime_ms"] == 2000


def test_a_single_radio_node_reports_no_per_radio_airtime():
    handler = _make_handler(_config(), radio_ids=None)

    assert "airtime_radios" not in handler.get_stats()


@pytest.mark.asyncio
async def test_a_reception_is_charged_to_the_radio_that_heard_it():
    """Driven through __call__, so deleting the engine change fails this."""
    handler = _make_handler(_config(BRIDGE))
    handler.storage = MagicMock()
    packet = _real_flood_packet()
    narrow_ms = handler.airtime_budgets.for_radio("link").calculate_airtime(packet.get_raw_length())

    await handler(packet, {"rx_radio_id": "link", "snr": 5.0, "rssi": -80})

    link = handler.airtime_budgets.for_radio("link").get_stats()
    local = handler.airtime_budgets.for_radio("local").get_stats()
    assert link["total_rx_airtime_ms"] == pytest.approx(narrow_ms)
    assert local["total_rx_airtime_ms"] == 0


@pytest.mark.asyncio
async def test_two_egresses_of_one_packet_each_charge_their_own_channel():
    """The point of the split: one logical forward, two different channel costs."""
    handler = _make_handler(_config(BRIDGE))
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0
    packet = _packet()

    task = await handler.schedule_retransmit_fanout(packet, 0.0, 1.0, ("local", "link"))
    result = await task

    assert result.all_success is True
    wide = handler.airtime_budgets.for_radio("local").get_stats()["current_airtime_ms"]
    narrow = handler.airtime_budgets.for_radio("link").get_stats()["current_airtime_ms"]
    assert narrow == pytest.approx(wide * 8, rel=0.05)


@pytest.mark.asyncio
async def test_a_fanout_sends_on_the_radio_with_budget_and_refuses_the_other():
    """End to end: one channel exhausted must cost that egress, not the forward."""
    handler = _make_handler(_config(BRIDGE))
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    task = await handler.schedule_retransmit_fanout(_packet(), 0.0, 1.0, ("local", "link"))
    result = await task

    assert result.successful_radio_ids == ["local"]
    assert result.failed_radio_ids == ["link"]
    assert handler.dispatcher.send_packet.await_count == 1
    # The exhausted radio spent nothing more; the other was charged its own cost.
    assert handler.airtime_budgets.for_radio("link").get_stats()["current_airtime_ms"] == 3600
    assert handler.airtime_budgets.for_radio("local").get_stats()[
        "current_airtime_ms"
    ] == pytest.approx(handler.airtime_budgets.for_radio("local").calculate_airtime(50))


@pytest.mark.asyncio
async def test_a_local_retry_charges_once_not_once_per_attempt():
    """The retry re-enters the lock, so the gate and the record run again."""
    handler = _make_handler(_config(BRIDGE))
    handler.dispatcher.send_packet = AsyncMock(side_effect=[False, True])
    wide_ms = handler.airtime_budgets.for_radio("local").calculate_airtime(50)

    task = await handler.schedule_retransmit(
        _packet(), delay=0.0, airtime_ms=1.0, local_transmission=True, preferred_tx_radio_id="local"
    )

    assert await task is True
    assert handler.dispatcher.send_packet.await_count == 2
    # Only the attempt that reached the air is charged.
    assert handler.airtime_budgets.for_radio("local").get_stats()[
        "current_airtime_ms"
    ] == pytest.approx(wide_ms)


# ---------------------------------------------------------------------------
# The egress radio is named before the send, by asking the fabric
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tx_mode,default_radio,rx_radio_id,expected",
    [
        # default: always the fabric's default radio, whatever heard it.
        ("default", "link", "local", "link"),
        ("default", "local", None, "local"),
        # sticky: the packet's OWN ingress radio, not the node's last RX.
        ("sticky", "local", "link", "link"),
        ("sticky", "local", None, "local"),
        # bridge with three radios: the first other radio in order.
        ("bridge", "local", "link", "local"),
        ("bridge", "local", None, "local"),
    ],
)
def test_the_fabric_names_one_egress_radio_for_every_tx_mode(
    tx_mode, default_radio, rx_radio_id, expected
):
    """Asked of a real RFFabric running the real selectors, not re-derived here.

    The engine used to restate the fabric's dispatch rules so it could know
    which channel to charge, and diverged from them. It asks now, and this is
    the question it asks.
    """
    config = _config([_radio("local", WIDE), _radio("link", NARROW), _radio("third", WIDE)])
    config["fabric"] = {"default_radio": default_radio, "tx_mode": tx_mode}
    handler = _make_handler(config, radio_ids=("local", "link", "third"))

    assert handler._planned_egress_radio_id(_real_flood_packet(), rx_radio_id) == expected


def test_the_plan_survives_another_radio_receiving_in_between():
    """The race the plan closes: a retransmit waits, and traffic keeps arriving."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "bridge"}
    handler = _make_handler(config)
    packet = _real_flood_packet()

    planned = handler._planned_egress_radio_id(packet, "local")
    handler.dispatcher.radio.fabric._last_rx_radio_id = "link"

    assert planned == "link"
    assert handler._planned_egress_radio_id(packet, "local") == planned


def test_a_single_radio_node_plans_no_egress_radio():
    """Nothing to choose, so the fabric is left exactly as it was."""
    handler = _make_handler(_config(), radio_ids=None)

    assert handler._planned_egress_radio_id(_real_flood_packet(), None) is None
    assert handler._planned_egress_radio_id(_real_flood_packet(), "local") is None


def test_an_unregistered_ingress_radio_falls_back_to_the_default():
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "link", "tx_mode": "sticky"}
    handler = _make_handler(config)

    assert handler._planned_egress_radio_id(_real_flood_packet(), "renamed") == "link"


# ---------------------------------------------------------------------------
# An older core that cannot say where a packet will leave by
# ---------------------------------------------------------------------------


def test_an_older_core_is_not_asked_to_plan():
    """The keyword is the version check; without it there is no answer to have."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "bridge"}
    handler = _make_handler(config, legacy_core=True)

    assert handler._planned_egress_radio_id(_real_flood_packet(), "local") is None


def test_an_older_core_leaves_every_radio_a_candidate():
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "bridge"}
    handler = _make_handler(config, legacy_core=True)

    assert set(handler._egress_candidates()) == {"local", "link"}


def test_tx_mode_default_is_exact_even_on_an_older_core():
    """The fabric always picks default_radio, so there is nothing to guess."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "link", "tx_mode": "default"}
    handler = _make_handler(config, legacy_core=True)

    assert handler._egress_candidates() == ("link",)


def test_an_unplanned_send_is_gated_on_the_radio_the_fabric_names():
    """An older core will say which radio it is about to use; that one has to
    have the budget, and the others are beside the point."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config, legacy_core=True)
    handler.dispatcher.radio.fabric._last_rx_radio_id = "link"
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    can_tx, wait = handler._egress_can_transmit(_packet(), None)
    assert can_tx is False
    assert wait > 0

    # The exhausted radio is no longer the one it would pick.
    handler.dispatcher.radio.fabric._last_rx_radio_id = "local"
    assert handler._egress_can_transmit(_packet(), None)[0] is True


def test_a_fabric_that_will_not_answer_falls_back_to_every_candidate():
    """Gating on *any* candidate is how a node transmits past a legal limit: it
    only has to guess the quiet channel."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "bridge"}
    handler = _make_handler(config, legacy_core=True)

    def _refuse(*args, **kwargs):
        raise RuntimeError("no answer")

    handler.dispatcher.radio.fabric.resolve_tx_radio_id = _refuse
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    can_tx, wait = handler._egress_can_transmit(_packet(), None)

    assert can_tx is False
    assert wait > 0


@pytest.mark.asyncio
async def test_an_unplanned_send_is_refused_when_its_own_radio_is_exhausted():
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config, legacy_core=True)
    handler.dispatcher.radio.fabric._last_rx_radio_id = "link"
    handler.airtime_budgets.for_radio("link").record_tx(3600)

    task = await handler.schedule_retransmit(_packet(), delay=0.0, airtime_ms=10.0)

    assert await task is False
    assert handler.dispatcher.send_packet.await_count == 0


@pytest.mark.asyncio
async def test_an_older_core_still_sends_on_the_radio_it_would_have_chosen():
    """Reported by review: holding every candidate to its budget refused a send
    the fabric was about to put on an idle radio, black-holing that band for the
    rest of the window."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config, legacy_core=True)
    handler.dispatcher.radio.fabric._last_rx_radio_id = "link"
    handler.airtime_budgets.for_radio("local").record_tx(3600)  # the other radio

    task = await handler.schedule_retransmit(_packet(), delay=0.0, airtime_ms=10.0)

    assert await task is True
    assert handler.dispatcher.send_packet.await_count == 1
    assert handler.airtime_budgets.for_radio("link").get_stats()["current_airtime_ms"] > 0


@pytest.mark.asyncio
async def test_an_unplanned_send_is_charged_to_the_radio_the_send_reports():
    """The fabric chose inside send(); its metadata is the only record of which."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "bridge"}
    handler = _make_handler(config, legacy_core=True)
    packet = _packet()
    packet._tx_metadata = {"radio_id": "link"}
    narrow_ms = handler.airtime_budgets.for_radio("link").calculate_airtime(50)

    task = await handler.schedule_retransmit(packet, delay=0.0, airtime_ms=1.0)
    assert await task is True

    assert handler.airtime_budgets.for_radio("link").get_stats()[
        "current_airtime_ms"
    ] == pytest.approx(narrow_ms)
    assert handler.airtime_budgets.for_radio("local").get_stats()["current_airtime_ms"] == 0


@pytest.mark.asyncio
async def test_a_default_mode_send_is_charged_to_the_radio_it_leaves_by():
    """The bug this fixes: default_radio is the second entry, so every packet
    went out on `link` and was charged to `local` at an eighth of its cost."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "link", "tx_mode": "default"}
    handler = _make_handler(config)
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0
    handler.storage = MagicMock()
    packet = _real_flood_packet()

    await handler(packet, {"rx_radio_id": "local", "snr": 5.0, "rssi": -80})

    # Measured on the packet that actually went out: the engine forwards a copy
    # with our hop appended, so the original is a byte short of the truth.
    sent_len = handler.dispatcher.send_packet.await_args.args[0].get_raw_length()
    narrow_ms = handler.airtime_budgets.for_radio("link").calculate_airtime(sent_len)
    wide_ms = handler.airtime_budgets.for_radio("local").calculate_airtime(sent_len)

    link = handler.airtime_budgets.for_radio("link").get_stats()
    local = handler.airtime_budgets.for_radio("local").get_stats()
    assert link["current_airtime_ms"] == pytest.approx(narrow_ms)
    assert local["current_airtime_ms"] == 0
    # The size of the error this fixes: the old code charged the wide radio.
    assert narrow_ms == pytest.approx(wide_ms * 8, rel=0.05)


# ---------------------------------------------------------------------------
# A lone radios[] entry, and rebuilds that change the topology
# ---------------------------------------------------------------------------


def test_one_configured_radio_keeps_its_own_modulation_and_limit():
    """A radios[] entry's settings must not be discarded for being the only one."""
    config = _config([_radio("link", NARROW, duty_cycle={"max_airtime_per_minute": 600})])
    config["radio"] = dict(WIDE)  # stale top-level block

    budgets = AirtimeBudgets(config)

    assert budgets.default.bandwidth == NARROW["bandwidth"]
    assert budgets.default.max_airtime_per_minute == 600


def test_a_plain_single_radio_node_is_untouched_by_that():
    config = _config()
    budgets = AirtimeBudgets(config)
    legacy = AirtimeManager(config)

    assert budgets.multi is False
    assert budgets.default.bandwidth == legacy.bandwidth
    assert budgets.default.max_airtime_per_minute == legacy.max_airtime_per_minute


def test_radios_merging_onto_one_channel_owe_that_channels_spend():
    """A channel owes what was transmitted on it, not what the arriving radio
    spent somewhere else. link's 1800 ms was on 910.525 MHz; retuning link to
    910.100 does not move that airtime onto 910.100."""
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(1800)
    budgets.for_radio("link").record_tx(1800)

    config["radios"][1]["radio"] = dict(WIDE)  # link retuned onto local's channel
    budgets.refresh()

    assert budgets.shares_budget("local", "link")
    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 1800
    # Both radios now sit on a channel with half its allowance left.
    assert budgets.for_radio("link").can_transmit(1700)[0] is True
    assert budgets.for_radio("link").can_transmit(1900)[0] is False


def test_a_radio_retuning_to_fresh_spectrum_takes_no_spend_with_it():
    """The spend stays on the channel that carried it. south moves to spectrum
    this node has not transmitted on, and that spectrum is genuinely idle."""
    config = _config([_radio("north", WIDE), _radio("south", WIDE)])
    budgets = AirtimeBudgets(config)
    budgets.for_radio("north").record_tx(3600)

    config["radios"][1]["radio"] = dict(NARROW)
    budgets.refresh()

    assert budgets.for_radio("north").get_stats()["current_airtime_ms"] == 3600
    assert budgets.for_radio("north").can_transmit(10)[0] is False
    assert budgets.for_radio("south").get_stats()["current_airtime_ms"] == 0
    assert budgets.for_radio("south").can_transmit(10)[0] is True


def test_a_channel_keeps_its_spend_after_its_last_radio_leaves():
    """Reported by review as the worst of these: a channel whose radios all move
    away lost its ledger, so the next radio onto that spectrum started from
    zero -- 3000 ms already on the air, then a fresh 3600 ms allowance inside
    the same minute."""
    config = _config([_radio("r1", WIDE), _radio("r2", NARROW)])
    budgets = AirtimeBudgets(config)
    budgets.for_radio("r1").record_tx(3000)

    config["radios"][0]["radio"] = dict(WIDE, frequency=911000000)
    budgets.refresh()
    config["radios"][1]["radio"] = dict(WIDE)  # r2 arrives on the channel r1 left
    budgets.refresh()

    assert budgets.for_radio("r2").get_stats()["current_airtime_ms"] == 3000
    assert budgets.for_radio("r2").can_transmit(3500)[0] is False
    assert budgets.for_radio("r2").can_transmit(500)[0] is True


def test_retuning_back_and_forth_does_not_inflate_what_was_spent():
    """Reported by review: copying a channel's history to both sides of a split
    and summing the copies on the next merge doubled the ledger per cycle. Six
    applies in the web UI turned 500 ms into 4000 ms and stopped the node
    forwarding for a minute."""
    config = _config([_radio("local", WIDE), _radio("link", WIDE)])
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(500)

    for step in range(6):
        config["radios"][0]["radio"] = (
            dict(WIDE, frequency=910300000) if step % 2 == 0 else dict(WIDE)
        )
        budgets.refresh()

    assert budgets.node_stats()["total_airtime_ms"] == 500
    assert budgets.for_radio("link").get_stats()["current_airtime_ms"] == 500


def test_losing_the_radio_list_does_not_hand_back_a_fresh_window():
    """Resetting to zero mid-window would allow a second full budget."""
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(3600)

    config.pop("radios")
    budgets.refresh()

    assert budgets.default.get_stats()["current_airtime_ms"] == 3600
    assert budgets.default.can_transmit(10)[0] is False


def test_a_rebuild_keeps_the_manager_callers_captured_at_boot():
    """main.py hands this object to NeighborScopeHelper, which keeps it for the
    life of the process; replacing it would leave that helper unthrottled."""
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    captured = budgets.default

    config["radios"][0]["radio"] = dict(WIDE, spreading_factor=9)
    budgets.refresh()

    assert budgets.default is captured
    assert captured.spreading_factor == 9


def test_a_shared_channel_is_held_to_the_strictest_limit():
    """shared_budget is the conservative option; it must not pick the loosest."""
    radios = [
        _radio("a", WIDE, duty_cycle={"max_airtime_per_minute": 6000}),
        _radio("b", WIDE, duty_cycle={"max_airtime_per_minute": 600}),
    ]

    budgets = AirtimeBudgets(_config(radios, shared_budget=True))

    assert budgets.for_radio("a").max_airtime_per_minute == 600


def test_a_radio_stating_no_limit_holds_its_channel_to_the_node_wide_one():
    radios = [_radio("a", WIDE, duty_cycle={"max_airtime_per_minute": 6000}), _radio("b", WIDE)]

    budgets = AirtimeBudgets(_config(radios))

    assert budgets.for_radio("a").max_airtime_per_minute == 3600


# ---------------------------------------------------------------------------
# The findings two adversarial reviews reproduced
# ---------------------------------------------------------------------------


def test_a_save_does_not_overwrite_a_lone_radios_entry_with_the_top_level_block():
    """A node with one radios[] entry is still metered on that entry.

    The legacy refresh path calls refresh_radio_params(config["radio"]), which
    is the stale top-level block. Tested on whether profiles were read, not on
    how many radios there are, or a 62.5 kHz radio is silently metered at
    500 kHz from the first web-UI save until the next restart.
    """
    from repeater.config_manager import ConfigManager

    config = _config([_radio("link", NARROW, duty_cycle={"max_airtime_per_minute": 600})])
    config["radio"] = dict(WIDE)
    handler = _make_handler(config, radio_ids=("link",))
    assert handler.airtime_mgr.bandwidth == NARROW["bandwidth"]

    daemon = MagicMock()
    daemon.repeater_handler = handler
    ConfigManager("/dev/null", config, daemon)._refresh_airtime_radio_params()

    assert handler.airtime_mgr.bandwidth == NARROW["bandwidth"]
    assert handler.airtime_mgr.max_airtime_per_minute == 600


def test_disabling_one_radio_leaves_the_other_metered_on_its_own_channel():
    """radio_type: none still boots a two-radio fabric -- get_radio_for_board
    hands back a NullRadio -- so an all-or-nothing profile collapses metering
    onto the top-level block for the radio that is still transmitting."""
    radios = [
        _radio("local", WIDE),
        dict(_radio("link", NARROW, duty_cycle={"max_airtime_per_minute": 600}), radio_type="none"),
    ]
    config = _config(radios)

    budgets = AirtimeBudgets(config)

    assert budgets.for_radio("local").bandwidth == WIDE["bandwidth"]
    assert budgets.for_radio("link").bandwidth == NARROW["bandwidth"]
    assert budgets.for_radio("link").max_airtime_per_minute == 600
    assert budgets.for_radio("local") is not budgets.for_radio("link")


def test_an_unprofilable_radio_is_metered_on_the_top_level_block():
    """It shares the channel the top-level block names rather than inventing one."""
    radios = [_radio("local", WIDE), dict(_radio("link", WIDE), radio_type="not-a-radio")]
    config = _config(radios)
    config["radio"] = dict(WIDE)

    budgets = AirtimeBudgets(config)

    assert budgets.shares_budget("link", "local")
    assert budgets.for_radio("link").bandwidth == WIDE["bandwidth"]


def test_repeated_rebuilds_do_not_compound_what_was_spent():
    """Carrying the whole node's spend onto every unrecognised channel turns
    100 -> 400 -> 1600 ms and wedges transmission for the rest of the window.

    Renamed on every pass, so the radio is unrecognised every time and the
    fallback runs four times rather than once.
    """
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(100)
    budgets.for_radio("link").record_tx(100)

    for step in range(4):
        config["radios"][1]["id"] = f"link{step}"
        budgets.refresh()

    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 100
    assert budgets.for_radio("link3").get_stats()["current_airtime_ms"] == 100


def test_a_rebuild_is_idempotent_for_an_unchanged_config():
    config = _config(BRIDGE)
    budgets = AirtimeBudgets(config)
    budgets.for_radio("link").record_tx(250)

    budgets.refresh()
    budgets.refresh()

    assert budgets.for_radio("link").get_stats()["current_airtime_ms"] == 250
    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 0


def test_readers_cannot_observe_a_partially_rebuilt_state(monkeypatch):
    budgets = AirtimeBudgets(_config(BRIDGE))
    build_started = threading.Event()
    release_build = threading.Event()
    read_finished = threading.Event()
    original_build = budgets._build_state

    def paused_build():
        build_started.set()
        assert release_build.wait(timeout=2)
        return original_build()

    monkeypatch.setattr(budgets, "_build_state", paused_build)
    refresh_thread = threading.Thread(target=budgets.refresh)
    refresh_thread.start()
    assert build_started.wait(timeout=2)

    read_thread = threading.Thread(target=lambda: (budgets.node_stats(), read_finished.set()))
    read_thread.start()
    assert not read_finished.wait(timeout=0.05)

    release_build.set()
    refresh_thread.join(timeout=2)
    read_thread.join(timeout=2)
    assert not refresh_thread.is_alive()
    assert read_finished.is_set()


def test_a_radio_added_on_fresh_spectrum_starts_empty():
    config = _config([_radio("local", WIDE), _radio("link", NARROW)])
    budgets = AirtimeBudgets(config)
    budgets.for_radio("local").record_tx(100)
    budgets.for_radio("link").record_tx(400)

    config["radios"].append(_radio("third", dict(WIDE, frequency=915000000)))
    budgets.refresh()

    # 915 MHz has carried nothing, and no duty cycle says otherwise.
    assert budgets.for_radio("third").get_stats()["current_airtime_ms"] == 0
    assert budgets.for_radio("local").get_stats()["current_airtime_ms"] == 100
    assert budgets.for_radio("link").get_stats()["current_airtime_ms"] == 400


def test_changing_the_default_radio_keeps_the_manager_captured_at_boot():
    """config is mutated in place, so working the old default out again after the
    rebuild finds the NEW one -- and preserves the wrong object, orphaning the
    manager NeighborScopeHelper is holding for the life of the process."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "default"}
    budgets = AirtimeBudgets(config)
    captured = budgets.default
    assert captured is budgets.for_radio("local")

    config["fabric"]["default_radio"] = "link"
    budgets.refresh()

    assert budgets.default is captured
    assert budgets.for_radio("link") is captured
    assert captured.bandwidth == NARROW["bandwidth"]
    assert not budgets.shares_budget("local", "link")
    assert budgets.for_radio("local").bandwidth == WIDE["bandwidth"]


def test_the_captured_manager_still_receives_what_is_charged_to_it():
    """The failure the identity swap prevents: a helper reading a manager that
    nothing debits sees an empty window and stops throttling itself."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "default"}
    budgets = AirtimeBudgets(config)
    captured = budgets.default

    config["fabric"]["default_radio"] = "link"
    budgets.refresh()
    budgets.for_radio("link").record_tx(500)

    assert captured.get_stats()["current_airtime_ms"] == 500


# ---------------------------------------------------------------------------
# End to end: the plan reaches the send, and the charge lands on that channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sticky_relay_is_sent_and_charged_on_the_radio_that_heard_it():
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config)
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0

    await handler(_real_flood_packet(), {"rx_radio_id": "link", "snr": 5.0, "rssi": -80})

    sent = handler.dispatcher.send_packet.await_args
    assert sent.kwargs["radio_id"] == "link"
    narrow_ms = handler.airtime_budgets.for_radio("link").calculate_airtime(
        sent.args[0].get_raw_length()
    )
    assert handler.airtime_budgets.for_radio("link").get_stats()[
        "current_airtime_ms"
    ] == pytest.approx(narrow_ms)
    assert handler.airtime_budgets.for_radio("local").get_stats()["current_airtime_ms"] == 0


@pytest.mark.asyncio
async def test_the_ingress_radio_stamped_on_the_packet_is_enough_to_plan_by():
    """openhop_core stamps packet._rx_radio_id, and the engine forwards a
    deep copy, so the forward has to carry it too -- it is what an older core
    routes by when nobody names a radio."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config)
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0
    packet = _real_flood_packet()
    packet._rx_radio_id = "link"

    await handler(packet, {"snr": 5.0, "rssi": -80})

    sent = handler.dispatcher.send_packet.await_args
    assert sent.kwargs["radio_id"] == "link"
    assert getattr(sent.args[0], "_rx_radio_id", None) == "link"


@pytest.mark.asyncio
async def test_an_older_core_still_forwards_and_still_meters():
    """The fallback has to be a working node, not just a safe one."""
    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "sticky"}
    handler = _make_handler(config, legacy_core=True)
    handler._calculate_tx_delay = lambda packet, snr=0.0: 0.0

    await handler(_real_flood_packet(), {"rx_radio_id": "link", "snr": 5.0, "rssi": -80})

    sent = handler.dispatcher.send_packet.await_args
    # No radio named: the fabric picks, exactly as it did before any of this.
    assert sent.kwargs.get("radio_id") is None
    # Charged somewhere rather than nowhere -- the default budget, since the
    # send reported no radio of its own.
    assert handler.airtime_budgets.default.get_stats()["current_airtime_ms"] > 0


def test_a_pending_restart_required_retune_does_not_become_live_metering():
    """Reported by review: a non-default radio's new modulation sits in radios[]
    until a restart, but the next unrelated duty-cycle save rebuilt the budgets
    from that same config and started metering it -- 8x under the truth on the
    narrow side, on hardware that had not been retuned."""
    from repeater.config_manager import ConfigManager

    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "default"}
    handler = _make_handler(config)
    assert handler.airtime_budgets.for_radio("link").bandwidth == NARROW["bandwidth"]

    daemon = MagicMock()
    daemon.repeater_handler = handler
    manager = ConfigManager("/dev/null", config, daemon)

    # A non-default radio is edited. Restart required; hardware unchanged.
    config["radios"][1]["radio"] = dict(WIDE)
    # Any later save that rebuilds the budgets, e.g. a duty-cycle change.
    manager._refresh_airtime_radio_params()

    assert handler.airtime_budgets.for_radio("link").bandwidth == NARROW["bandwidth"]
    # And the limit, which is policy rather than hardware, still follows config.
    config["radios"][1]["duty_cycle"] = {"max_airtime_per_minute": 600}
    manager._refresh_airtime_radio_params()
    assert handler.airtime_budgets.for_radio("link").max_airtime_per_minute == 600
    assert handler.airtime_budgets.for_radio("link").bandwidth == NARROW["bandwidth"]


def test_the_default_radio_adopts_its_new_modulation_once_it_is_applied():
    from repeater.config_manager import ConfigManager

    config = _config(BRIDGE)
    config["fabric"] = {"default_radio": "local", "tx_mode": "default"}
    handler = _make_handler(config)
    daemon = MagicMock()
    daemon.repeater_handler = handler
    manager = ConfigManager("/dev/null", config, daemon)

    config["radios"][0]["radio"] = dict(NARROW)
    manager._refresh_airtime_radio_params(default_radio_applied=True)

    assert handler.airtime_budgets.for_radio("local").bandwidth == NARROW["bandwidth"]


# ---------------------------------------------------------------------------
# What a partial radios[] entry is metered on
# ---------------------------------------------------------------------------
# Deliberately unlike the library fallback (62.5 kHz / SF8 / CR8) on every
# field, so a profile built from defaults cannot be mistaken for one built from
# the node's own block.
SLOW = {
    "frequency": 869618000,
    "bandwidth": 125000,
    "spreading_factor": 12,
    "coding_rate": 5,
    "preamble_length": 32,
}
LIBRARY_FALLBACK = dict(SLOW, bandwidth=62500, spreading_factor=8, coding_rate=8)


def _inheriting_config(radios: list) -> dict:
    """A node whose top-level block is the defaults its entries inherit."""
    config = _config(radios)
    config["radio_type"] = "sx1262"
    config["radio"] = dict(SLOW)
    return config


PARTIAL_LINK = {"id": "link", "radio": {"frequency": 910525000}}


def test_a_partial_entry_is_metered_on_the_nodes_own_air_settings():
    """An entry that states only its frequency used to be metered on library
    defaults rather than on the block it inherits from."""
    inherited = AirtimeBudgets(_inheriting_config([PARTIAL_LINK]))
    explicit = AirtimeBudgets(_inheriting_config([_radio("link", dict(SLOW, frequency=910525000))]))

    assert inherited.for_radio("link").spreading_factor == 12
    assert inherited.for_radio("link").bandwidth == 125000
    assert inherited.for_radio("link").calculate_airtime(50) == pytest.approx(
        explicit.for_radio("link").calculate_airtime(50)
    )


def test_a_partial_entry_is_not_metered_faster_than_it_transmits():
    """The hazard stated plainly: the old library-default profile charges a
    fraction of the real time on air, and under-charging is the wrong direction
    to be wrong about a legal limit."""
    inherited = AirtimeBudgets(_inheriting_config([PARTIAL_LINK]))
    fallback = AirtimeBudgets(
        _inheriting_config([_radio("link", dict(LIBRARY_FALLBACK, frequency=910525000))])
    )

    charged = inherited.for_radio("link").calculate_airtime(50)
    would_have_charged = fallback.for_radio("link").calculate_airtime(50)

    assert charged > would_have_charged * 4


def test_a_partial_entry_lands_on_the_channel_its_inherited_bandwidth_puts_it_on():
    """Channels are keyed on (frequency, bandwidth), so an entry metered at a
    defaulted bandwidth would be budgeted as a channel of its own rather than
    alongside the radio it actually shares spectrum with."""
    budgets = AirtimeBudgets(
        _inheriting_config(
            [
                PARTIAL_LINK,
                _radio("twin", dict(SLOW, frequency=910525000)),
                _radio("elsewhere", dict(LIBRARY_FALLBACK, frequency=910525000)),
            ]
        )
    )

    assert budgets.shares_budget("link", "twin")
    assert not budgets.shares_budget("link", "elsewhere")

    budgets.for_radio("twin").record_tx(3600)
    assert budgets.for_radio("link").can_transmit(10)[0] is False
