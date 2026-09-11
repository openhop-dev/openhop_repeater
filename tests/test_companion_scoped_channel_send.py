"""The per-message channel flood scope is Core's; the repeater must only inherit it.

openHop Core owns the feature end to end (``docs/openhop-frame-extensions.md``
there, and its own protocol tests). What Core cannot check is that *this*
package's subclasses still reach it: ``RepeaterCompanionBridge`` and
``CompanionFrameServer`` both sit between a client and Core's send path, and
either could shadow a method or drop a keyword without any Core test noticing.

So these are wiring assertions, deliberately thin. Protocol behaviour --
byte layouts, error mapping, key validation -- belongs in Core and is not
duplicated here.

The second test is the one that matters on a repeater specifically: the
dispatcher re-resolves flood scope for everything it sends, so a scoped
companion packet only survives because Core marks it decided. If that mark
were lost, the message would go out under the repeater's own region instead of
the one the client asked for, and nothing would report an error.
"""

import functools
import struct
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from openhop_core import LocalIdentity
from openhop_core.companion import constants as core_constants
from openhop_core.companion.models import Channel
from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import ROUTE_TYPE_TRANSPORT_FLOOD
from openhop_core.protocol.transport_keys import calc_transport_code, get_auto_key_for
from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer
from repeater.packet_router import PacketRouter

# The dependency pin is `openhop_core@dev`, so until the Core change is on that
# branch CI installs a Core without these symbols and this module cannot even
# import. Skip rather than fail: the branch is then independently green, and
# the moment Core lands the guard starts running again. The skip reason names
# the cause so a silent permanent skip is visible in the report.
pytestmark = pytest.mark.skipif(
    not hasattr(core_constants, "OPENHOP_CHANNEL_TXT_SCOPED"),
    reason="installed openhop_core predates the per-message flood scope override",
)

SCOPE_KEY = get_auto_key_for("#USA")


def _frame_server(bridge):
    """A repeater frame server whose outbound frames land in a list."""
    server = CompanionFrameServer(bridge, "0x77", port=0)
    frames: list[bytes] = []
    server._write_frame = frames.append
    return server, frames


@pytest.mark.asyncio
async def test_frame_server_subclass_inherits_core_extension_handlers():
    """The repeater's CompanionFrameServer must not shadow command 3."""
    bridge = Mock()
    bridge.get_public_key = Mock(return_value=bytes(range(32)))
    bridge.get_channel = Mock(return_value=Channel(name="general", secret=bytes(16)))
    bridge.send_channel_message = AsyncMock(return_value=True)
    server, frames = _frame_server(bridge)

    probe = bytes(
        [core_constants.CMD_SEND_CHANNEL_TXT_MSG, core_constants.OPENHOP_CHANNEL_SCOPE_PROBE]
    )
    probe += bytes(core_constants.OPENHOP_SCOPE_PROBE_RESERVED_LEN)
    await server._handle_cmd(probe)

    send = bytes(
        [core_constants.CMD_SEND_CHANNEL_TXT_MSG, core_constants.OPENHOP_CHANNEL_TXT_SCOPED, 1]
    )
    send += struct.pack("<I", 1234) + SCOPE_KEY + b"hello"
    await server._handle_cmd(send)

    assert frames[0][0] == core_constants.RESP_CODE_OPENHOP_EXTENSION
    assert frames[0][1:7] == core_constants.OPENHOP_EXTENSION_MARKER
    assert frames[1] == bytes([core_constants.RESP_CODE_OK])
    bridge.send_channel_message.assert_awaited_once_with(
        1, "hello", timestamp=1234, flood_scope_key=SCOPE_KEY
    )


@pytest.mark.asyncio
async def test_scoped_send_through_repeater_bridge_reaches_injector_scoped():
    """End to end through RepeaterCompanionBridge, with the dispatcher's mark set."""
    injected = []

    async def injector(pkt, **kwargs):
        injected.append(pkt)
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), injector, node_name="rep")
    bridge.channels.set(0, Channel(name="test-ch", secret=b"\xab" * 16))

    assert await bridge.send_channel_message(0, "hello", flood_scope_key=SCOPE_KEY) is True

    assert len(injected) == 1
    pkt = injected[0]
    assert pkt.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert pkt.transport_codes[0] == calc_transport_code(SCOPE_KEY, pkt)
    assert pkt.transport_codes[1] == 0
    # Ownership metadata. The transport code is in fact protected by the route
    # change alone -- the dispatcher re-scopes only ROUTE_TYPE_FLOOD -- so this
    # is asserted as the contract Core states, not as the thing standing
    # between the message and the repeater's own region.
    assert pkt._flood_scope_applied is True


# ---------------------------------------------------------------------------
# Full production path: bridge -> PacketRouter -> engine -> dispatcher -> radio
# ---------------------------------------------------------------------------

