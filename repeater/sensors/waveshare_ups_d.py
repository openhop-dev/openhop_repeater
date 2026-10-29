"""
Waveshare UPS HAT (D) battery monitor plug-in — INA219 at I2C 0x43.

Reads a single 21700 Li-ion cell via INA219. Reports voltage, current,
power, battery percent, and charge state.

Requires: pip install smbus2

Config example:
  - type: waveshare_ups_d
    name: "battery"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x43  # Waveshare UPS HAT (D) fixed address
      bus_number: 1       # I2C bus number (1 for Raspberry Pi default)
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
_REG_CAL = 0x05

# 32V range, ±320mV gain, 128-sample averaging, continuous shunt+bus
_CONFIG_VALUE = 0x3FFF

# Waveshare UPS HAT (D) calibration — 0.01Ω shunt (per Waveshare sample code)
# current_lsb ≈ 0.1524 mA/LSB, power_lsb = current_lsb × 20
_CAL_VALUE = 26868
_CURRENT_LSB = 0.0001524  # A per LSB
_POWER_LSB = _CURRENT_LSB * 20.0  # W per LSB


def _voltage_to_percent(v: float) -> int:
    """Piecewise linear SoC estimate for a single 21700 Li-ion cell (3.0–4.2 V)."""
    if v >= 4.20:
        return 100
    if v >= 4.00:
        return int(85 + (v - 4.00) / 0.20 * 15)
    if v >= 3.80:
        return int(60 + (v - 3.80) / 0.20 * 25)
    if v >= 3.70:
        return int(40 + (v - 3.70) / 0.10 * 20)
    if v >= 3.50:
        return int(15 + (v - 3.50) / 0.20 * 25)
    if v >= 3.00:
        return int((v - 3.00) / 0.50 * 15)
    return 0


@SensorRegistry.register("waveshare_ups_d")
class WaveshareUpsDSensor(SensorBase):
    sensor_type = "waveshare_ups_d"

    _settings_schema = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x43",
            "help": "Hex I2C address (e.g. 0x43)",
        },
        {
            "key": "bus_number",
            "type": "integer",
            "label": "I2C Bus Number",
            "default": 1,
            "help": "Linux I2C bus number",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        addr = self.settings.get("i2c_address", 0x43)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))

        self.available = False

        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2

            bus = smbus2.SMBus(self.bus_number)
            try:
                self._write(bus, _REG_CONFIG, _CONFIG_VALUE)
                self._write(bus, _REG_CAL, _CAL_VALUE)
                # 128-sample averaging takes ~68 ms; wait for first conversion
                time.sleep(0.15)
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "Waveshare UPS HAT (D) INA219 initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )
        except Exception as exc:
            self.log.warning(
                "Waveshare UPS HAT (D) init failed (addr=0x%02X, bus=%d): %s",
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
            raise RuntimeError("Waveshare UPS HAT (D) not available")

        try:
            bus = self._smbus2.SMBus(self.bus_number)
            try:
                # Re-apply calibration in case of external reset
                self._write(bus, _REG_CAL, _CAL_VALUE)

                bus_v = (self._read_u(bus, _REG_BUS) >> 3) * 4 / 1000.0
                shunt_mv = self._read_s(bus, _REG_SHUNT) * 0.01
                current_ma = self._read_s(bus, _REG_CURRENT) * _CURRENT_LSB * 1000.0
                power_mw = self._read_u(bus, _REG_POWER) * _POWER_LSB * 1000.0
            finally:
                bus.close()

            pct = _voltage_to_percent(bus_v)

            # HAT (D) sign convention: negative = charging, positive = discharging
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
            raise RuntimeError(f"Waveshare UPS HAT (D) read failed: {exc}") from exc
