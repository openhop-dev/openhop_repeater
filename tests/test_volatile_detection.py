"""Detection of RAM-backed storage for the volatile metrics (is_volatile_fs, /api/stats)."""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.utils import fs
from repeater.web.api_endpoints import APIEndpoints

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="fstatfs is Linux only")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(fs.sys, "platform", "linux")


@pytest.mark.parametrize(
    "magic,expected",
    [
        (0x01021994, True),  # tmpfs
        (0x858458F6, True),  # ramfs
        (0xEF53, False),  # ext4
        (0x794C7630, False),  # overlayfs
        (0x12345678, False),  # unknown
    ],
)
def test_is_volatile_fs_magics(linux, monkeypatch, tmp_path, magic, expected):
    monkeypatch.setattr(fs, "_fstatfs_type", lambda fd: magic)
    assert fs.is_volatile_fs(tmp_path) is expected


def test_is_volatile_fs_statfs_failure(linux, monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "_fstatfs_type", lambda fd: None)
    assert fs.is_volatile_fs(tmp_path) is False


def test_is_volatile_fs_non_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(fs.sys, "platform", "darwin")
    assert fs.is_volatile_fs(tmp_path) is False


def test_is_volatile_fs_never_raises(linux, monkeypatch, tmp_path):
    def boom(fd):
        raise RuntimeError("boom")

    monkeypatch.setattr(fs, "_fstatfs_type", boom)
    assert fs.is_volatile_fs(tmp_path) is False


def test_is_volatile_fs_uses_nearest_existing_parent(linux, monkeypatch, tmp_path):
    seen = []

    def fake(fd):
        seen.append(fd)
        return 0x01021994

    monkeypatch.setattr(fs, "_fstatfs_type", fake)
    assert fs.is_volatile_fs(tmp_path / "missing" / "nested") is True
    assert len(seen) == 1


@linux_only
def test_is_volatile_fs_real_directory(tmp_path):
    assert isinstance(fs.is_volatile_fs(tmp_path), bool)


@linux_only
def test_is_volatile_fs_real_dev_shm():
    mounts = Path("/proc/mounts").read_text().splitlines()
    shm = [line.split()[2] for line in mounts if line.split()[1] == "/dev/shm"]
    if not shm:
        pytest.skip("/dev/shm is not a dedicated mount")
    assert fs.is_volatile_fs("/dev/shm") is (shm[0] in ("tmpfs", "ramfs"))


def test_volatile_storage_info_keys_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "is_volatile_fs", lambda p: True)
    handler = SQLiteHandler(tmp_path / "disk", volatile_dir=tmp_path / "ram")
    info = handler.volatile_storage_info()
    assert set(info) == {"volatile", "path", "since"}
    assert info["volatile"] is True
    assert info["path"] == str(tmp_path / "ram")
    # No packets: falls back to the database file time, or None.
    assert info["since"] is None or info["since"] > 0


def test_volatile_storage_info_since_is_oldest_packet(tmp_path):
    handler = SQLiteHandler(tmp_path)
    with sqlite3.connect(handler.volatile_path) as conn:
        for ts in (2000.0, 1000.0, 3000.0):
            conn.execute(
                "INSERT INTO packets (timestamp, type, route, length, transmitted,"
                " is_duplicate) VALUES (?, 0, 0, 0, 0, 0)",
                (ts,),
            )
    assert handler.volatile_storage_info()["since"] == 1000.0


def _endpoints(sqlite_handler):
    api = APIEndpoints.__new__(APIEndpoints)
    storage = SimpleNamespace(sqlite_handler=sqlite_handler)
    handler = SimpleNamespace(storage=storage)
    api.daemon_instance = SimpleNamespace(repeater_handler=handler)
    return api


def test_metrics_storage_info_endpoint_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "is_volatile_fs", lambda p: True)
    info = _endpoints(SQLiteHandler(tmp_path))._metrics_storage_info()
    assert info["volatile"] is True


def test_metrics_storage_info_without_storage():
    api = APIEndpoints.__new__(APIEndpoints)
    api.daemon_instance = None
    assert api._metrics_storage_info() == {"volatile": False, "path": None, "since": None}


def test_metrics_storage_info_survives_handler_error():
    class Broken:
        def volatile_storage_info(self):
            raise RuntimeError("boom")

    info = _endpoints(Broken())._metrics_storage_info()
    assert info["volatile"] is False
