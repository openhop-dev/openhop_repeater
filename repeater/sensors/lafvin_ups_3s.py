"""
LAFVIN UPS Module 3S battery monitor plug-in — INA219 at I2C 0x41.

Reads a 3S lithium-ion/LiPo battery pack via INA219. Reports pack voltage,
current, power, estimated state-of-charge, and charge state.

The INA219 address is 0x41 by default (A0 bridged to VCC, A1 to GND on the
LAFVIN board) but is configurable for boards wired differently.

Requires: pip install smbus2

Config example:
  - type: lafvin_ups_3s
    name: "battery"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x41   # Default LAFVIN UPS 3S address
      bus_number: 1        # I2C bus (1 for Raspberry Pi default)
      shunt_ohms: 0.1      # Shunt resistor value in ohms
      max_amps: 5.0        # Maximum expected current in amps
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# INA219 register addresses
_REG_CONFIG = 0x00
_REG_SHUNT = 0x01
_REG_BUS = 0x02
_REG_POWER = 0x03
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05

# 32V range, ±320mV shunt gain, 12-bit ADC, continuous shunt+bus
_CONFIG_VALUE = 0x399F

# 3S LiPo/Li-ion pack voltage thresholds (3 cells in series)
_V_MAX = 12.6  # 4.20 V/cell × 3 — fully charged
_V_MIN = 9.0  # 3.00 V/cell × 3 — cutoff


def _pack_voltage_to_percent(v: float) -> int:
    """Piecewise linear SoC estimate for a 3S Li-ion/LiPo pack (9.0–12.6 V)."""
    cell = v / 3.0
    if cell >= 4.20:
        return 100
    if cell >= 4.00:
        return int(85 + (cell - 4.00) / 0.20 * 15)
    if cell >= 3.80:
        return int(60 + (cell - 3.80) / 0.20 * 25)
    if cell >= 3.70:
        return int(40 + (cell - 3.70) / 0.10 * 20)
    if cell >= 3.50:
        return int(15 + (cell - 3.50) / 0.20 * 25)
    if cell >= 3.00:
        return int((cell - 3.00) / 0.50 * 15)
    return 0


@SensorRegistry.register("lafvin_ups_3s")
class LafvinUps3sSensor(SensorBase):
    sensor_type = "lafvin_ups_3s"

    _settings_schema = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x41",
            "help": "Hex I2C address (e.g. 0x41)",
        },
        {
            "key": "bus_number",
            "type": "integer",
            "label": "I2C Bus Number",
            "default": 1,
            "help": "Linux I2C bus number",
        },
        {
            "key": "shunt_ohms",
            "type": "number",
            "label": "Shunt Resistor (Ω)",
            "default": 0.1,
            "help": "Shunt resistor value in ohms",
        },
        {
            "key": "max_amps",
            "type": "number",
            "label": "Max Amps",
            "default": 5.0,
            "help": "Maximum expected current in amps",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        addr = self.settings.get("i2c_address", 0x41)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))
        self.shunt_ohms = float(self.settings.get("shunt_ohms", 0.1))
        self.max_amps = float(self.settings.get("max_amps", 5.0))

        # INA219 calibration per datasheet
        self.current_lsb = self.max_amps / 32768.0
        cal = int(0.04096 / (self.current_lsb * self.shunt_ohms))
        self.calibration = max(1, min(cal, 0xFFFF))
        self.power_lsb = self.current_lsb * 20.0

        self.available = False

        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2

            bus = smbus2.SMBus(self.bus_number)
            try:
                self._write(bus, _REG_CONFIG, _CONFIG_VALUE)
                self._write(bus, _REG_CALIBRATION, self.calibration)
                time.sleep(0.15)
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "LAFVIN UPS 3S INA219 initialized (addr=0x%02X, bus=%d, shunt=%.3fΩ)",
                self.i2c_address,
                self.bus_number,
                self.shunt_ohms,
            )
        except Exception as exc:
            self.log.warning(
                "LAFVIN UPS 3S init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )

    def _write(self, bus, reg: int, val: int) -> None:
        bus.write_i2c_block_data(self.i2c_address, reg, [(val >> 8) & 0xFF, val & 0xFF])

    def _read_u(self, bus, reg: int) -> int:
        d = bus.read_i2c_block_data(self.i2c_address, reg, 2)
        return (d[0] << 8) | d[1]

    def _read_s(self, bus, reg: int) -> int:
        v = self._read_u(bus, reg)
        return v - 0x10000 if v & 0x8000 else v

    def _read(self) -> Dict[str, Any]:
        """Read voltage, current, power, and derived battery state."""
        if not self.available:
            raise RuntimeError("LAFVIN UPS 3S not available")

        try:
            bus = self._smbus2.SMBus(self.bus_number)
            try:
                self._write(bus, _REG_CALIBRATION, self.calibration)

                bus_v = (self._read_u(bus, _REG_BUS) >> 3) * 4 / 1000.0
                shunt_mv = self._read_s(bus, _REG_SHUNT) * 0.01
                current_ma = self._read_s(bus, _REG_CURRENT) * self.current_lsb * 1000.0
                power_mw = self._read_u(bus, _REG_POWER) * self.power_lsb * 1000.0
            finally:
                bus.close()

            pct = _pack_voltage_to_percent(bus_v)

            if current_ma < -50:
                state = "charging"
            elif current_ma > 50:
                state = "discharging"
            else:
                state = "idle"

            return {
                "bus_voltage_v": round(bus_v, 3),
                "shunt_voltage_mv": round(shunt_mv, 2),
                "current_ma": round(current_ma, 1),
                "power_mw": round(power_mw, 1),
                "battery_percent": pct,
                "charge_state": state,
            }
        except Exception as exc:
            raise RuntimeError(f"LAFVIN UPS 3S read failed: {exc}") from exc
