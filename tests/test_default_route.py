"""Synthetic Linux proc fixtures; no dependency on host routes or NIC state."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from repeater.data_acquisition.default_route import MAX_ROUTE_BYTES, default_interface
from repeater.data_acquisition.network_usage import NetworkUsageCollector
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

V4_HEADER = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"


def ipv4(interface="uplink", metric=10, flags="0003"):
    return f"{interface} 00000000 0100000A {flags} 0 0 {metric} 00000000 0 0 0\n"


def reader(v4="", v6=""):
    return lambda path, limit: V4_HEADER + v4 if path.endswith("/route") else v6


def ipv6(interface="uplink", metric=10, flags="00000003", source_prefix="00"):
    return (
        f"{'0' * 32} 00 {'0' * 32} {source_prefix} {'0' * 31}1 "
        f"{metric:08x} 00000000 00000000 {flags} {interface}\n"
    )


def recommend(v4="", v6="", sources=("uplink", "other"), stats=None):
    return default_interface(
        sources,
        route_reader=reader(v4, v6),
        platform="linux",
        interface_stats=lambda: (
            stats if stats is not None else {name: SimpleNamespace(isup=True) for name in sources}
        ),
    )


@pytest.mark.parametrize(
    "v4,v6,expected",
    [
        ("", ipv6(), "uplink"),
        (ipv4(), ipv6(), "uplink"),
        (ipv4(), ipv6("other"), None),
        ("", ipv6("other", 20) + ipv6("uplink", 10), "uplink"),
        ("", ipv6("other", 10) + ipv6("uplink", 10), None),
        ("", ipv6(flags="00000203"), None),
        ("", ipv6(flags="00000002"), None),
        (ipv4(), ipv6(source_prefix="40"), None),
    ],
)
def test_dual_stack_minimum_metric_and_ambiguity(v4, v6, expected):
    assert recommend(v4, v6) == expected


@pytest.mark.parametrize(
    "v4,expected",
    [
        (ipv4("other", 20) + ipv4(), "uplink"),
        (ipv4("other") + ipv4(), None),
        (ipv4() + ipv4(), "uplink"),
        (ipv4(flags="0203"), None),
        (ipv4(flags="0002"), None),
        (ipv4("lo"), None),
        (ipv4("absent"), None),
        ("", None),
        (ipv4() + "broken\n", None),
        (ipv4().replace("10 00000000", "-1 00000000"), None),
        (ipv4().replace("0100000A", "INVALID!"), None),
    ],
)
def test_ipv4_routes_are_validated_without_guessing(v4, expected):
    assert recommend(v4) == expected


@pytest.mark.parametrize(
    "stats",
    [
        {},
        {"uplink": SimpleNamespace(isup=False)},
        {"uplink": SimpleNamespace(isup=True, flags="up,loopback,running")},
    ],
)
def test_recommendation_requires_current_up_nonloopback_nic(stats):
    assert recommend(ipv4(), stats=stats) is None


@pytest.mark.parametrize(
    "v6",
    ["broken\n", ipv6().replace("0000000a", "ZZZZZZZZ"), ipv6().replace("00000003", "-0000003")],
)
def test_malformed_ipv6_cannot_be_hidden_by_valid_ipv4(v6):
    assert recommend(ipv4(), v6) is None


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("mode", ["missing", "permission", "oversized", "malformed"])
def test_either_unreadable_table_makes_recommendation_unknown(family, mode):
    def read(path, limit):
        assert limit == MAX_ROUTE_BYTES
        if path == ("/proc/net/route" if family == 4 else "/proc/net/ipv6_route"):
            if mode == "missing":
                raise FileNotFoundError()
            if mode == "permission":
                raise PermissionError()
            if mode == "oversized":
                return " " * (limit + 1)
            return "malformed\n"
        return V4_HEADER + ipv4() if path.endswith("/route") else ipv6()

    assert (
        default_interface(
            {"uplink"},
            route_reader=read,
            interface_stats=lambda: {"uplink": SimpleNamespace(isup=True)},
            platform="linux",
        )
        is None
    )


def test_stats_failure_is_optional_and_unsupported_platform_does_not_probe():
    def fail(*args):
        raise OSError("probe failure")

    assert (
        default_interface(
            {"uplink"}, route_reader=reader(ipv4()), interface_stats=fail, platform="linux"
        )
        is None
    )
    assert (
        default_interface({"uplink"}, route_reader=fail, interface_stats=fail, platform="darwin")
        is None
    )


def test_real_file_reads_are_bounded(tmp_path, monkeypatch):
    table = tmp_path / "route"
    table.write_text(" " * (MAX_ROUTE_BYTES + 1))
    builtin_open = open
    sizes = []

    class Stream:
        def __enter__(self):
            self.stream = builtin_open(table)
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size):
            sizes.append(size)
            return self.stream.read(size)

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: Stream())
    assert default_interface({"uplink"}, platform="linux") is None
    assert sizes == [MAX_ROUTE_BYTES + 1]


def test_split_default_vpn_routes_are_not_a_universal_uplink():
    split_v4 = ipv4("other").replace("00000000 0 0 0", "00000080 0 0 0")
    assert recommend(ipv4() + split_v4) is None
    split_v6 = ipv6("other").replace(" 00 ", " 01 ", 1)
    assert recommend(ipv4(), split_v6) is None


def test_openapi_recommendation_is_required_nullable_raw_source():
    document = yaml.safe_load((Path(__file__).parents[1] / "repeater/web/openapi.yaml").read_text())
    schema = document["components"]["schemas"]["NetworkUsage"]
    assert "default_interface" in schema["required"]
    assert schema["properties"]["default_interface"]["type"] == "string"
    assert schema["properties"]["default_interface"]["nullable"] is True


def test_collector_does_not_recommend_a_route_absent_from_window(tmp_path):
    collector = NetworkUsageCollector(
        SQLiteHandler(tmp_path),
        clock=lambda: 120,
        counters=dict,
        route_reader=reader(ipv4()),
        interface_stats=lambda: {"uplink": SimpleNamespace(isup=True)},
    )
    assert collector.snapshot(hours=1)["interfaces"] == []
    assert collector.snapshot(hours=1)["default_interface"] is None


def test_collector_recommends_raw_route_source_from_returned_history(tmp_path):
    db = SQLiteHandler(tmp_path)
    db.store_usage_buckets(
        [
            {
                "kind": "interface",
                "source": "uplink",
                "bucket": 60,
                "upload_bytes": 0,
                "download_bytes": 0,
                "messages": 0,
            },
            {
                "kind": "interface",
                "source": "busy",
                "bucket": 60,
                "upload_bytes": 999999,
                "download_bytes": 999999,
                "messages": 0,
            },
        ]
    )
    collector = NetworkUsageCollector(
        db,
        clock=lambda: 120,
        counters=dict,
        route_reader=reader(ipv4()),
        interface_stats=lambda: {"uplink": SimpleNamespace(isup=True)},
    )
    report = collector.snapshot(hours=1)
    assert report["default_interface"] == "uplink"
    assert report["default_interface"] in {row["source"] for row in report["interfaces"]}
