"""Usage buckets share the packet database, without storing packet contents."""

import sqlite3

import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def bucket(timestamp=120, **overrides):
    return dict(
        bucket=timestamp,
        kind="mqtt",
        source="broker-a",
        upload_bytes=10,
        download_bytes=0,
        messages=1,
        **overrides,
    )


def test_usage_retention_keeps_cutoff_bucket(tmp_path):
    store = SQLiteHandler(tmp_path)
    store.store_usage_buckets([bucket(60), bucket(120), bucket(180)])
    store.prune_usage_before(120)
    assert store.get_usage_totals(0, 120) == []
    assert store.get_usage_totals(120, 180)[0]["upload_bytes"] == 10
    assert store.get_usage_totals(120, 240)[0]["upload_bytes"] == 20


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", 121),
        ("bucket", -60),
        ("kind", "unknown"),
        ("source", ""),
        ("source", "x" * 257),
        ("upload_bytes", -1),
        ("download_bytes", -1),
        ("messages", -1),
        ("upload_bytes", 1.5),
    ],
)
def test_invalid_usage_rolls_back_entire_batch(tmp_path, field, value):
    store = SQLiteHandler(tmp_path)
    invalid = bucket(180)
    invalid[field] = value
    with pytest.raises(sqlite3.IntegrityError):
        store.store_usage_buckets([bucket(), invalid])
    assert store.get_usage_totals(0, 240) == []


def test_empty_batch_and_empty_history(tmp_path):
    store = SQLiteHandler(tmp_path)
    store.store_usage_buckets([])
    assert store.get_usage_totals(0, 240) == []


def test_usage_persists_and_groups_sources_after_reopen(tmp_path):
    store = SQLiteHandler(tmp_path)
    store.store_usage_buckets(
        [
            {
                "bucket": 120,
                "kind": "interface",
                "source": "eth0",
                "upload_bytes": 100,
                "download_bytes": 200,
                "messages": 0,
            },
            {
                "bucket": 120,
                "kind": "mqtt",
                "source": "broker-a",
                "upload_bytes": 60,
                "download_bytes": 0,
                "messages": 2,
            },
        ]
    )
    store.store_usage_buckets(
        [
            {
                "bucket": 120,
                "kind": "mqtt",
                "source": "broker-a",
                "upload_bytes": 40,
                "download_bytes": 0,
                "messages": 1,
            },
        ]
    )
    reopened = SQLiteHandler(tmp_path)
    assert reopened.get_usage_totals(120, 180) == [
        {
            "kind": "interface",
            "source": "eth0",
            "upload_bytes": 100,
            "download_bytes": 200,
            "messages": 0,
        },
        {
            "kind": "mqtt",
            "source": "broker-a",
            "upload_bytes": 100,
            "download_bytes": 0,
            "messages": 3,
        },
    ]
