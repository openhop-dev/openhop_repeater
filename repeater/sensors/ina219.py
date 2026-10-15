"""
INA219 current/voltage/power monitor sensor plug-in.

Requires: pip install smbus2

Config example:
  - type: ina219
    name: "power_monitor"
    enabled: true
    auto_install_packages: false
    settings:
      i2c_address: 0x40  # Default INA219 I2C address
            bus_number: 1      # I2C bus number (1 for Raspberry Pi default)
      max_expected_amps: 2.0
      shunt_ohms: 0.1  # 0.1 Ohm shunt resistor
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

# INA219 register map
_REG_CONFIG = 0x00
_REG_SHUNT_VOLTAGE = 0x01
_REG_BUS_VOLTAGE = 0x02
_REG_POWER = 0x03
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05

# 32V range, 320mV shunt range, 12-bit ADC, continuous shunt+bus conversion
_CONFIG_32V_320MV_CONTINUOUS = 0x399F


@SensorRegistry.register("ina219")
class INA219Sensor(SensorBase):
    sensor_type = "ina219"

    _settings_schema = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x40",
            "help": "Hex I2C address (e.g. 0x40, 0x41, 0x44, 0x45)",
        },
        {
            "key": "bus_number",
            "type": "integer",
            "label": "I2C Bus Number",
            "default": 1,
            "help": "Linux I2C bus number",
        },
        {
            "key": "max_expected_amps",
            "type": "number",
            "label": "Max Expected Amps",
            "default": 2.0,
            "help": "Maximum expected current for calibration",
        },
        {
            "key": "shunt_ohms",
            "type": "number",
            "label": "Shunt Resistor (Ω)",
            "default": 0.1,
            "help": "Shunt resistor value in ohms",
        },
    ]

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        addr = self.settings.get("i2c_address", 0x40)
        self.i2c_address = int(addr, 0) if isinstance(addr, str) else int(addr)
        self.bus_number = int(self.settings.get("bus_number", 1))
        self.max_expected_amps = float(self.settings.get("max_expected_amps", 2.0))
        self.shunt_ohms = float(self.settings.get("shunt_ohms", 0.1))

        # INA219 calibration math from datasheet
        if self.max_expected_amps <= 0:
            self.max_expected_amps = 2.0
        if self.shunt_ohms <= 0:
            self.shunt_ohms = 0.1

        self.current_lsb = self.max_expected_amps / 32768.0
        cal = int(0.04096 / (self.current_lsb * self.shunt_ohms))
        self.calibration = max(1, min(cal, 0xFFFF))
        self.power_lsb = self.current_lsb * 20.0

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

            # Verify bus is accessible and program sensor once
            bus = smbus2.SMBus(self.bus_number)
            try:
                self._write_register(bus, _REG_CONFIG, _CONFIG_32V_320MV_CONTINUOUS)
                self._write_register(bus, _REG_CALIBRATION, self.calibration)
            finally:
                bus.close()

            self.available = True
            self.log.info(
                "INA219 initialized (addr=0x%02X, bus=%d, shunt=%.3fΩ, max_A=%.1f)",
                self.i2c_address,
                self.bus_number,
                self.shunt_ohms,
                self.max_expected_amps,
            )
        except Exception as exc:
            self.log.warning(
                "INA219 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )
            self.available = False

    @staticmethod
    def _swap_word(value: int) -> int:
        return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

    def _write_register(self, bus, register: int, value: int) -> None:
        bus.write_word_data(self.i2c_address, register, self._swap_word(value & 0xFFFF))

    def _read_register(self, bus, register: int) -> int:
        return self._swap_word(bus.read_word_data(self.i2c_address, register))

    @staticmethod
    def _to_signed_16(value: int) -> int:
        return value - 0x10000 if value & 0x8000 else value

    def _read(self) -> Dict[str, Any]:
        """Read voltage, current, and power from INA219."""
        if not self.available:
            raise RuntimeError("INA219 device not available")

        try:
            bus = self._smbus2.SMBus(self.bus_number)
            try:
                # Reapply calibration in case the chip was reset externally.
                self._write_register(bus, _REG_CALIBRATION, self.calibration)

                raw_bus = self._read_register(bus, _REG_BUS_VOLTAGE)
                raw_shunt = self._to_signed_16(self._read_register(bus, _REG_SHUNT_VOLTAGE))
                raw_current = self._to_signed_16(self._read_register(bus, _REG_CURRENT))
                raw_power = self._read_register(bus, _REG_POWER)
            finally:
                bus.close()

            bus_voltage_v = ((raw_bus >> 3) & 0x1FFF) * 0.004
            shunt_voltage_v = raw_shunt * 0.00001
            current_ma = raw_current * self.current_lsb * 1000.0
            power_mw = raw_power * self.power_lsb * 1000.0

            return {
                "bus_voltage_v": round(bus_voltage_v, 3),
                "shunt_voltage_v": round(shunt_voltage_v, 5),
                "current_ma": round(current_ma, 2),
                "power_mw": round(power_mw, 2),
            }
        except Exception as exc:
            raise RuntimeError(f"INA219 read failed: {exc}") from exc
