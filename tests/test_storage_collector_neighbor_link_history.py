"""Regression test: StorageCollector.get_neighbor_link_history must accept and
pass through ``before``, ``since`` and ``bucket``.

The web endpoint forwards all three; a wrapper that only knew the original
four keywords raised TypeError as soon as a client sent any of them.
"""

from unittest.mock import Mock

from repeater.data_acquisition.storage_collector import StorageCollector


def _bare_collector() -> StorageCollector:
    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = Mock()
    return collector


def test_passes_paging_and_bucket_arguments_through():
    collector = _bare_collector()
    collector.get_neighbor_link_history(
        peer_hash="2A",
        path_hash_size=1,
        hours=168,
        limit=500,
        before=1700000000.0,
        since=1699000000.0,
        bucket=600,
    )
    collector.sqlite_handler.get_neighbor_link_history.assert_called_once_with(
        peer_hash="2A",
        path_hash_size=1,
        hours=168,
        limit=500,
        before=1700000000.0,
        since=1699000000.0,
        bucket=600,
    )


def test_defaults_match_older_callers():
    collector = _bare_collector()
    collector.get_neighbor_link_history(peer_hash="2A", path_hash_size=1)
    collector.sqlite_handler.get_neighbor_link_history.assert_called_once_with(
        peer_hash="2A", path_hash_size=1, hours=24, limit=1000, before=None, since=None, bucket=None
    )
