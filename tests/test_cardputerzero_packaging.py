"""The CardputerZero store package stays consistent with the files it is built from."""

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "cardputerzero"
HARDWARE_KEY = "cardputerzero-cap-lora-1262"


@pytest.fixture()
def make_config(tmp_path, monkeypatch):
    """make_config.py loaded against a share directory laid out as build_deb.sh does."""
    share = tmp_path / "share"
    share.mkdir()
    for name in ("config.yaml.example", "radio-presets.json", "radio-settings.json"):
        shutil.copy(ROOT / name, share / name)
    monkeypatch.setenv("OPENHOP_CZ_SHARE", str(share))
    spec = importlib.util.spec_from_file_location("cz_make_config", PKG / "app" / "make_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hardware_entry_is_in_radio_settings():
    entry = json.loads((ROOT / "radio-settings.json").read_text())["hardware"][HARDWARE_KEY]
    assert (entry["bus_id"], entry["cs_id"], entry["cs_pin"]) == (0, 1, -1)
    assert (entry["reset_pin"], entry["busy_pin"], entry["irq_pin"]) == (26, 22, 23)
    assert entry["use_dio3_tcxo"] is True and entry["dio3_tcxo_voltage"] == 3.0
    assert entry["use_dio2_rf"] is True


def test_regions_skip_bands_the_module_cannot_use(make_config):
    regions = make_config.regions()
    assert regions
    assert all(radio["frequency"] >= 800_000_000 for _, _, _, radio in regions)
    assert any("USA" in title for _, title, _, _ in regions)


def test_new_config_uses_the_hardware_entry_and_keeps_edits(make_config, tmp_path):
    config = tmp_path / "etc" / "config.yaml"
    config.parent.mkdir()
    usa = next(r for r in make_config.regions() if "USA" in r[1])
    argv = ["make_config.py", str(usa[0]), "--config", str(config), "--storage", str(tmp_path)]

    assert make_config.main(argv) == 0
    cfg = yaml.safe_load(config.read_text())
    assert cfg["radio_type"] == "sx1262"
    assert cfg["sx1262"]["cs_id"] == 1 and cfg["sx1262"]["irq_pin"] == 23
    assert "name" not in cfg["sx1262"] and "tx_power" not in cfg["sx1262"]
    assert cfg["radio"]["frequency"] == usa[3]["frequency"]
    assert cfg["radio"]["tx_power"] == 22
    assert cfg["storage"]["storage_dir"] == str(tmp_path)
    assert json.loads((config.parent / "summary.json").read_text())["region"] == usa[1]

    # Changing region later only touches the radio settings.
    cfg["repeater"]["node_name"] = "edited-in-dashboard"
    config.write_text(yaml.safe_dump(cfg))
    other = next(r for r in make_config.regions() if r[3]["frequency"] != usa[3]["frequency"])
    assert make_config.main(["make_config.py", str(other[0]), "--config", str(config)]) == 0
    cfg = yaml.safe_load(config.read_text())
    assert cfg["repeater"]["node_name"] == "edited-in-dashboard"
    assert cfg["radio"]["frequency"] == other[3]["frequency"]


def test_no_service_runs_as_root():
    # The store rejects packages whose systemd services run as root.
    units = sorted((PKG / "files").glob("*.service"))
    assert units
    for unit in units:
        users = re.findall(r"^User=(.*)$", unit.read_text(), flags=re.MULTILINE)
        assert users == ["openhop"], unit.name


def test_store_listing_files_exist():
    manifest = json.loads((PKG / "app-builder.json").read_text())
    store = manifest["store"]
    assert manifest["package_name"] == "openhop-cz"
    assert len(store["summary"]) <= 80
    assert re.fullmatch(r"[A-Za-z0-9]{4}", store["share_code"])
    assert len(store["permissions"]) == 7
    for relative in [store["icon"], *store["screenshots"]]:
        assert (PKG / relative).is_file(), relative
