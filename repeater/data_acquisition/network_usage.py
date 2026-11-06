"""Namespace interface deltas and locally accepted MQTT payload accounting.

Never sum interfaces: bridges, tunnels and uplinks can observe the same bytes.
MQTT payloads exclude topics, framing, retries and TLS; acceptance is not receipt.
Samples are attributed to their ending minute (boundary samples to the prior
minute). Incomplete minutes and unobserved intervals are not certified coverage.
"""

import hashlib
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

import psutil


def broker_name(value):
    """Labels may come from legacy endpoint strings; never retain URL credentials."""
    name = str(value or "MQTT broker")
    if any(marker in name for marker in ("://", "@", "?", "#")):
        return "MQTT broker"
    return name[:256]


def broker_identity(broker):
    """Stable opaque endpoint identity: excludes credentials, topics and list index."""
    host = str(broker.get("host", ""))
    host = urlsplit(host if "://" in host else "//" + host).hostname or ""
    safe = [host.lower(), broker.get("port", 1883), broker_name(broker.get("name"))]
    return "mqtt:" + hashlib.sha256(json.dumps(safe).encode()).hexdigest()[:32]


class NetworkUsageCollector:
    MAX_PENDING = 4096
    RETENTION = 8 * 86400  # Seven complete days plus a day of rounding/retry margin.
    INTERVAL = 10

    def __init__(self, database, *, clock=time.time, counters=None):
        self.database = database
        self.clock = clock
        self.counters = counters or (lambda: psutil.net_io_counters(pernic=True, nowrap=False))
        self._baseline = {}
        self._pending = {}
        self._lock = threading.Lock()
        self.last_sample = None
        self.error = None
        self._sample_error = None
        self.running = False
        self._retry = None
        self._names = {}
        self._coverage = set()
        self._continuous_since = None
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_prune = None

    def _bound_pending(self, now):
        # Called under the accumulator lock; bounded work even on endless DB outage.
        cutoff = now - self.RETENTION
        while self._pending and (
            len(self._pending) > self.MAX_PENDING or next(iter(self._pending))[2] < cutoff
        ):
            key = next(iter(self._pending))
            del self._pending[key]
            self._coverage.discard(key[2])
            self._continuous_since = now
        while len(self._names) > self.MAX_PENDING:
            del self._names[next(iter(self._names))]
        self._coverage = {b for b in self._coverage if b >= cutoff}

    def record_mqtt(self, source, name, payload):
        size = len(payload.encode("utf-8") if isinstance(payload, str) else payload or b"")
        now = self.clock()
        with self._lock:
            key = ("mqtt", source, int(now // 60) * 60)
            row = self._pending.setdefault(key, [0, 0, 0])
            row[0] += size
            row[2] += 1
            self._names[("mqtt", source)] = broker_name(name)
            self._bound_pending(now)

    def sample(self):
        now = self.clock()
        try:
            current = dict(self.counters())
        except Exception:  # noqa: BLE001 - keep external sampler/DB errors sanitized
            with self._lock:
                self._baseline = {}
                self._continuous_since = None
                self._sample_error = "Interface sampling unavailable"
            return
        with self._lock:
            continuous = (
                self.last_sample is not None
                and 0 < now - self.last_sample <= self.INTERVAL * 3
                and self._continuous_since is not None
            )
            if not continuous:
                self._baseline = {}
                self._continuous_since = now
            if set(current) != set(self._baseline):
                self._continuous_since = now
            bucket = max(0, int((now - 0.000001) // 60) * 60)
            for name, value in current.items():
                previous = self._baseline.get(name)
                row = self._pending.setdefault(("interface", name, bucket), [0, 0, 0])
                if previous is not None:
                    if (
                        value.bytes_sent < previous.bytes_sent
                        or value.bytes_recv < previous.bytes_recv
                    ):
                        self._continuous_since = now
                    else:
                        row[0] += value.bytes_sent - previous.bytes_sent
                        row[1] += value.bytes_recv - previous.bytes_recv
            # A complete minute needs uninterrupted successful samples across both
            # edges. Empty interface/MQTT sets are genuinely observed zero traffic.
            end = int(now // 60) * 60
            if self._continuous_since <= end - 60:
                self._coverage.add(end - 60)
            self._baseline = current
            self.last_sample = now
            self._sample_error = None
            self._bound_pending(now)

    def flush(self):
        # A detached batch owns its deltas until acknowledged; publishers never
        # hold this lock or do I/O. The DB receipt handles ambiguous commit errors.
        with self._flush_lock:
            if self._retry is not None and self._retry[4] < self.clock() - self.RETENTION:
                # Receipts have the same retention horizon. Never replay a batch
                # whose receipt may already have been pruned.
                self._retry = None
            if self._retry is None:
                with self._lock:
                    rows = [
                        {
                            "kind": k[0],
                            "source": k[1],
                            "bucket": k[2],
                            "upload_bytes": v[0],
                            "download_bytes": v[1],
                            "messages": v[2],
                        }
                        for k, v in self._pending.items()
                    ]
                    self._retry = (
                        uuid.uuid4().hex,
                        rows,
                        dict(self._names),
                        set(self._coverage),
                        int(self.clock()),
                    )
                    self._pending.clear()
                    self._coverage.clear()
            try:
                self.database.store_usage_batch(*self._retry)
            except Exception:  # noqa: BLE001 - keep external sampler/DB errors sanitized
                self.error = "Usage persistence unavailable; retry pending"
                return False
            self._retry = None
            self.error = None
            return True

    def _run(self):
        try:
            while not self._stop.is_set():
                self.sample()
                self.flush()
                now = self.clock()
                if self._last_prune is None or now - self._last_prune >= 3600:
                    try:
                        self.database.prune_usage_before(int((now - self.RETENTION) // 60) * 60)
                        self._last_prune = now
                    except Exception:  # noqa: BLE001 - keep external sampler/DB errors sanitized
                        self.error = "Usage retention maintenance unavailable"
                self._stop.wait(self.INTERVAL)
            self.sample()
            # A retry may own an earlier detached batch; drain new deltas too.
            if self.flush():
                self.flush()
        finally:
            self.running = False

    def start(self):
        if self._thread is not None:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, name="network-usage", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                self.error = "Usage collector shutdown incomplete"

    def snapshot(self, days=1, *, hours=None) -> dict:
        if hours is None:
            hours = days * 24
        end = int(self.clock() // 60) * 60
        start = end - hours * 3600
        rows, first, covered, names = self.database.get_usage_snapshot(start, end)
        interfaces = [
            {**{k: v for k, v in r.items() if k != "kind"}, "name": r["source"]}
            for r in rows
            if r["kind"] == "interface"
        ]
        brokers = [
            {
                "source": r["source"],
                "name": names.get(("mqtt", r["source"]), "MQTT broker"),
                "upload_bytes": r["upload_bytes"],
                "messages": r["messages"],
            }
            for r in rows
            if r["kind"] == "mqtt"
        ]
        return {
            "hours": hours,
            "days": hours / 24,
            "start": start,
            "end": end,
            "bucket_seconds": 60,
            "available_since": first,
            "partial": covered != hours * 60,
            "scope": "Current network namespace (container-visible when containerized); interfaces overlap; MQTT is locally accepted payload bytes, not transport bytes or broker receipt",
            "interfaces": interfaces,
            "mqtt": {
                "brokers": brokers,
                "upload_bytes": sum(r["upload_bytes"] for r in brokers),
                "messages": sum(r["messages"] for r in brokers),
            },
            "collection": {
                "running": self.running,
                "last_sample": self.last_sample,
                "error": self.error or self._sample_error,
            },
        }
