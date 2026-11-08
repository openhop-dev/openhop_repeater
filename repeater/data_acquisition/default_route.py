"""Best-effort namespace main-table recommendation, not a policy-routing lookup."""

import re
import sys

import psutil

MAX_ROUTE_BYTES = 256 * 1024


def _read_table(path, limit):
    with open(path, encoding="ascii") as stream:
        return stream.read(limit + 1)


def default_interface(sources, *, route_reader=None, interface_stats=None, platform=None):
    """Return a raw, currently up source or None; discovery must not break history."""
    if (sys.platform if platform is None else platform) != "linux":
        return None
    try:
        read = route_reader or _read_table
        text = read("/proc/net/route", MAX_ROUTE_BYTES)
        if len(text) > MAX_ROUTE_BYTES:
            return None
        lines = text.splitlines()
        if not lines or lines[0].split() != [
            "Iface",
            "Destination",
            "Gateway",
            "Flags",
            "RefCnt",
            "Use",
            "Metric",
            "Mask",
            "MTU",
            "Window",
            "IRTT",
        ]:
            return None
        candidates = []
        for line in lines[1:]:
            fields = line.split()
            if (
                len(fields) != 11
                or any(re.fullmatch(r"[0-9A-Fa-f]{8}", fields[i]) is None for i in (1, 2, 7))
                or any(not fields[i].isdecimal() for i in (4, 5, 6, 8, 9, 10))
            ):
                return None
            if re.fullmatch(r"[0-9A-Fa-f]{4,8}", fields[3]) is None:
                return None
            flags = int(fields[3], 16)
            if flags & 1 and not flags & 0x200 and fields[7] == "00000080":
                return None  # Common VPN split-default (/1) overrides a default.
            if fields[1] == fields[7] == "00000000":
                flags = int(fields[3], 16)
                if flags & 1 and not flags & 0x200:
                    candidates.append((int(fields[6]), fields[0]))
        families = [candidates]
        text = read("/proc/net/ipv6_route", MAX_ROUTE_BYTES)
        if len(text) > MAX_ROUTE_BYTES:
            return None
        candidates = []
        for line in text.splitlines():
            fields = line.split()
            widths = (32, 2, 32, 2, 32, 8, 8, 8, 8)
            if (
                len(fields) != 10
                or any(
                    re.fullmatch(rf"[0-9A-Fa-f]{{{width}}}", fields[i]) is None
                    for i, width in enumerate(widths)
                )
                or int(fields[1], 16) > 128
                or int(fields[3], 16) > 128
            ):
                return None
            flags = int(fields[8], 16)
            if flags & 1 and not flags & 0x200 and fields[1] == "01":
                return None
            if fields[0] == "0" * 32 and fields[1] == "00":
                flags = int(fields[8], 16)
                if flags & 1 and not flags & 0x200:
                    if fields[2] != "0" * 32 or fields[3] != "00":
                        return None  # Source-specific default cannot be universal.
                    candidates.append((int(fields[5], 16), fields[9]))
        families.append(candidates)
        names = set()
        for candidates in families:
            if candidates:
                best = min(metric for metric, _ in candidates)
                names.update(name for metric, name in candidates if metric == best)
        if len(names) != 1:
            return None
        name = names.pop()
        stats = (interface_stats or psutil.net_if_stats)()
        if name == "lo" or name not in sources or name not in stats or not stats[name].isup:
            return None
        if "loopback" in getattr(stats[name], "flags", "").split(","):
            return None
        return name
    except Exception:  # noqa: BLE001 - optional recommendation never fails history
        return None