# The repeater's own region, used both as the companion's default scope and as
# the dispatcher's, so the override is fighting a scope that really would win.
REPEATER_REGION_KEY = get_auto_key_for("#washington")
CLIENT_REGION_KEY = get_auto_key_for("#USA")


class _CaptureRadio:
    """Radio that records the raw bytes the dispatcher hands it."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.spreading_factor = 8
        self.bandwidth = 125000
        self.coding_rate = 8
        self.preamble_length = 17
        self.frequency = 915000000
        self.tx_power = 14

    async def send(self, data):
        self.sent.append(bytes(data))
        # Real wrappers (sx1262, kiss_modem) return a metadata dict, and the
        # engine reads `.get()` off it after a successful TX. Returning a bare
        # True here would crash the engine *after* the bytes were already on
        # the air, hiding the thing this test exists to check.
        return {"radio_id": "test-radio"}

    def set_rx_callback(self, callback):
        pass


def _engine_config():
    """Minimal engine config: no duty-cycle deferral, no TX delay, no adverts."""
    return {
        "repeater": {
            "mode": "forward",
            "cache_ttl": 3600,
            "node_name": "scope-int",
            "send_advert_interval_hours": 0,
        },
        "mesh": {"unscoped_flood_allow": True, "loop_detect": "off"},
        "delays": {"tx_delay_factor": 0.0, "direct_tx_delay_factor": 0.0},
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": False},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 125000,
            "coding_rate": 8,
            "preamble_length": 17,
        },
    }


def _wire_production_chain():
    """Assemble the real send chain and return (bridge, radio).

    Everything from the bridge down is the production object: a real core
    Dispatcher (whose ``send_packet`` runs the node-level scope resolver), the
    real RepeaterHandler engine, and a real PacketRouter. Only the radio and
    the daemon container are substituted.
    """
    radio = _CaptureRadio()
    dispatcher = Dispatcher(radio)
    dispatcher.default_flood_transport_key = REPEATER_REGION_KEY

    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        from repeater.engine import RepeaterHandler

        engine = RepeaterHandler(_engine_config(), dispatcher, 0xAB, local_hash_bytes=bytes([0xAB]))

    daemon = MagicMock()
    daemon.repeater_handler = engine
    daemon.dispatcher = dispatcher
    daemon.companion_bridges = {}
    for helper in (
        "trace_helper",
        "discovery_helper",
        "advert_helper",
        "login_helper",
        "text_helper",
        "path_helper",
        "protocol_request_helper",
    ):
        setattr(daemon, helper, None)

    router = PacketRouter(daemon)
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        functools.partial(router.inject_packet, origin_hash="0x77"),
        node_name="scoped-companion",
    )
    bridge.channels.set(0, Channel(name="test-ch", secret=b"\xab" * 16))
    # The companion's own default scope is the repeater's region. Without the
    # override this is what every flood it sends is scoped with, so the test
    # below is a genuine contest rather than a scope applied to empty space.
    bridge.set_default_flood_scope("washington", REPEATER_REGION_KEY)
    daemon.companion_bridges = {0x77: bridge}
    return bridge, radio


def _last_radio_packet(radio):
    assert radio.sent, "nothing reached the radio"
    pkt = Packet()
    assert pkt.read_from(radio.sent[-1]), "radio bytes did not parse"
    return pkt


@pytest.mark.asyncio
async def test_default_scope_reaches_the_radio_without_an_override():
    """Control: the repeater's own region is what normally goes out.

    Without this the test below would pass against a node that simply never
    scopes anything, which is the failure it is meant to catch.
    """
    bridge, radio = _wire_production_chain()

    assert await bridge.send_channel_message(0, "control") is True

    pkt = _last_radio_packet(radio)
    assert pkt.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert pkt.transport_codes[0] == calc_transport_code(REPEATER_REGION_KEY, pkt)


@pytest.mark.asyncio
async def test_override_survives_the_whole_repeater_path_to_the_radio():
    """The client's region is on the air, not the repeater's.

    Same node and channel as the control above; only the override differs. The
    packet crosses PacketRouter.inject_packet, the RepeaterHandler engine, and
    Dispatcher.send_packet -- which runs the node-level scope resolver against
    a default that would otherwise claim this packet -- before the bytes are
    read back off the radio.
    """
    bridge, radio = _wire_production_chain()

    assert await bridge.send_channel_message(0, "hello", flood_scope_key=CLIENT_REGION_KEY) is True

    pkt = _last_radio_packet(radio)
    assert pkt.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert pkt.transport_codes[0] == calc_transport_code(CLIENT_REGION_KEY, pkt)
    assert pkt.transport_codes[0] != calc_transport_code(REPEATER_REGION_KEY, pkt)
    assert pkt.transport_codes[1] == 0
