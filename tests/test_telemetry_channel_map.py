"""Telemetry channel-map, guest gating and companion self-telemetry tests.

The repeater answers GET_TELEMETRY with CayenneLPP built from the SensorManager
cache. These tests pin the wire contract:

- each sensor reading owns TELEM_CHANNEL_STRIDE channels (power, environment,
  wind, supply, illuminance), so a quantity keeps a stable channel even when an
  earlier group is empty;
- a reading that produces nothing does not consume channels;
- no (channel, type) pair is emitted twice - two LPP_VOLTAGE entries on one
  channel is a decoder hazard;
- channel bytes never wrap (``_add_field`` masks rather than raising);
- guests get the sensor block only when ``repeater.telemetry.guest_environment``
  is set;
- the companion self-telemetry hook returns the same body the repeater identity
  would answer with.
"""

import pytest

from repeater.companion.frame_server import CompanionFrameServer
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LPP_SIZES = {
    2: 2,  # analog input
    100: 4,  # generic
    101: 2,  # luminosity
    103: 2,  # temperature
    104: 1,  # humidity
    115: 2,  # barometric pressure
    116: 2,  # voltage
    117: 2,  # current
    120: 1,  # percentage
    128: 2,  # power
    132: 2,  # direction
}

TELEM_CHANNEL_SELF = 1


def decode_lpp(blob):
    """Decode a CayenneLPP frame into [(channel, type, raw_value_bytes)]."""
    rows, i = [], 0
    while i < len(blob):
        channel, lpp_type = blob[i], blob[i + 1]
        size = _LPP_SIZES[lpp_type]
        rows.append((channel, lpp_type, blob[i + 2 : i + 2 + size]))
        i += 2 + size
    assert i == len(blob), "frame must be consumed exactly"
    return rows


def pairs(rows):
    return [(channel, lpp_type) for channel, lpp_type, _ in rows]


class _Client:
    def __init__(self, guest):
        self.guest = guest

    def is_guest(self):
        return self.guest

    def is_admin(self):
        return not self.guest


class _Helper(ProtocolRequestHelper):
    """ProtocolRequestHelper with the sensor provider and config swapped in."""

    def __init__(self, readings, config=None):
        self.sensor_manager = _Manager(readings)
        self.config = config or {}

    def _get_sensor_readings(self):
        return self.sensor_manager.get_summary()["readings"]


class _Manager:
    def __init__(self, readings):
        self._readings = readings

    def get_summary(self):
        return {"readings": self._readings}


class _FrameServer(CompanionFrameServer):
    """Companion frame server with just the sensor provider populated."""

    def __init__(self, readings):
        self.sensor_manager = _Manager(readings)


def _reading(**data):
    return {"name": "weather", "type": "ecowitt", "ok": True, "timestamp": "2026-11-15T00:00:00Z", "data": data}


_INVERSE_ALL = (~0x07) & 0xFF  # client requests base + location + environment


def _telemetry(helper, guest=False, inverse=_INVERSE_ALL):
    return helper._handle_get_telemetry(_Client(guest), 0, bytes([inverse]))


# ---------------------------------------------------------------------------
# Channel map
# ---------------------------------------------------------------------------


def test_channel_map_assigns_one_group_per_channel():
    readings = [
        _reading(
            temperature_c=10.5,
            humidity_pct=65.0,
            pressure_hpa=994.8,
            wind_dir_deg=76.0,
            supply_voltage_v=3.7,
            illuminance_lux=800.0,
        )
    ]
    frame = _telemetry(_Helper(readings))
    rows = decode_lpp(frame)
    got = pairs(rows)

    stride = ProtocolRequestHelper.TELEM_CHANNEL_STRIDE
    base = TELEM_CHANNEL_SELF + 1

    # Node voltage floor first, then the reading's groups in map order.
    assert got[0] == (TELEM_CHANNEL_SELF, 116)
    assert (base + ProtocolRequestHelper.TELEM_GROUP_ENVIRONMENT, 103) in got
    assert (base + ProtocolRequestHelper.TELEM_GROUP_ENVIRONMENT, 104) in got
    assert (base + ProtocolRequestHelper.TELEM_GROUP_ENVIRONMENT, 115) in got
    assert (base + ProtocolRequestHelper.TELEM_GROUP_WIND, 132) in got
    assert (base + ProtocolRequestHelper.TELEM_GROUP_SUPPLY, 116) in got
    assert (base + ProtocolRequestHelper.TELEM_GROUP_SOLAR, 101) in got
    assert stride == 5


def test_empty_reading_does_not_consume_channels():
    # hardware_stats reports no telemetry; the weather reading after it must
    # still land on the first group channel, not five channels along.
    readings = [
        {"name": "system-health", "type": "hardware_stats", "ok": True, "data": {"cpu": {"usage_percent": 3}}},
        _reading(temperature_c=20.0),
    ]
    rows = decode_lpp(_telemetry(_Helper(readings)))
    channels = [c for c, _, _ in rows]
    assert channels[0] == TELEM_CHANNEL_SELF
    assert min(c for c in channels if c != TELEM_CHANNEL_SELF) == (
        TELEM_CHANNEL_SELF + 1 + ProtocolRequestHelper.TELEM_GROUP_ENVIRONMENT
    )


