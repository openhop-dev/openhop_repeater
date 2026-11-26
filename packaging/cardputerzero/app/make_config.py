#!/usr/bin/env python3
"""Create or re-region the openHop Repeater config for CardputerZero + Cap LoRa-1262.

A new config starts from openHop's own config.yaml.example and overrides only
what this hardware needs. If the config already exists, only the radio
frequency settings are changed, so dashboard edits survive a region change.

Usage:
  make_config.py --list
  make_config.py REGION_INDEX [--config PATH] [--storage DIR] [--mode forward|monitor|no_tx]

REGION_INDEX is a row number from --list (MeshCore's suggested radio settings).
"""

import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHARE = os.environ.get("OPENHOP_CZ_SHARE", os.path.join(HERE, "..", "share"))
DEFAULT_CONFIG = "/etc/openhop-cz/config.yaml"
DEFAULT_STORAGE = "/var/lib/openhop-cz"
HARDWARE_KEY = "cardputerzero-cap-lora-1262"

# The Cap LoRa-1262 is an 868/915 MHz module; 433 MHz presets cannot work on it.
MIN_FREQ_MHZ = 800.0

# Keys of the radio-settings.json entry that belong in the config's radio:
# section; "name" and "description" are display only, the rest is sx1262: wiring.
RADIO_KEYS = ("tx_power", "preamble_length")
DISPLAY_KEYS = ("name", "description")


def hardware():
    """The Cap LoRa-1262 wiring as (sx1262 settings, radio settings).

    Read from the package's copy of openHop's radio-settings.json, which is
    also what the dashboard's setup wizard offers, so the two cannot disagree.
    """
    with open(os.path.join(SHARE, "radio-settings.json")) as f:
        entry = json.load(f)["hardware"][HARDWARE_KEY]
    radio = {k: entry[k] for k in RADIO_KEYS if k in entry}
    sx1262 = {k: v for k, v in entry.items() if k not in RADIO_KEYS + DISPLAY_KEYS}
    return sx1262, radio


def regions():
    """Usable presets as (index, title, description, radio-settings dict)."""
    with open(os.path.join(SHARE, "radio-presets.json")) as f:
        entries = json.load(f)["config"]["suggested_radio_settings"]["entries"]
    out = []
    for i, e in enumerate(entries):
        mhz = float(e["frequency"])
        if mhz < MIN_FREQ_MHZ:
            continue
        radio = {
            "frequency": int(round(mhz * 1_000_000)),
            "bandwidth": int(round(float(e["bandwidth"]) * 1000)),
            "spreading_factor": int(e["spreading_factor"]),
            "coding_rate": int(e["coding_rate"]),
        }
        out.append((i, e["title"], e["description"], radio))
    return out


def opt(argv, name, default):
    return argv[argv.index(name) + 1] if name in argv else default


def main(argv):
    if "--list" in argv:
        for i, title, desc, _ in regions():
            print(f"{i}\t{title}\t{desc}")
        return 0
    if len(argv) < 2 or not argv[1].isdigit():
        print(__doc__)
        return 64

    match = [r for r in regions() if r[0] == int(argv[1])]
    if not match:
        print(f"unknown region index {argv[1]}", file=sys.stderr)
        return 65
    import yaml  # only needed when writing; --list works without it

    _, title, desc, radio = match[0]
    config_path = opt(argv, "--config", DEFAULT_CONFIG)
    storage = opt(argv, "--storage", DEFAULT_STORAGE)
    mode = opt(argv, "--mode", "forward")

    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        cfg.setdefault("radio", {}).update(radio)
    else:
        with open(os.path.join(SHARE, "config.yaml.example")) as f:
            cfg = yaml.safe_load(f)
        sx1262, hw_radio = hardware()
        cfg["radio_type"] = "sx1262"
        cfg.setdefault("sx1262", {}).update(sx1262)
        cfg.setdefault("radio", {}).update(radio)
        cfg["radio"].update(hw_radio)
        cfg.setdefault("storage", {})["storage_dir"] = storage
        rep = cfg.setdefault("repeater", {})
        rep["node_name"] = socket.gethostname()[:24] or "cardputerzero"
        rep["mode"] = mode
        # 512 MB device: keep the web server small.
        cfg.setdefault("http", {}).update({"thread_pool": 4, "thread_pool_max": 8})

    tmp = config_path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# openHop Repeater config for CardputerZero + Cap LoRa-1262\n")
        yaml.safe_dump(cfg, f, sort_keys=False)
    os.chmod(tmp, 0o640)
    os.replace(tmp, config_path)

    # World-readable summary for the on-device app (the config itself is private).
    summary = {
        "region": title,
        "radio": desc.replace(" / ", "  "),
        "node_name": cfg.get("repeater", {}).get("node_name", ""),
    }
    summary_path = os.path.join(os.path.dirname(config_path), "summary.json")
    with open(summary_path + ".tmp", "w") as f:
        json.dump(summary, f)
    os.chmod(summary_path + ".tmp", 0o644)
    os.replace(summary_path + ".tmp", summary_path)
    print(f"{config_path}: region set to {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
