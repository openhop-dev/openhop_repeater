# openHop Repeater for the M5Stack CardputerZero

Packages openHop Repeater for the M5Stack **CardputerZero** with the
**Cap LoRa-1262**, as `openhop-cz`, an arm64 `.deb` for the CardputerZero app
store. Publishing to the store is described in [PUBLISHING.md](PUBLISHING.md).

## What the package does

- Bundles openHop Repeater and its Python dependencies in a private runtime
  under `/opt/openhop-cz/venv`. Nothing is downloaded at install time.
- Runs it as a systemd service, `openhop-cz.service`, under the unprivileged
  `openhop` user. It is off until started from the app.
- Adds an **openHop** app to the launcher: start/stop, region picker,
  start-at-boot toggle, and the web dashboard address (`http://<device-ip>:8000`).
- Everything else (node name, TX power, passwords, policies) is configured in
  openHop's own web dashboard. Its setup wizard offers one hardware choice,
  the Cap LoRa-1262.

## Hardware

Verified on a CardputerZero (Debian trixie, Python 3.13) with a Cap LoRa-1262:

| Signal | Value |
|---|---|
| SPI | `/dev/spidev0.1` (`bus_id: 0`, `cs_id: 1`, kernel chip select) |
| IRQ / RESET / BUSY | GPIO 23 / 26 / 22 |
| TCXO | DIO3, 3.0 V |
| RF switch | DIO2 |
| Cap 5V rail | `/sys/class/leds/ext_5v_out/brightness` |
| Antenna switch enable | PI4IOE5V6408 at I2C `0x43` on `/dev/i2c-1`, P0 high |

The pins are the `cardputerzero-cap-lora-1262` entry of the repository's
`radio-settings.json`. The 5V rail and the antenna switch are outside
openHop's hardware model, so `app/cap_lora_power.py` switches them on before
the service starts and off after it stops. The expander uses the datasheet
register layout (`0x03` direction, `0x05` output, `0x07` high-Z); register
`0x01` reads back the device ID `0xA0`.

Do not hot-swap the Cap, and attach the antenna before starting the repeater.
The M5 LoRa and GPS apps cannot use the radio while the repeater is running.

## Layout

- `app/cap_lora_power.py` - Cap 5V rail and antenna switch control
- `app/make_config.py` - creates the config from `config.yaml.example` for a chosen region
- `app/openhop_ui.py` - the on-device framebuffer app (320x170, keyboard driven)
- `files/` - systemd units, polkit and udev rules, launcher entry
- `maintscripts/` - Debian maintainer scripts
- `build_deb.sh`, `ci-deps.sh` - the build
- `app-builder.json`, `assets/`, `screenshots/` - store listing

## Build

The package bundles compiled Python modules, so it is built on arm64 Debian
trixie, the CardputerZero's OS. From the repository root, either on a
CardputerZero:

```bash
packaging/cardputerzero/build_deb.sh
```

or in Docker on any arm64 host (an x86 host needs QEMU and `--platform linux/arm64`):

```bash
docker run --rm -v "$PWD:/src" -w /src debian:trixie sh -c 'packaging/cardputerzero/ci-deps.sh && packaging/cardputerzero/build_deb.sh'
```

The result is `packaging/cardputerzero/build/openhop-cz_<version>_arm64.deb`.
The version is the openHop version of the checkout plus a Debian revision
(`REVISION`, default 1): tag `1.2.0` gives `1.2.0-1`, a commit after it gives
`1.2.1~dev7-1`. The `CardputerZero Package` workflow runs the same build.

Install with `sudo apt install ./openhop-cz_<version>_arm64.deb`, then open
**openHop** from the launcher.

## Files on the device

- `/etc/openhop-cz/config.yaml` - repeater config (private to the `openhop` user)
- `/var/lib/openhop-cz/` - database and identity key
- `journalctl -u openhop-cz` - logs

## How the app controls the service

The launcher app runs as the logged-in user and has no root access. A polkit
rule lets members of the `gpio` group start and stop exactly these units:
`openhop-cz.service`, `openhop-cz-setup@<preset>.service` (writes the region
into the config) and `openhop-cz-autostart@on|off.service`. All of them run as
`openhop`.
