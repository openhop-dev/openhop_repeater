"""Read-only VE.Direct text telemetry (Victron protocol 3.34)."""

from __future__ import annotations

import importlib
import re
import time
from typing import ClassVar

from .base import SensorBase
from .registry import SensorRegistry


class VEDirectParser:
    """Incremental binary parser; HEX bytes do not contribute to text checksum."""

    checksum_label = b"\r\nChecksum\t"

    def __init__(self):
        self.buffer = bytearray()
        self.in_hex = False

    def feed(self, data: bytes) -> list[dict[str, str]]:
        frames = []
        for byte in data:
            # Checksum is exactly one arbitrary byte, including ':' or LF.
            if self.buffer.endswith(self.checksum_label):
                self.buffer.append(byte)
                block = bytes(self.buffer)
                for start in range(len(block)):
                    if block[start : start + 2] != b"\r\n":
                        continue
                    candidate = block[start:]
                    if sum(candidate) % 256:
                        continue
                    try:
                        lines = candidate[2 : -len(self.checksum_label) - 1].split(b"\r\n")
                        if not 1 <= len(lines) <= 22:
                            break
                        fields = {}
                        for line in lines:
                            label, value = line.decode("ascii").split("\t")
                            if (
                                not 1 <= len(label) <= 8
                                or len(value) > 32
                                or not label.isascii()
                                or label in fields
                                or any(ord(c) < 32 or ord(c) > 126 for c in label + value)
                            ):
                                raise ValueError("invalid field")
                            fields[label] = value
                    except (ValueError, UnicodeError):
                        break
                    frames.append(fields)
                    break
                self.buffer.clear()
            elif self.in_hex:
                if byte == 10:
                    self.in_hex = False
            elif byte == 58:
                self.in_hex = True
            else:
                self.buffer.append(byte)
                if len(self.buffer) > 2048:
                    # Keep suffix for the next complete block; bounded even for noise.
                    del self.buffer[:-2048]
        return frames


# Protocol Table I, pp. 6–8. Only explicitly supported labels leave the parser.
# label -> (flat key, divisor, unit); flags use divisor None.
FIELDS = {
    "V": ("battery_voltage_v", 1000, "V"),
    "I": ("battery_current_a", 1000, "A"),
    "VPV": ("pv_voltage_v", 1000, "V"),
    "PPV": ("pv_power_w", 1, "W"),
    "IL": ("load_current_a", 1000, "A"),
    "CS": ("charge_state_code", 1, None),
    "ERR": ("error_code", 1, None),
    "MPPT": ("tracker_state_code", 1, None),
    "HSDS": ("day_sequence", 1, None),
    "OR": ("off_reason", 1, None),
    "LOAD": ("load_output", None, None),
    "Relay": ("relay", None, None),
    "Alarm": ("alarm", None, None),
    "AR": ("alarm_reason", 1, None),
    "VS": ("aux_voltage_v", 1000, "V"),
    "VM": ("midpoint_voltage_v", 1000, "V"),
    "DM": ("midpoint_deviation_percent", 10, "%"),
    "T": ("battery_temperature_c", 1, "°C"),
    "P": ("battery_power_w", 1, "W"),
    "CE": ("consumed_ah", 1000, "Ah"),
    "SOC": ("state_of_charge_percent", 10, "%"),
    "TTG": ("time_to_go_minutes", 1, "min"),
    "H1": ("deepest_discharge_ah", 1000, "Ah"),
    "H2": ("last_discharge_ah", 1000, "Ah"),
    "H3": ("average_discharge_ah", 1000, "Ah"),
    "H4": ("charge_cycles", 1, None),
    "H5": ("full_discharges", 1, None),
    "H6": ("cumulative_drawn_ah", 1000, "Ah"),
    "H7": ("min_battery_voltage_v", 1000, "V"),
    "H8": ("max_battery_voltage_v", 1000, "V"),
    "H9": ("seconds_since_full_charge", 1, "s"),
    "H10": ("automatic_syncs", 1, None),
    "H11": ("low_voltage_alarms", 1, None),
    "H12": ("high_voltage_alarms", 1, None),
    "H15": ("min_aux_voltage_v", 1000, "V"),
    "H16": ("max_aux_voltage_v", 1000, "V"),
    "H17": ("history_energy_h17_kwh", 100, "kWh"),
    "H18": ("history_energy_h18_kwh", 100, "kWh"),
    "MON": ("monitor_mode_code", 1, None),
    "H19": ("yield_total_kwh", 100, "kWh"),
    "H20": ("yield_today_kwh", 100, "kWh"),
    "H21": ("max_power_today_w", 1, "W"),
    "H22": ("yield_yesterday_kwh", 100, "kWh"),
    "H23": ("max_power_yesterday_w", 1, "W"),
}


