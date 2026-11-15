"""Ecowitt / Fine Offset weather-station gateway sensor (local HTTP API).

Reads the gateway's own JSON endpoint directly — no cloud, no Home Assistant,
no MQTT.  Works with GW1000/GW1100/GW2000/GW3000 class gateways:

    GET http://<gateway>/get_livedata_info?cmd=get_livedata_info

The response is a set of blocks (``common_list``, ``piezoRain``, ``wh25``,
``lightning``, ``debug``) whose entries are ``{"id": ..., "val": "9.7", "unit":
"C"}``.  IDs are the Ecowitt/Fine Offset field codes; values carry their unit
as a suffix, so everything is parsed and normalised here.

Output keys are chosen so the repeater's CayenneLPP telemetry encoder can use
them directly (``temperature_c``, ``humidity_pct``, ``pressure_hpa``) while the
rest (wind, rain, solar, array battery) stay available in the web UI and MQTT.

``bus_voltage_v`` is deliberately NOT emitted: the repeater uses that key as
the node's own pack voltage for the telemetry voltage floor, and the station's
capacitor voltage is not this host's supply.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# Field codes from the gateway's local API.
ID_OUT_TEMP = "0x02"
ID_DEWPOINT = "0x03"
ID_OUT_HUMIDITY = "0x07"
ID_WIND_DIR = "0x0A"
ID_WIND_SPEED = "0x0B"
ID_WIND_GUST = "0x0C"
ID_RAIN_EVENT = "0x0D"
ID_RAIN_RATE = "0x0E"
ID_SOLAR = "0x15"
ID_UV = "0x17"
ID_MAX_GUST = "0x19"

# piezoRain block
ID_RAIN_DAY = "0x7C"
ID_RAIN_HOUR = "0x7D"
ID_RAIN_WEEK = "0x11"
ID_RAIN_MONTH = "0x12"
ID_RAIN_YEAR = "0x13"
ID_RAIN_TOTAL = "0x14"

_HPA_PER_INHG = 33.8639
_KMH_PER_MPH = 1.609344


_VALUE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(.*)$")


def _split_value(raw: Any) -> tuple[Optional[float], str]:
    """Split ``"81%"`` / ``"3.24 km/h"`` into (81.0, "%" / "km/h")."""
    if raw is None:
        return None, ""
    text = str(raw).strip()
    if not text:
        return None, ""
    match = _VALUE_RE.match(text)
    if match is None:
        return None, text.lower()
    return float(match.group(1)), match.group(2).strip().lower()


def _value_unit(raw: Any, unit_hint: Any = None) -> tuple[Optional[float], str]:
    """Value and unit for a field, honouring the gateway's separate ``unit`` key.

    Some entries carry the unit inside the value ("3.24 km/h"), others put it in
    a sibling key (``{"val": "50.0", "unit": "F"}``). The sibling key wins, so a
    Fahrenheit-configured gateway is converted rather than treated as Celsius.
    """
    value, unit = _split_value(raw)
    hint = str(unit_hint or "").strip().lower()
    return value, (hint or unit)


def _to_celsius(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    if unit in ("f", "°f"):
        return (value - 32.0) * 5.0 / 9.0
    return value


def _to_kmh(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    if unit in ("mph",):
        return value * _KMH_PER_MPH
    if unit in ("m/s", "ms"):
        return value * 3.6
    if unit in ("kt", "knots"):
        return value * 1.852
    return value


def _to_hpa(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    if unit == "kpa":
        return value * 10.0
    if unit in ("inhg", "in"):
        return value * _HPA_PER_INHG
    if unit == "mmhg":
        return value * 1.3332239
    return value


def _to_mm(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    # Totals report "in"; rates report "in/hr" or "in/h".
    if unit.startswith("in"):
        return value * 25.4
    return value


def _entry(entries: Dict[str, dict], key: str) -> tuple[Any, Any]:
    """(value, unit) for one field code, tolerating a missing entry."""
    entry = entries.get(key) or {}
    return entry.get("val"), entry.get("unit")


def _by_id(entries: Any) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("id")
        if isinstance(key, str):
            out[key] = entry
    return out


@SensorRegistry.register("ecowitt")
class EcowittGatewaySensor(SensorBase):
    """Poll an Ecowitt/Fine Offset gateway's local JSON API."""

    sensor_type = "ecowitt"

    _settings_schema = [
        {
            "key": "host",
            "type": "string",
            "label": "Gateway Host",
            "default": "",
            "help": "Gateway hostname or IP address",
        },
        {
            "key": "scheme",
            "type": "string",
            "label": "Scheme",
            "default": "http",
            "help": "HTTP or HTTPS (gateway web server defaults to http)",
        },
        {
            "key": "port",
            "type": "integer",
            "label": "Port",
            "default": 80,
            "help": "Gateway web/API port (80 on GW2000/GW3000)",
        },
        {
            "key": "endpoint",
            "type": "string",
            "label": "Endpoint",
            "default": "/get_livedata_info?cmd=get_livedata_info",
            "help": "Live-data path on the gateway",
        },
        {
            "key": "poll_interval_seconds",
            "type": "number",
            "label": "Poll Interval (s)",
            "default": 60.0,
            "help": "How often to poll; the station array itself reports about once a minute",
        },
        {
            "key": "timeout_seconds",
            "type": "number",
            "label": "Timeout (s)",
            "default": 5.0,
            "help": "HTTP request timeout",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)
        self.host = str(self.settings.get("host", "") or "").strip()
        self.scheme = str(self.settings.get("scheme", "http") or "http").strip()
        self.port = int(self.settings.get("port", 80) or 80)
        self.endpoint = str(
            self.settings.get("endpoint", "/get_livedata_info?cmd=get_livedata_info")
            or "/get_livedata_info?cmd=get_livedata_info"
        )
        self.poll_interval_seconds = float(self.settings.get("poll_interval_seconds", 60.0))
        self.timeout_seconds = float(self.settings.get("timeout_seconds", 5.0))
        if not self.host:
            raise ValueError("ecowitt sensor requires settings.host")
        if not self.endpoint.startswith("/"):
            self.endpoint = "/" + self.endpoint
        self.url = f"{self.scheme}://{self._netloc()}{self.endpoint}"

    def _netloc(self) -> str:
        """Build host[:port], bracketing a bare IPv6 literal.

        A bare IPv6 address contains colons, so testing for ":" alone would
        mistake it for host:port and produce an unparseable URL.
        """
        host = self.host
        if host.startswith("["):
            return host if "]:" in host else f"{host}:{self.port}"
        if host.count(":") > 1:  # bare IPv6 literal, e.g. fe80::1
            return f"[{host}]:{self.port}"
        if ":" in host:  # host:port supplied in settings
            return host
        return f"{host}:{self.port}"

    # -- transport ---------------------------------------------------------
    def _fetch(self) -> Dict[str, Any]:
        request = urllib.request.Request(self.url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"gateway returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"gateway unreachable: {exc.reason}") from exc
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise RuntimeError("gateway returned non-JSON payload") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("gateway returned an unexpected JSON shape")
        return payload

    # -- parsing -----------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        payload = self._fetch()
        common = _by_id(payload.get("common_list"))
        rain = _by_id(payload.get("piezoRain")) or _by_id(payload.get("rain"))
        wh25 = payload.get("wh25")
        indoor = wh25[0] if isinstance(wh25, list) and wh25 else {}
        if not isinstance(indoor, dict):
            indoor = {}
        lightning = payload.get("lightning")
        strike = lightning[0] if isinstance(lightning, list) and lightning else {}
        if not isinstance(strike, dict):
            strike = {}
        debug = payload.get("debug")
        gateway = debug[0] if isinstance(debug, list) and debug else {}
        if not isinstance(gateway, dict):
            gateway = {}

        data: Dict[str, Any] = {}

        def put(key: str, value: Any) -> None:
            if value is not None:
                data[key] = value

        # Outdoor block (WH90/WS90 array, or a WH69-style combined sensor).
        value, unit = _value_unit(*_entry(common, ID_OUT_TEMP))
        put("temperature_c", _to_celsius(value, unit))
        value, unit = _value_unit(*_entry(common, ID_OUT_HUMIDITY))
        put("humidity_pct", value)
        value, unit = _value_unit(*_entry(common, ID_DEWPOINT))
        put("dewpoint_c", _to_celsius(value, unit))

        # Pressure: the WH25/indoor module carries the calibrated pair.
        value, unit = _split_value(indoor.get("rel"))
        put("pressure_hpa", _to_hpa(value, unit))
        value, unit = _split_value(indoor.get("abs"))
        put("pressure_abs_hpa", _to_hpa(value, unit))
        value, unit = _value_unit(indoor.get("intemp"), indoor.get("unit"))
        put("indoor_temperature_c", _to_celsius(value, unit))
        value, unit = _split_value(indoor.get("inhumi"))
        put("indoor_humidity_pct", value)

        # Wind.
        value, unit = _value_unit(*_entry(common, ID_WIND_SPEED))
        put("wind_speed_kmh", _to_kmh(value, unit))
        value, unit = _value_unit(*_entry(common, ID_WIND_GUST))
        put("wind_gust_kmh", _to_kmh(value, unit))
        value, unit = _value_unit(*_entry(common, ID_MAX_GUST))
        put("wind_max_gust_kmh", _to_kmh(value, unit))
        value, unit = _value_unit(*_entry(common, ID_WIND_DIR))
        put("wind_dir_deg", value)

        # Rain (piezo block first; older gateways use a plain "rain" block).
        value, unit = _value_unit(*_entry(rain, ID_RAIN_RATE))
        put("rain_rate_mmh", _to_mm(value, unit))
        value, unit = _value_unit(*_entry(rain, ID_RAIN_DAY))
        put("rain_day_mm", _to_mm(value, unit))
        value, unit = _value_unit(*_entry(rain, ID_RAIN_WEEK))
        put("rain_week_mm", _to_mm(value, unit))
        value, unit = _value_unit(*_entry(rain, ID_RAIN_MONTH))
        put("rain_month_mm", _to_mm(value, unit))
        value, unit = _value_unit(*_entry(rain, ID_RAIN_YEAR))
        put("rain_year_mm", _to_mm(value, unit))
        value, unit = _value_unit(*_entry(rain, ID_RAIN_TOTAL))
        put("rain_total_mm", _to_mm(value, unit))

        # Solar: the same field code carries irradiance (W/m2) or illuminance
        # (lux) depending on the gateway's configuration. Keep them apart - a
        # W/m2 value must never be reported as lux (they differ by ~100x).
        value, unit = _value_unit(*_entry(common, ID_SOLAR))
        if value is not None:
            if unit in ("lux", "lx"):
                data["illuminance_lux"] = value
            elif unit.startswith("fc"):
                data["illuminance_lux"] = value * 10.7639
            else:
                data["solar_wm2"] = value
        value, unit = _value_unit(*_entry(common, ID_UV))
        put("uv_index", value)

        # Sensor-array battery: the gateway parks it on the rain-year entry.
        year_entry = rain.get(ID_RAIN_YEAR, {})
        if isinstance(year_entry, dict):
            if year_entry.get("battery") is not None:
                try:
                    data["station_battery"] = int(year_entry["battery"])
                except (TypeError, ValueError):
                    pass
            for src, dst in (("ws90cap_volt", "station_capacitor_v"),
                             ("voltage", "station_battery_v")):
                try:
                    if year_entry.get(src) is not None:
                        data[dst] = float(year_entry[src])
                except (TypeError, ValueError):
                    pass
            if year_entry.get("ws90_ver") is not None:
                data["station_array_version"] = str(year_entry["ws90_ver"])
            if "station_capacitor_v" in data:
                # Generic supply-rail key the telemetry encoder understands, so
                # the array's capacitor voltage rides as its own LPP voltage
                # entry without overwriting the node's pack voltage.
                data["supply_voltage_v"] = data["station_capacitor_v"]

        # Lightning (WH57 and similar).
        if strike:
            value, unit = _split_value(strike.get("distance"))
            put("lightning_distance_km", value)
            try:
                if strike.get("count") is not None:
                    data["lightning_count"] = int(strike["count"])
            except (TypeError, ValueError):
                pass

        # Gateway health, useful when the station goes quiet.
        if gateway:
            try:
                if gateway.get("heap") is not None:
                    data["gateway_heap_bytes"] = int(gateway["heap"])
                if gateway.get("runtime") is not None:
                    data["gateway_uptime_s"] = int(gateway["runtime"])
            except (TypeError, ValueError):
                pass
            if gateway.get("reset_reason") is not None:
                data["gateway_reset_reason"] = str(gateway["reset_reason"])

        if not data:
            raise RuntimeError("gateway JSON contained no recognised fields")
        return data
