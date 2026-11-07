"""
Waveshare UPS HAT (E) battery monitor plug-in — BMS MCU at I2C 0x2D.

The HAT (E) uses a dedicated BMS chip (not an INA219) that exposes charge
state, pack voltage/current, per-cell voltages, remaining capacity in mAh,
and time-to-empty / time-to-full estimates directly via I2C registers.

Requires: pip install smbus2

Config example:
  - type: waveshare_ups_e
    name: "battery"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x2D  # Waveshare UPS HAT (E) fixed address
      bus_number: 1       # I2C bus number (1 for Raspberry Pi default)
      low_cell_mv: 3150   # Per-cell warning threshold in mV
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# Register map
_REG_STATUS = 0x02  # 1 byte  — charge state flags
_REG_VBUS = 0x10  # 6 bytes — input (VBUS) voltage, current, power
_REG_BATT = 0x20  # 12 bytes — pack voltage, current, percent, mAh, time
_REG_CELLS = 0x30  # 8 bytes — four cell voltages (LE uint16 each)

# Charge state flag bits
_FLAG_FAST_CHARGE = 0x40
_FLAG_CHARGING = 0x80
_FLAG_DISCHARGING = 0x20


def _u16le(data: list, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


@SensorRegistry.register("waveshare_ups_e")
class WaveshareUpsESensor(SensorBase):
    sensor_type = "waveshare_ups_e"

    _settings_schema = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x2D",
            "help": "Hex I2C address (e.g. 0x2D)",
        },
        {
            "key": "bus_number",
            "type": "integer",
            "label": "I2C Bus Number",
            "default": 1,
            "help": "Linux I2C bus number",
        },
        {
            "key": "low_cell_mv",
            "type": "integer",
            "label": "Low Cell Threshold (mV)",
            "default": 3150,
            "help": "Per-cell voltage warning threshold in mV",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        addr = self.settings.get("i2c_address", 0x2D)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))
        self.low_cell_mv = int(self.settings.get("low_cell_mv", 3150))

        self.available = False

        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2

            bus = smbus2.SMBus(self.bus_number)
            try:
                bus.read_i2c_block_data(self.i2c_address, _REG_STATUS, 1)
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "Waveshare UPS HAT (E) initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )
        except Exception as exc:
            self.log.warning(
                "Waveshare UPS HAT (E) init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )

    def _read(self) -> Dict[str, Any]:
        """Read all battery state from the HAT (E) BMS."""
        if not self.available:
            raise RuntimeError("Waveshare UPS HAT (E) not available")

        try:
            bus = self._smbus2.SMBus(self.bus_number)
            try:
                status = bus.read_i2c_block_data(self.i2c_address, _REG_STATUS, 1)[0]
                vb = bus.read_i2c_block_data(self.i2c_address, _REG_VBUS, 6)
                bd = bus.read_i2c_block_data(self.i2c_address, _REG_BATT, 12)
                cd = bus.read_i2c_block_data(self.i2c_address, _REG_CELLS, 8)
            finally:
                bus.close()

            # Charge state
            if status & _FLAG_FAST_CHARGE:
                charge_state = "fast_charging"
            elif status & _FLAG_CHARGING:
                charge_state = "charging"
            elif status & _FLAG_DISCHARGING:
                charge_state = "discharging"
            else:
                charge_state = "idle"

            # VBUS (input power from charger)
            vbus_voltage_mv = _u16le(vb, 0)
            vbus_current_ma = _u16le(vb, 2)
            vbus_power_mw = _u16le(vb, 4)

            # Battery pack
            batt_voltage_mv = _u16le(bd, 0)
            batt_current_ma = _u16le(bd, 2)
            if batt_current_ma > 0x7FFF:  # signed 16-bit
                batt_current_ma -= 0x10000
            batt_percent = _u16le(bd, 4)
            remaining_mah = _u16le(bd, 6)
            time_remaining = _u16le(bd, 8)
            time_to_full = _u16le(bd, 10)

            # Per-cell voltages (4 cells)
            cells_mv = [
                _u16le(cd, 0),
                _u16le(cd, 2),
                _u16le(cd, 4),
                _u16le(cd, 6),
            ]

            result: Dict[str, Any] = {
                "charge_state": charge_state,
                "battery_voltage_mv": batt_voltage_mv,
                "battery_current_ma": batt_current_ma,
                "battery_percent": batt_percent,
                "remaining_capacity_mah": remaining_mah,
                "vbus_voltage_mv": vbus_voltage_mv,
                "vbus_current_ma": vbus_current_ma,
                "vbus_power_mw": vbus_power_mw,
                "cell_voltages_mv": cells_mv,
                "low_cell_warning": any(0 < v < self.low_cell_mv for v in cells_mv),
            }

            # Only include whichever time estimate is relevant
            if batt_current_ma < 0:
                result["time_to_empty_min"] = time_remaining
            else:
                result["time_to_full_min"] = time_to_full

            return result

        except Exception as exc:
            raise RuntimeError(f"Waveshare UPS HAT (E) read failed: {exc}") from exc
