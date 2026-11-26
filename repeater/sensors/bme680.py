"""
BME680 temperature, humidity, pressure and gas-resistance sensor plug-in.

Same reading names as the bme280 plug-in (temperature_c, humidity_pct,
pressure_hpa) plus gas_resistance_ohm, so everything that understands a
BME280 reading (dashboard, telemetry) understands this one too.

Requires: smbus2 (already used by the bme280 plug-in)

Config example:
  - type: bme680
    name: env
    enabled: true
    settings:
      i2c_address: 0x76        # 0x77 when SDO is tied high
      bus_number: 1
      gas: true                # heat the gas plate each reading (about 150 ms)
      heater_temp_c: 320
      heater_ms: 150
      read_timeout_seconds: 1.0

Compensation follows the Bosch BME680 datasheet floating-point formulas.
Gas resistance is a raw value (higher = cleaner air); a true IAQ index needs
Bosch's closed BSEC library and is not computed here.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from .base import SensorBase
from .registry import SensorRegistry

_REG_ID = 0xD0
_CHIP_ID = 0x61
_REG_RESET = 0xE0
_REG_COEFF1 = 0x89  # 25 bytes
_REG_COEFF2 = 0xE1  # 16 bytes
_REG_RES_HEAT_VAL = 0x00
_REG_RES_HEAT_RANGE = 0x02
_REG_RANGE_SW_ERR = 0x04
_REG_CTRL_GAS_0 = 0x70
_REG_CTRL_GAS_1 = 0x71
_REG_CTRL_HUM = 0x72
_REG_CTRL_MEAS = 0x74
_REG_CONFIG = 0x75
_REG_RES_HEAT_0 = 0x5A
_REG_GAS_WAIT_0 = 0x64
_REG_FIELD0 = 0x1D  # status + press/temp/hum/gas, 15 bytes

_OS_X1, _OS_X2, _OS_X4 = 1, 2, 3
_MODE_FORCED = 0x01
_NEW_DATA = 0x80
_GAS_VALID = 0x20
_HEAT_STAB = 0x10

_K1 = (0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, -0.8, 0.0, 0.0, -0.2, -0.5, 0.0, -1.0, 0.0, 0.0)
_K2 = (0.0, 0.0, 0.0, 0.0, 0.1, 0.7, 0.0, -0.8, -0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _as_int(value: Any, default: int) -> int:
    """Accept 118, "118" or "0x76" (forms submit text)."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return int(value.strip(), 0)
    return int(value)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _s8(v: int) -> int:
    return v - 256 if v > 127 else v


def _s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def parse_calibration(c1, c2, heat_val, heat_range, sw_err) -> dict[str, Any]:
    """c1: 25 bytes from 0x89, c2: 16 bytes from 0xE1; others single registers."""
    c = list(c1) + list(c2)
    return {
        "t1": (c[34] << 8) | c[33],
        "t2": _s16((c[2] << 8) | c[1]),
        "t3": _s8(c[3]),
        "p1": (c[6] << 8) | c[5],
        "p2": _s16((c[8] << 8) | c[7]),
        "p3": _s8(c[9]),
        "p4": _s16((c[12] << 8) | c[11]),
        "p5": _s16((c[14] << 8) | c[13]),
        "p6": _s8(c[16]),
        "p7": _s8(c[15]),
        "p8": _s16((c[20] << 8) | c[19]),
        "p9": _s16((c[22] << 8) | c[21]),
        "p10": c[23],
        "h1": (c[27] << 4) | (c[26] & 0x0F),
        "h2": (c[25] << 4) | (c[26] >> 4),
        "h3": _s8(c[28]),
        "h4": _s8(c[29]),
        "h5": _s8(c[30]),
        "h6": c[31],
        "h7": _s8(c[32]),
        "gh1": _s8(c[37]),
        "gh2": _s16((c[36] << 8) | c[35]),
        "gh3": _s8(c[38]),
        "res_heat_val": _s8(heat_val),
        "res_heat_range": (heat_range & 0x30) >> 4,
        "range_sw_err": _s8(sw_err & 0xF0) // 16,
    }


