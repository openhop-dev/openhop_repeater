"""
SHTC3 temperature and humidity sensor plug-in (RAK1901 WisBlock sensor).

Requires: pip install smbus2

The SHTC3 uses 16-bit command words and requires a raw I2C read (no register
byte prefix), so smbus2.i2c_rdwr is used instead of the standard SMBus API.

Config example:
  - type: shtc3
    name: "ambient"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x70  # SHTC3 fixed I2C address
      bus_number: 1       # I2C bus number (1 for Raspberry Pi default)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# SHTC3 two-byte command words
_CMD_WAKE = [0x35, 0x17]
_CMD_MEAS = [0x7C, 0xA2]  # T-first, normal power mode
_CMD_SLEEP = [0xB0, 0x98]


@SensorRegistry.register("shtc3")
class SHTC3Sensor(SensorBase):
    sensor_type = "shtc3"

    _settings_schema = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x70",
            "help": "Hex I2C address (e.g. 0x70)",
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

        addr = self.settings.get("i2c_address", 0x70)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))

        self.available = False

        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2

            # Verify sensor is reachable: wake then immediately sleep
            bus = smbus2.SMBus(self.bus_number)
            try:
                bus.i2c_rdwr(smbus2.i2c_msg.write(self.i2c_address, _CMD_WAKE))
                time.sleep(0.002)
                bus.i2c_rdwr(smbus2.i2c_msg.write(self.i2c_address, _CMD_SLEEP))
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "SHTC3 initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )
        except Exception as exc:
            self.log.warning(
                "SHTC3 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )
            self.available = False

    def _read(self) -> Dict[str, Any]:
        """Read temperature and humidity from SHTC3."""
        if not self.available:
            raise RuntimeError("SHTC3 device not available")

        smbus2 = self._smbus2
        bus = smbus2.SMBus(self.bus_number)
        try:
            bus.i2c_rdwr(smbus2.i2c_msg.write(self.i2c_address, _CMD_WAKE))
            time.sleep(0.002)
            bus.i2c_rdwr(smbus2.i2c_msg.write(self.i2c_address, _CMD_MEAS))
            time.sleep(0.02)  # measurement takes ~12 ms in normal power mode

            r = smbus2.i2c_msg.read(self.i2c_address, 6)
            bus.i2c_rdwr(r)
            data = list(r)

            bus.i2c_rdwr(smbus2.i2c_msg.write(self.i2c_address, _CMD_SLEEP))

            # Bytes: T_MSB, T_LSB, T_CRC, RH_MSB, RH_LSB, RH_CRC
            t_raw = (data[0] << 8) | data[1]
            rh_raw = (data[3] << 8) | data[4]
            temp_c = round(-45.0 + 175.0 * t_raw / 65536.0, 2)
            temp_f = round(temp_c * 9.0 / 5.0 + 32.0, 2)
            rh = round(100.0 * rh_raw / 65536.0, 2)

            return {
                "temperature_c": temp_c,
                "temperature_f": temp_f,
                "humidity_pct": rh,
            }
        except Exception as exc:
            raise RuntimeError(f"SHTC3 read failed: {exc}") from exc
        finally:
            bus.close()
