import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple

from openhop_core.protocol import Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_TRACE,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


def _update_link_stats(
    stats,
    *,
    now: float,
    now_monotonic: float,
    rssi: float,
    snr: float,
    score: float,
    is_duplicate: bool,
    alpha: float,
) -> None:
    """Fold one observation into a ``NeighbourLink`` or ``RadioLinkStats``."""
    first_sample = stats.sample_count == 0
    stats.sample_count += 1
    if is_duplicate:
        stats.duplicate_sample_count += 1

    stats.last_seen = now
    stats.last_seen_monotonic = now_monotonic
    stats.last_rssi = rssi
    stats.last_snr = snr
    stats.last_score = score

    if first_sample:
        stats.first_seen = now
        stats.ewma_rssi = rssi
        stats.ewma_snr = snr
        stats.ewma_score = score
        stats.best_score = score
        stats.worst_score = score
        return

    stats.ewma_rssi = alpha * rssi + (1.0 - alpha) * stats.ewma_rssi
    stats.ewma_snr = alpha * snr + (1.0 - alpha) * stats.ewma_snr
    stats.ewma_score = alpha * score + (1.0 - alpha) * stats.ewma_score

    if score > stats.best_score:
        stats.best_score = score
    if score < stats.worst_score:
        stats.worst_score = score


@dataclass
class RadioLinkStats:
    """One neighbour as heard by one radio of a multi-radio node."""

    radio_id: str

    first_seen: float
    last_seen: float
    last_seen_monotonic: float

    sample_count: int = 0
    duplicate_sample_count: int = 0

    last_rssi: float = 0.0
    last_snr: float = 0.0
    last_score: float = 0.0

    ewma_rssi: float = 0.0
    ewma_snr: float = 0.0
    ewma_score: float = 0.0

    best_score: float = 0.0
    worst_score: float = 1.0


@dataclass
class NeighbourLink:
    peer_hash: str
    path_hash_size: int

    first_seen: float
    last_seen: float
    last_seen_monotonic: float

    sample_count: int = 0
    duplicate_sample_count: int = 0

    last_rssi: float = 0.0
    last_snr: float = 0.0
    last_score: float = 0.0

    ewma_rssi: float = 0.0
    ewma_snr: float = 0.0
    ewma_score: float = 0.0

    best_score: float = 0.0
    worst_score: float = 1.0

    # Per receiving radio. The fields above stay merged across every radio.
    radios: dict = field(default_factory=dict)

    def update(
        self,
        *,
        now: float,
        now_monotonic: float,
        rssi: float,
        snr: float,
        score: float,
        is_duplicate: bool,
        alpha: float,
        rx_radio_id: Optional[str] = None,
    ) -> None:
        sample = dict(
            now=now,
            now_monotonic=now_monotonic,
            rssi=rssi,
            snr=snr,
            score=score,
            is_duplicate=is_duplicate,
            alpha=alpha,
        )
        _update_link_stats(self, **sample)
        if rx_radio_id is None:
            return
        radio_id = str(rx_radio_id)
        per_radio = self.radios.get(radio_id)
        if per_radio is None:
            per_radio = self.radios[radio_id] = RadioLinkStats(
                radio_id=radio_id,
                first_seen=now,
                last_seen=now,
                last_seen_monotonic=now_monotonic,
            )
        _update_link_stats(per_radio, **sample)