def compensate(cal, adc_t, adc_p, adc_h):
    """Datasheet float formulas -> (temperature_c, pressure_pa, humidity_pct)."""
    var1 = (adc_t / 16384.0 - cal["t1"] / 1024.0) * cal["t2"]
    var2 = ((adc_t / 131072.0 - cal["t1"] / 8192.0) ** 2) * (cal["t3"] * 16.0)
    t_fine = var1 + var2
    temperature = t_fine / 5120.0

    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * (cal["p6"] / 131072.0)
    var2 = var2 + var1 * cal["p5"] * 2.0
    var2 = var2 / 4.0 + cal["p4"] * 65536.0
    var1 = ((cal["p3"] * var1 * var1) / 16384.0 + cal["p2"] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * cal["p1"]
    if var1 == 0:
        raise RuntimeError("pressure compensation divided by zero")
    pressure = 1048576.0 - adc_p
    pressure = ((pressure - var2 / 4096.0) * 6250.0) / var1
    var1 = cal["p9"] * pressure * pressure / 2147483648.0
    var2 = pressure * (cal["p8"] / 32768.0)
    var3 = (pressure / 256.0) ** 3 * (cal["p10"] / 131072.0)
    pressure = pressure + (var1 + var2 + var3 + cal["p7"] * 128.0) / 16.0

    var1 = adc_h - (cal["h1"] * 16.0 + (cal["h3"] / 2.0) * temperature)
    scale = (
        1.0
        + (cal["h4"] / 16384.0) * temperature
        + (cal["h5"] / 1048576.0) * temperature * temperature
    )
    var2 = var1 * ((cal["h2"] / 262144.0) * scale)
    var3 = cal["h6"] / 16384.0
    var4 = cal["h7"] / 2097152.0
    humidity = var2 + (var3 + var4 * temperature) * var2 * var2
    return temperature, pressure, min(100.0, max(0.0, humidity))


def heater_code(cal, target_c, ambient_c):
    var1 = cal["gh1"] / 16.0 + 49.0
    var2 = (cal["gh2"] / 32768.0) * 0.0005 + 0.00235
    var3 = cal["gh3"] / 1024.0
    var4 = var1 * (1.0 + var2 * target_c)
    var5 = var4 + var3 * ambient_c
    res = 3.4 * (
        var5 * (4.0 / (4.0 + cal["res_heat_range"])) * (1.0 / (1.0 + cal["res_heat_val"] * 0.002))
        - 25
    )
    return max(0, min(255, int(res)))


def wait_code(ms):
    ms = max(1, min(4032, int(ms)))
    factor = 0
    while ms > 63:
        ms //= 4
        factor += 1
    return (factor << 6) | ms


def gas_resistance(cal, adc_gas, gas_range):
    var1 = 1340.0 + 5.0 * cal["range_sw_err"]
    var2 = var1 * (1.0 + _K1[gas_range] / 100.0)
    var3 = 1.0 + _K2[gas_range] / 100.0
    return 1.0 / (var3 * 0.000000125 * (1 << gas_range) * ((adc_gas - 512.0) / var2 + 1.0))


@SensorRegistry.register("bme680")
class BME680Sensor(SensorBase):
    sensor_type = "bme680"

    _settings_schema: ClassVar = [
        {
            "key": "i2c_address",
            "type": "string",
            "label": "I2C Address",
            "default": "0x76",
            "help": "Hex I2C address (e.g. 0x76, 0x77)",
        },
        {
            "key": "bus_number",
            "type": "integer",
            "label": "I2C Bus Number",
            "default": 1,
            "help": "Linux I2C bus number",
        },
        {
            "key": "gas",
            "type": "integer",
            "label": "Gas Measurement",
            "default": 1,
            "help": "1 = heat the gas plate each reading, 0 = off",
        },
        {
            "key": "heater_temp_c",
            "type": "number",
            "label": "Heater Temperature (degC)",
            "default": 320,
            "help": "Gas plate target temperature",
        },
        {
            "key": "heater_ms",
            "type": "integer",
            "label": "Heater Time (ms)",
            "default": 150,
            "help": "Gas plate heating duration",
        },
        {
            "key": "read_timeout_seconds",
            "type": "number",
            "label": "Read Timeout (s)",
            "default": 1.0,
            "help": "Max time to wait for measurement",
        },
    ]

    def __init__(self, name: str, config: dict[str, Any] | None = None, log=None):
        super().__init__(name=name, config=config, log=log)
        self.i2c_address = _as_int(self.settings.get("i2c_address"), 0x76)
        self.bus_number = _as_int(self.settings.get("bus_number"), 1)
        self.gas = _as_bool(self.settings.get("gas"), True)
        self.heater_temp = float(self.settings.get("heater_temp_c") or 320)
        self.heater_ms = _as_int(self.settings.get("heater_ms"), 150)
        self._poll_interval = 0.01
        self._poll_attempts = max(
            1, int(float(self.settings.get("read_timeout_seconds", 1.0)) / self._poll_interval)
        )
        self._cal: dict[str, Any] | None = None
        self._last_temp = 25.0
        self.available = False
        if not self.ensure_python_modules([("smbus2", "smbus2")]):
            return
        try:
            import smbus2  # type: ignore[import-not-found]

            self._smbus2 = smbus2
            bus = smbus2.SMBus(self.bus_number)
            try:
                chip_id = bus.read_byte_data(self.i2c_address, _REG_ID)
                if chip_id != _CHIP_ID:
                    hint = " (0x60 is a BME280: use type bme280)" if chip_id == 0x60 else ""
                    raise RuntimeError(f"unexpected chip id 0x{chip_id:02X}{hint}")
                self._cal = parse_calibration(
                    bus.read_i2c_block_data(self.i2c_address, _REG_COEFF1, 25),
                    bus.read_i2c_block_data(self.i2c_address, _REG_COEFF2, 16),
                    bus.read_byte_data(self.i2c_address, _REG_RES_HEAT_VAL),
                    bus.read_byte_data(self.i2c_address, _REG_RES_HEAT_RANGE),
                    bus.read_byte_data(self.i2c_address, _REG_RANGE_SW_ERR),
                )
            finally:
                bus.close()
            self.available = True
            self.log.info(
                "BME680 initialized (addr=0x%02X, bus=%d, gas=%s)",
                self.i2c_address,
                self.bus_number,
                self.gas,
            )
        except Exception as exc:  # noqa: BLE001 - a missing sensor must not stop the daemon
            self.log.warning(
                "BME680 init failed (addr=0x%02X, bus=%d): %s",
                self.i2c_address,
                self.bus_number,
                exc,
            )
            self.available = False

    def _measure(self, bus):
        a = self.i2c_address
        bus.write_byte_data(a, _REG_CTRL_HUM, _OS_X2)
        bus.write_byte_data(a, _REG_CONFIG, 0x00)  # IIR filter off
        if self.gas:
            code = heater_code(self._cal, self.heater_temp, self._last_temp)
            bus.write_byte_data(a, _REG_RES_HEAT_0, code)
            bus.write_byte_data(a, _REG_GAS_WAIT_0, wait_code(self.heater_ms))
            bus.write_byte_data(a, _REG_CTRL_GAS_1, 0x10)  # run_gas, heater profile 0
            bus.write_byte_data(a, _REG_CTRL_GAS_0, 0x00)  # heater on
        else:
            bus.write_byte_data(a, _REG_CTRL_GAS_1, 0x00)
            bus.write_byte_data(a, _REG_CTRL_GAS_0, 0x08)  # heater off
        bus.write_byte_data(a, _REG_CTRL_MEAS, (_OS_X2 << 5) | (_OS_X4 << 2) | _MODE_FORCED)
        attempts = self._poll_attempts + (self.heater_ms // 10 if self.gas else 0)
        for _ in range(attempts):
            time.sleep(self._poll_interval)
            if bus.read_byte_data(a, _REG_FIELD0) & _NEW_DATA:
                break
        else:
            raise RuntimeError("measurement timed out")
        return bus.read_i2c_block_data(a, _REG_FIELD0, 15)

    def _read(self) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("BME680 device not available")
        bus = self._smbus2.SMBus(self.bus_number)
        try:
            d = self._measure(bus)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"BME680 read failed: {exc}") from exc
        finally:
            bus.close()
        adc_p = (d[2] << 12) | (d[3] << 4) | (d[4] >> 4)
        adc_t = (d[5] << 12) | (d[6] << 4) | (d[7] >> 4)
        adc_h = (d[8] << 8) | d[9]
        temperature, pressure, humidity = compensate(self._cal, adc_t, adc_p, adc_h)
        self._last_temp = temperature
        result = {
            "temperature_c": round(temperature, 2),
            "humidity_pct": round(humidity, 2),
            "pressure_hpa": round(pressure / 100.0, 2),
        }
        if self.gas:
            msb, lsb = d[13], d[14]
            if lsb & _GAS_VALID and lsb & _HEAT_STAB:
                adc_gas = (msb << 2) | (lsb >> 6)
                result["gas_resistance_ohm"] = round(gas_resistance(self._cal, adc_gas, lsb & 0x0F))
        return result
