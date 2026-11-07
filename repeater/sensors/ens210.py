"""
ENS210 relative humidity and temperature sensor plug-in.

Requires: pip install smbus2

Config example:
  - type: ens210
    name: "ambient"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x43   # Default ENS210 I2C address
      bus_number: 1        # I2C bus number (1 for Raspberry Pi default)
      read_timeout_seconds: 1.0  # Max time to wait for valid data (polls every 50 ms)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# ENS210 register addresses
_REG_SENS_RUN = 0x21
_REG_SENS_START = 0x22
_REG_T_VAL = 0x30
_REG_H_VAL = 0x33


@SensorRegistry.register("ens210")
class ENS210Sensor(SensorBase):
    sensor_type = "ens210"

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
        {
            "key": "read_timeout_seconds",
            "type": "number",
            "label": "Read Timeout (s)",
            "default": 1.0,
            "help": "Max time to wait for measurement",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        addr = self.settings.get("i2c_address", 0x43)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))
        self._poll_interval = 0.05  # 50 ms between validity checks
        self._poll_attempts = max(
            1, int(float(self.settings.get("read_timeout_seconds", 1.0)) / self._poll_interval)
        )

        self.available = False
        if not self.ensure_python_modules(
            [
                ("smbus2", "smbus2"),
            ]
        ):
            return

        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2
            # Verify the bus is accessible
            smbus2.SMBus(self.bus_number).close()
            self.available = True
            self.log.info(
                "ENS210 initialized (addr=0x%02X, bus=%d)",
                self.i2c_address,
                self.bus_number,
            )
        except Exception as exc:
            self.log.warning(
                "ENS210 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )
            self.available = False

    def _read(self) -> Dict[str, Any]:
        """Read temperature and humidity from ENS210."""
        if not self.available:
            raise RuntimeError("ENS210 device not available")

        bus = self._smbus2.SMBus(self.bus_number)
        try:
            bus.write_byte_data(self.i2c_address, _REG_SENS_RUN, 0x03)
            bus.write_byte_data(self.i2c_address, _REG_SENS_START, 0x03)

            for _ in range(self._poll_attempts):
                time.sleep(self._poll_interval)
                t_data = bus.read_i2c_block_data(self.i2c_address, _REG_T_VAL, 3)
                h_data = bus.read_i2c_block_data(self.i2c_address, _REG_H_VAL, 3)
                if (t_data[2] & 0x01) and (h_data[2] & 0x01):
                    break
            else:
                raise RuntimeError(
                    f"ENS210 measurement timed out after {self._poll_attempts * self._poll_interval:.1f}s"
                )

            t_raw = t_data[0] | (t_data[1] << 8)
            h_raw = h_data[0] | (h_data[1] << 8)

            return {
                "temperature_c": round(t_raw / 64.0 - 273.15, 2),
                "humidity_pct": round(h_raw / 512.0, 2),
            }
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"ENS210 read failed: {exc}") from exc
        finally:
            bus.close()