# Protocol 3.34, p. 15: CS state of operation. Product families emit subsets.
CHARGE_STATES = {
    0: "Off",
    1: "Low power",
    2: "Fault",
    3: "Bulk",
    4: "Absorption",
    5: "Float",
    6: "Storage",
    7: "Equalize (manual)",
    9: "Inverting",
    11: "Power supply",
    245: "Starting-up",
    246: "Repeated absorption",
    247: "Auto equalize / Recondition",
    248: "BatterySafe",
    252: "External Control",
}


# Protocol 3.34, pp. 15–16. Decode statuses inside this drop-in sensor.
ENUM_VALUES = {
    "CS": CHARGE_STATES,
    "ERR": {
        0: "No error",
        2: "Battery voltage too high",
        17: "Charger temperature too high",
        18: "Charger over current",
        19: "Charger current reversed",
        20: "Bulk time limit exceeded",
        21: "Current sensor issue (sensor bias/sensor broken)",
        26: "Terminals overheated",
        28: "Converter issue (dual converter models only)",
        33: "Input voltage too high (solar panel)",
        34: "Input current too high (solar panel)",
        38: "Input shutdown (due to excessive battery voltage)",
        39: "Input shutdown (due to current flow during off mode)",
        65: "Lost communication with one of devices",
        66: "Synchronised charging device configuration issue",
        67: "BMS connection lost",
        68: "Network misconfigured",
        116: "Factory calibration data lost",
        117: "Invalid/incompatible firmware",
        119: "User settings invalid",
    },
    "MPPT": {0: "Off", 1: "Voltage or current limited", 2: "MPP Tracker active"},
    "MON": {
        -9: "Solar charger",
        -8: "Wind turbine",
        -7: "Shaft generator",
        -6: "Alternator",
        -5: "Fuel cell",
        -4: "Water generator",
        -3: "DC/DC charger",
        -2: "AC charger",
        -1: "Generic source",
        0: "Battery monitor (BMV)",
        1: "Generic load",
        2: "Electric drive",
        3: "Fridge",
        4: "Water pump",
        5: "Bilge pump",
        6: "DC system",
        7: "Inverter",
        8: "Water heater",
    },
}


# Protocol 3.34, p. 11: OR is hexadecimal; AR is decimal, both additive masks.
MASK_VALUES = {
    "OR": {
        1: "No input power",
        2: "Switched off (power switch)",
        4: "Switched off (device mode register)",
        8: "Remote input",
        16: "Protection active",
        32: "Paygo",
        64: "BMS",
        128: "Engine shutdown detection",
        256: "Analysing input voltage",
    },
    "AR": {
        1: "Low Voltage",
        2: "High Voltage",
        4: "Low SOC",
        8: "Low Starter Voltage",
        16: "High Starter Voltage",
        32: "Low Temperature",
        64: "High Temperature",
        128: "Mid Voltage",
        256: "Overload",
        512: "DC-ripple",
        1024: "Low V AC out",
        2048: "High V AC out",
        4096: "Short Circuit",
        8192: "BMS Lockout",
    },
}


def status_display_value(label, value):
    if label in ENUM_VALUES:
        return ENUM_VALUES[label].get(value, f"Unknown ({value})")
    reasons = []
    unknown = value
    for bit, text in MASK_VALUES[label].items():
        if value & bit:
            reasons.append(text)
            unknown &= ~bit
    if unknown:
        reasons.append(f"Unknown bits (0x{unknown:X})")
    return "; ".join(reasons) if reasons else "None"


def normalize(fields):
    data = {}
    for label, (key, divisor, _) in FIELDS.items():
        if label not in fields:
            continue
        raw = fields[label]
        data[key] = None
        if divisor is None:
            if raw.upper() in {"ON", "OFF"}:
                data[key] = raw.upper() == "ON"
        else:
            pattern = r"(?:0x)?[0-9A-Fa-f]+" if label == "OR" else r"[+-]?[0-9]+"
            if not re.fullmatch(pattern, raw):
                continue
            value = int(raw, 16 if label == "OR" else 10)
            if (
                (label in {"CS", "ERR", "MPPT", "AR"} and value < 0)
                or (label == "TTG" and value < 0)
                or (label == "SOC" and not 0 <= value <= 1000)
                or (label == "HSDS" and not 0 <= value <= 364)
            ):
                continue
            data[key] = value if divisor == 1 else value / divisor
    return data


