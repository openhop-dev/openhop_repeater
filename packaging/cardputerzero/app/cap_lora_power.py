#!/usr/bin/env python3
"""Power and antenna-switch control for the M5Stack Cap LoRa-1262 on CardputerZero.

openHop drives the SX1262 itself, but two things on this hardware sit outside
the radio and must be set up before it starts:

  1. The Cap's 5V rail, exposed by the CardputerZero BSP as an LED class
     device (/sys/class/leds/ext_5v_out).
  2. The antenna switch enable (SX_ANT_SW), wired to P0 of a PI4IOE5V6408
     I/O expander at I2C address 0x43 on /dev/i2c-1. P0 must be driven high.

Usage:  cap_lora_power.py on | off | status

Uses only the standard library and needs no root: the CardputerZero image
gives the gpio group write access to the rail and the i2c group the bus.
"""

import fcntl
import os
import sys
import time

RAIL = "/sys/class/leds/ext_5v_out/brightness"
I2C_BUS = "/dev/i2c-1"
I2C_ADDR = 0x43
I2C_SLAVE = 0x0703

# PI4IOE5V6408 registers (datasheet layout; 0x01 is the device ID, 0xA0)
REG_ID = 0x01
REG_DIRECTION = 0x03  # 1 = output
REG_OUTPUT = 0x05  # output level
REG_HIGH_Z = 0x07  # 1 = high impedance (power-on default 0xFF)
P0 = 0x01
SETTLE_S = 0.15


class Expander:
    def __init__(self):
        self.fd = os.open(I2C_BUS, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, I2C_ADDR)

    def read(self, reg):
        os.write(self.fd, bytes([reg]))
        return os.read(self.fd, 1)[0]

    def write(self, reg, value):
        os.write(self.fd, bytes([reg, value & 0xFF]))

    def close(self):
        os.close(self.fd)


def rail_get():
    with open(RAIL) as f:
        return int(f.read().strip() or 0)


def rail_set(on):
    with open(RAIL, "w") as f:
        f.write("1" if on else "0")


def power_on():
    if rail_get() <= 0:
        rail_set(True)
        time.sleep(SETTLE_S)
    if rail_get() <= 0:
        raise RuntimeError(f"5V rail did not turn on ({RAIL})")
    x = Expander()
    try:
        dev_id = x.read(REG_ID)
        if dev_id & 0xE0 != 0xA0:
            raise RuntimeError(f"unexpected I/O expander ID 0x{dev_id:02X} at 0x{I2C_ADDR:02X}")
        # Set the level first so P0 never drives low when it becomes an output.
        x.write(REG_OUTPUT, x.read(REG_OUTPUT) | P0)
        x.write(REG_DIRECTION, x.read(REG_DIRECTION) | P0)
        x.write(REG_HIGH_Z, x.read(REG_HIGH_Z) & ~P0)
        if not (x.read(REG_OUTPUT) & P0) or (x.read(REG_HIGH_Z) & P0):
            raise RuntimeError("antenna switch enable (P0) did not latch high")
    finally:
        x.close()


def power_off():
    try:
        x = Expander()
        try:
            x.write(REG_HIGH_Z, x.read(REG_HIGH_Z) | P0)
            x.write(REG_DIRECTION, x.read(REG_DIRECTION) & ~P0)
        finally:
            x.close()
    except OSError:
        pass  # Cap removed or rail already off
    rail_set(False)


def status():
    rail = rail_get() > 0
    print(f"5V rail: {'on' if rail else 'off'}")
    try:
        x = Expander()
        try:
            out, direction, hiz = x.read(REG_OUTPUT), x.read(REG_DIRECTION), x.read(REG_HIGH_Z)
        finally:
            x.close()
        enabled = bool(out & P0) and bool(direction & P0) and not (hiz & P0)
        print(f"Cap LoRa-1262: detected, antenna switch {'enabled' if enabled else 'disabled'}")
        return 0 if rail and enabled else 1
    except OSError:
        print("Cap LoRa-1262: not detected (is the Cap attached and the rail on?)")
        return 2


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "on":
            power_on()
        elif cmd == "off":
            power_off()
        elif cmd == "status":
            return status()
        else:
            print(__doc__)
            return 64
    except (OSError, RuntimeError) as e:
        print(f"cap_lora_power: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
