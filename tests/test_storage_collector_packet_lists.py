"""Regression test: StorageCollector's packet list wrappers must forward
``include_raw`` so the endpoints' opt-in reaches SQLite.
"""

from unittest.mock import Mock

from repeater.data_acquisition.storage_collector import StorageCollector


def _bare_collector() -> StorageCollector:
    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = Mock()
    return collector


def test_recent_packets_forwards_include_raw():
    collector = _bare_collector()
    collector.get_recent_packets(limit=25, include_raw=True)
    collector.sqlite_handler.get_recent_packets.assert_called_once_with(25, include_raw=True)


def test_filtered_packets_forwards_include_raw_and_defaults_off():
    collector = _bare_collector()
    collector.get_filtered_packets(limit=10, offset=5)
    collector.sqlite_handler.get_filtered_packets.assert_called_once_with(
        None, None, None, None, 10, 5, include_raw=False
    )