class NeighbourLinkTracker:
    def __init__(self, config: dict):
        self._neighbour_links: dict[str, NeighbourLink] = {}
        self._neighbour_links_lock = threading.RLock()

        self._metrics_enabled = True
        self._ewma_alpha = 0.20
        self._ttl_seconds = 86400.0
        self._max_entries = 512
        self.refresh_config(config)

    @property
    def links(self) -> dict[str, NeighbourLink]:
        return self._neighbour_links

    @property
    def lock(self) -> threading.RLock:
        return self._neighbour_links_lock

    @property
    def metrics_enabled(self) -> bool:
        return self._metrics_enabled

    @property
    def ewma_alpha(self) -> float:
        return self._ewma_alpha

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def refresh_config(self, config: dict) -> None:
        repeater_config = config.get("repeater", {})

        self._metrics_enabled = bool(repeater_config.get("neighbour_link_metrics_enabled", True))

        try:
            alpha = float(repeater_config.get("neighbour_link_ewma_alpha", 0.20))
        except (TypeError, ValueError):
            alpha = 0.20
        self._ewma_alpha = max(0.0, min(1.0, alpha))

        try:
            ttl = float(repeater_config.get("neighbour_link_ttl_seconds", 86400))
        except (TypeError, ValueError):
            ttl = 86400.0
        self._ttl_seconds = max(1.0, ttl)

        try:
            max_entries = int(repeater_config.get("neighbour_link_max_entries", 512))
        except (TypeError, ValueError):
            max_entries = 512
        self._max_entries = max(1, max_entries)

    def purge_expired_locked(self, now_monotonic: float) -> None:
        expired_keys = [
            key
            for key, link in self._neighbour_links.items()
            if (now_monotonic - link.last_seen_monotonic) > self._ttl_seconds
        ]
        for key in expired_keys:
            del self._neighbour_links[key]

    def evict_stalest_locked(self) -> None:
        if not self._neighbour_links:
            return
        stalest_key = min(
            self._neighbour_links,
            key=lambda key: self._neighbour_links[key].last_seen_monotonic,
        )
        del self._neighbour_links[stalest_key]

    @staticmethod
    def get_upstream_peer_identity(
        packet: Packet,
        route_type: int,
        payload_type: Optional[int],
        *,
        path_hashes=None,
        path_hash_size: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[int]]:
        if route_type not in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
            return None, None
        if payload_type == PAYLOAD_TYPE_TRACE:
            return None, None

        hashes = path_hashes if path_hashes is not None else packet.get_path_hashes_hex()
        if not hashes:
            return None, None

        size = path_hash_size
        if size is None:
            size = packet.get_path_hash_size() if hasattr(packet, "get_path_hash_size") else None
        if not size or int(size) <= 0:
            return None, None

        peer_hash = str(hashes[-1]).upper()
        if not peer_hash:
            return None, None

        return peer_hash, int(size)

    def observe(
        self,
        packet: Packet,
        *,
        route_type: int,
        payload_type: Optional[int],
        rssi: float,
        snr: float,
        score: float,
        is_duplicate: bool,
        rx_radio_id: Optional[str] = None,
    ) -> None:
        if not self._metrics_enabled:
            return

        peer_hash, path_hash_size = self.get_upstream_peer_identity(
            packet,
            route_type,
            payload_type,
        )
        if not peer_hash or not path_hash_size:
            return

        now = time.time()
        now_monotonic = time.monotonic()
        key = f"{path_hash_size}:{peer_hash}"

        with self._neighbour_links_lock:
            self.purge_expired_locked(now_monotonic)
            link = self._neighbour_links.get(key)

            if link is None and len(self._neighbour_links) >= self._max_entries:
                self.evict_stalest_locked()

            if link is None:
                link = NeighbourLink(
                    peer_hash=peer_hash,
                    path_hash_size=path_hash_size,
                    first_seen=now,
                    last_seen=now,
                    last_seen_monotonic=now_monotonic,
                )
                self._neighbour_links[key] = link

            link.update(
                now=now,
                now_monotonic=now_monotonic,
                rssi=float(rssi),
                snr=float(snr),
                score=float(score),
                is_duplicate=is_duplicate,
                alpha=self._ewma_alpha,
                rx_radio_id=rx_radio_id,
            )

    @staticmethod
    def _radio_entries(
        link: NeighbourLink, radio_ids: list, now_monotonic: float, active_window: float
    ) -> list:
        """Per-radio stats in configured order, then any radio no longer configured."""
        ordered = [rid for rid in radio_ids if rid in link.radios]
        ordered += sorted(rid for rid in link.radios if rid not in radio_ids)
        entries = []
        for radio_id in ordered:
            stats = link.radios[radio_id]
            age_seconds = max(0.0, now_monotonic - stats.last_seen_monotonic)
            entries.append(
                {
                    "radio_id": radio_id,
                    "sample_count": stats.sample_count,
                    "duplicate_sample_count": stats.duplicate_sample_count,
                    "first_seen": stats.first_seen,
                    "last_seen": stats.last_seen,
                    "age_seconds": age_seconds,
                    "active": age_seconds <= active_window,
                    "last_rssi": stats.last_rssi,
                    "last_snr": stats.last_snr,
                    "last_score": stats.last_score,
                    "ewma_rssi": stats.ewma_rssi,
                    "ewma_snr": stats.ewma_snr,
                    "ewma_score": stats.ewma_score,
                    "best_score": stats.best_score,
                    "worst_score": stats.worst_score,
                }
            )
        return entries

    def snapshot(
        self,
        *,
        active_within_seconds: float = 900.0,
        radio_ids: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """Every tracked link, most recently seen first.

        ``radio_ids`` is the node's configured radios in order. With two or more,
        each link also carries ``radios``: its stats as heard by each radio.
        """
        try:
            active_window = max(0.0, float(active_within_seconds))
        except (TypeError, ValueError):
            active_window = 900.0
        radio_ids = [str(rid) for rid in (radio_ids or []) if rid is not None]
        per_radio = len(radio_ids) >= 2

        now_monotonic = time.monotonic()
        snapshot = []
        with self._neighbour_links_lock:
            self.purge_expired_locked(now_monotonic)
            for link in self._neighbour_links.values():
                age_seconds = max(0.0, now_monotonic - link.last_seen_monotonic)
                item = {
                    "peer_hash": link.peer_hash,
                    "path_hash_size": link.path_hash_size,
                    "sample_count": link.sample_count,
                    "duplicate_sample_count": link.duplicate_sample_count,
                    "first_seen": link.first_seen,
                    "last_seen": link.last_seen,
                    "age_seconds": age_seconds,
                    "active": age_seconds <= active_window,
                    "last_rssi": link.last_rssi,
                    "last_snr": link.last_snr,
                    "last_score": link.last_score,
                    "ewma_rssi": link.ewma_rssi,
                    "ewma_snr": link.ewma_snr,
                    "ewma_score": link.ewma_score,
                    "best_score": link.best_score,
                    "worst_score": link.worst_score,
                }
                if per_radio:
                    item["radios"] = self._radio_entries(
                        link, radio_ids, now_monotonic, active_window
                    )
                snapshot.append(item)

        return sorted(snapshot, key=lambda item: item["last_seen"], reverse=True)