def test_quantities_without_lpp_types_are_not_encoded():
    # Wind speed, rain and UV have no CayenneLPP type; carrying them in analog
    # input makes clients render a wrong measurement, so they stay off the air.
    readings = [_reading(wind_speed_kmh=12.5, rain_rate_mmh=1.2, rain_day_mm=4.4, uv_index=7.0, solar_wm2=650.0)]
    rows = decode_lpp(_telemetry(_Helper(readings)))
    assert all(lpp_type != 2 for _, lpp_type, _ in rows)  # no analog input


def test_irradiance_is_not_sent_as_illuminance():
    # solar_wm2 is W/m2; the luminosity field is lux. Different quantity.
    assert decode_lpp(_telemetry(_Helper([_reading(solar_wm2=650.0)]))) == [
        (TELEM_CHANNEL_SELF, 116, b"\x00\x00")
    ]


def test_no_duplicate_type_on_one_channel():
    # A rail voltage and a supply voltage must not both land on the power
    # channel: two LPP_VOLTAGE entries on one channel is a decoder hazard.
    readings = [_reading(bus_voltage_v=12.5, current_ma=250, power_mw=3000, supply_voltage_v=4.6, battery_percent=80)]
    rows = decode_lpp(_telemetry(_Helper(readings)))
    assert len(pairs(rows)) == len(set(pairs(rows)))


def test_channel_space_guard_stops_before_wrapping(caplog):
    readings = [_reading(temperature_c=20.0) for _ in range(60)]
    with caplog.at_level("WARNING"):
        rows = decode_lpp(_telemetry(_Helper(readings)))
    channels = [c for c, _, _ in rows]
    assert max(channels) <= 0xFF
    assert 0 not in channels
    # 60 readings would need channels up to 2 + 59*5 + 4; the guard truncates.
    assert len(readings) > len(rows)
    assert any("channel space exhausted" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Guest gating
# ---------------------------------------------------------------------------


def test_guest_gets_voltage_only_by_default():
    readings = [_reading(temperature_c=20.0)] * 2  # two so the floor is one entry
    frame = _telemetry(_Helper(readings, config={"repeater": {"telemetry": {"guest_environment": False}}}), guest=True)
    assert decode_lpp(frame) == [(TELEM_CHANNEL_SELF, 116, b"\x00\x00")]


def test_guest_gets_sensor_block_when_opted_in():
    readings = [_reading(temperature_c=20.0)]
    config = {"repeater": {"telemetry": {"guest_environment": True}}}
    guest_frame = _telemetry(_Helper(readings, config=config), guest=True)
    admin_frame = _telemetry(_Helper(readings, config=config), guest=False)
    assert guest_frame == admin_frame
    assert (TELEM_CHANNEL_SELF + 1 + ProtocolRequestHelper.TELEM_GROUP_ENVIRONMENT, 103) in pairs(
        decode_lpp(guest_frame)
    )


def test_guest_requesting_base_only_still_gets_no_environment():
    # The guest mask is narrowed to TELEM_PERM_ENVIRONMENT, never widened, so a
    # client that asks for base only receives the voltage floor.
    config = {"repeater": {"telemetry": {"guest_environment": True}}}
    base_only = (~0x01) & 0xFF
    frame = _telemetry(_Helper([_reading(temperature_c=20.0)], config=config), guest=True, inverse=base_only)
    assert decode_lpp(frame) == [(TELEM_CHANNEL_SELF, 116, b"\x00\x00")]


# ---------------------------------------------------------------------------
# Companion self-telemetry parity
# ---------------------------------------------------------------------------


def test_companion_self_telemetry_matches_repeater_body():
    readings = [_reading(temperature_c=10.5, humidity_pct=65.0, pressure_hpa=994.8, wind_dir_deg=76.0, supply_voltage_v=3.7)]
    config = {"repeater": {"telemetry": {"guest_environment": True}}}
    repeater_frame = _telemetry(_Helper(readings, config=config), guest=False)
    hook = _FrameServer(readings)._get_self_telemetry_lpp()
    # The companion push prepends its own channel-1 voltage entry, so the hook
    # returns exactly the repeater frame minus that 4-byte entry.
    assert hook == repeater_frame[4:]


def test_companion_self_telemetry_without_sensor_manager_is_empty():
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.sensor_manager = None
    assert server._get_self_telemetry_lpp() == b""


def test_companion_self_telemetry_survives_a_broken_manager():
    class Boom:
        def get_summary(self):
            raise RuntimeError("sensor manager exploded")

    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.sensor_manager = Boom()
    assert server._get_self_telemetry_lpp() == b""
