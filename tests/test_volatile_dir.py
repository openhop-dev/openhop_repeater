"""storage.volatile_dir: repeater-volatile.db can live outside storage_dir (e.g. on tmpfs)."""

from repeater.config import load_config, resolve_volatile_dir, resolve_storage_dir
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def test_resolve_volatile_dir_defaults_to_storage_dir(tmp_path):
    config = {"storage": {"storage_dir": str(tmp_path)}}
    assert resolve_storage_dir(config) == tmp_path.resolve()
    assert resolve_volatile_dir(config) == tmp_path.resolve()


def test_resolve_volatile_dir_uses_configured_dir(tmp_path):
    disk = tmp_path / "disk"
    ram = tmp_path / "ram"
    config = {"storage": {"storage_dir": str(disk), "volatile_dir": str(ram)}}
    assert resolve_storage_dir(config) == disk.resolve()
    assert resolve_volatile_dir(config) == ram.resolve()


def test_resolve_volatile_dir_relative_to_config_file(tmp_path):
    config = {"storage": {"storage_dir": "disk", "volatile_dir": "ram"}}
    config_path = tmp_path / "config.yaml"
    assert (
        resolve_volatile_dir(config, config_path=str(config_path)) == (tmp_path / "ram").resolve()
    )


def test_load_config_makes_volatile_dir_absolute(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("storage:\n  storage_dir: disk\n  volatile_dir: ram\n")
    config = load_config(str(config_path))
    assert config["storage"]["storage_dir"] == str((tmp_path / "disk").resolve())
    assert config["storage"]["volatile_dir"] == str((tmp_path / "ram").resolve())


def test_load_config_leaves_volatile_dir_unset(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("storage:\n  storage_dir: disk\n")
    assert "volatile_dir" not in load_config(str(config_path))["storage"]


def test_handler_default_paths_in_storage_dir(tmp_path):
    handler = SQLiteHandler(tmp_path)
    assert handler.sqlite_path == tmp_path / "repeater.db"
    assert handler.volatile_path == tmp_path / "repeater-volatile.db"


def test_handler_volatile_path_in_volatile_dir(tmp_path):
    disk, ram = tmp_path / "disk", tmp_path / "ram" / "nested"
    handler = SQLiteHandler(disk, volatile_dir=ram)
    assert handler.sqlite_path == disk / "repeater.db"
    assert handler.volatile_path == ram / "repeater-volatile.db"
    assert ram.is_dir()