@SensorRegistry.register("victron_vedirect")
class VictronVEDirectSensor(SensorBase):
    sensor_type = "victron_vedirect"
    _settings_schema: ClassVar = [
        {
            "key": "serial_device",
            "type": "string",
            "label": "VE.Direct serial device",
            "default": "",
            "description": "Prefer /dev/serial/by-id/; never share a reader",
        },
        {
            "key": "read_timeout_seconds",
            "type": "number",
            "label": "Read deadline (seconds)",
            "default": 1.5,
            "min": 0.05,
            "max": 2.0,
        },
    ]

    def __init__(self, name, config=None, log=None):
        super().__init__(name, config, log)
        if not isinstance(self.settings, dict):
            raise ValueError("serial_device settings must be a mapping")  # noqa: TRY004 - config validation
        self.serial_device = self.settings.get("serial_device")
        if (
            not isinstance(self.serial_device, str)
            or not self.serial_device.startswith("/")
            or self.serial_device == "/"
            or len(self.serial_device) > 1024
            or any(ord(c) < 32 for c in self.serial_device)
            or ".." in self.serial_device.split("/")
        ):
            raise ValueError("serial_device must be a local absolute device path")
        raw_timeout = self.settings.get("read_timeout_seconds", 1.5)
        try:
            self.read_timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            raise ValueError("read_timeout_seconds must be between 0.05 and 2.0") from None
        if isinstance(raw_timeout, bool) or not 0.05 <= self.read_timeout_seconds <= 2.0:
            raise ValueError("read_timeout_seconds must be between 0.05 and 2.0")

        self._seen = set()
        self._data = {}
        self._load_power_seen = False
        self._serial = None
        if self.ensure_python_modules([("serial", "pyserial")]):
            try:
                self._serial = importlib.import_module("serial")
            except ImportError:
                self.log.warning(
                    "pyserial unavailable; install pyserial in the service environment"
                )

    def _reading_metrics(self, ok):
        metrics = []
        for field in sorted(self._seen):
            key, divisor, unit = FIELDS[field]
            available = ok and self._data.get(key) is not None
            label = {
                "CS": "Charge state",
                "ERR": "Error",
                "MPPT": "Tracker state",
                "MON": "Monitor mode",
                "OR": "Off reason",
                "AR": "Alarm reason",
            }.get(field, key.replace("_", " ").title())
            if available and (field in ENUM_VALUES or field in MASK_VALUES):
                label = f"{label} — {status_display_value(field, self._data[key])}"
            descriptor = {
                "id": "/" + field,
                "source_path": "/" + field,
                "data_key": key,
                "label": label,
                "unit": unit,
                "kind": "boolean" if divisor is None else "number",
                "category": "status" if unit is None else "measurement",
                "available": available,
            }
            if not available:
                descriptor["reason"] = (
                    "read_failed" if not ok else "invalid" if key in self._data else "missing"
                )
            metrics.append(descriptor)
        if self._load_power_seen:
            available = ok and self._data.get("load_power_w") is not None
            descriptor = {
                "id": "/derived/load_power_w",
                "source_path": "/derived/load_power_w",
                "data_key": "load_power_w",
                "label": "Load power (calculated)",
                "unit": "W",
                "kind": "number",
                "category": "measurement",
                "available": available,
            }
            if not available:
                descriptor["reason"] = (
                    "read_failed"
                    if not ok
                    else "invalid"
                    if "load_power_w" in self._data
                    else "missing"
                )
            metrics.append(descriptor)
        return metrics

    def _read(self):
        self._data = {}
        if self._serial is None:
            raise RuntimeError("pyserial unavailable; install pyserial in the service environment")
        try:
            self._data = self._read_serial()
        except ImportError:
            raise RuntimeError(
                "pyserial unavailable; install pyserial in the service environment"
            ) from None
        except (OSError, ValueError):
            raise RuntimeError(
                "VE.Direct serial transport unavailable; check device and permissions"
            ) from None
        # Derived after merging this poll's validated blocks, never across polls.
        if "battery_voltage_v" in self._data and "load_current_a" in self._data:
            self._load_power_seen = True
            voltage = self._data["battery_voltage_v"]
            current = self._data["load_current_a"]
            # mV × mA resolves to micro-watts; discard only binary float noise.
            self._data["load_power_w"] = (
                round(voltage * current, 6) if voltage is not None and current is not None else None
            )
        return self._data

    def _read_serial(self):
        serial = self._serial
        parser = VEDirectParser()
        data = {}
        received_frame = False
        with serial.Serial(
            port=self.serial_device,
            baudrate=19200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=min(0.1, self.read_timeout_seconds),
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            exclusive=True,
        ) as port:
            # The read budget starts once the port is open, and the first read is
            # always made: a slow open must not use up the time meant for reading.
            deadline = time.monotonic() + self.read_timeout_seconds
            remaining_bytes = 16384
            while remaining_bytes:
                port.timeout = min(0.1, max(0, deadline - time.monotonic()))
                payload = port.read(min(256, remaining_bytes))
                remaining_bytes -= len(payload)
                frames = parser.feed(payload)
                for fields in frames:
                    received_frame = True
                    self._seen.update(set(fields) & FIELDS.keys())
                    data.update(normalize(fields))
                if time.monotonic() >= deadline:
                    break
        if received_frame:
            return data
        raise RuntimeError("No valid VE.Direct text frame before read limit")
