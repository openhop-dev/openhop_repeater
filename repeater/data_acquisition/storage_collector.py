import asyncio
import concurrent.futures
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from repeater.config import build_radio_profiles, resolve_storage_dir

from .mqtt_handler import MeshCoreToMqttPusher
from .network_usage import NetworkUsageCollector
from .rrdtool_handler import RRDToolHandler
from .sqlite_handler import SQLiteHandler
from .storage_utils import PacketRecord

logger = logging.getLogger("StorageCollector")


def _node_airtime_stats(repeater_handler) -> Optional[dict]:
    """The whole node's airtime figures, however old the handler is.

    Prefers ``airtime_stats()``, which sums the channels a multi-radio node
    meters separately. Falls back to the default radio's manager for a handler
    that predates it, and to None when there is no handler at all.
    """
    if repeater_handler is None:
        return None
    node_stats = getattr(repeater_handler, "airtime_stats", None)
    if callable(node_stats):
        try:
            return node_stats()
        except Exception as exc:
            logger.debug(f"Node airtime stats unavailable: {exc}")
    airtime_mgr = getattr(repeater_handler, "airtime_mgr", None)
    if airtime_mgr is None:
        return None
    try:
        return airtime_mgr.get_stats()
    except Exception as exc:
        logger.debug(f"Airtime stats unavailable: {exc}")
        return None


class StorageCollector:
    def __init__(self, config: dict, local_identity=None, repeater_handler=None):
        self.config = config
        self.repeater_handler = repeater_handler
        self.glass_publish_callback = None
        self._pending_tasks = set()

        metrics_config = config.get("metrics")
        if not isinstance(metrics_config, dict):
            metrics_config = {}
        self.rrd_enabled = bool(metrics_config.get("rrd_enabled", True))

        # Dedicated single writer thread for all blocking storage work (the SQLite
        # write, the cumulative-counts aggregate, RRD updates, and network
        # publishing). This keeps that work off the asyncio event loop, which it
        # was previously stalling for seconds per packet on a busy mesh — starving
        # every other coroutine (e.g. send_advert would time out). One worker
        # preserves packet write ordering and reuses a single thread-local SQLite
        # connection (no WAL writer contention, no connection fan-out).
        self._db_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="storage-writer"
        )

        # Radio count is fixed at boot (build_radio_stack opens the hardware),
        # so this is resolved once rather than per packet. Air settings can
        # still change at runtime; those are re-read when status is published.
        self._multi_radio = len(build_radio_profiles(config)) > 1

        self.storage_dir = resolve_storage_dir(config)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite_handler = SQLiteHandler(self.storage_dir)
        self.network_usage = NetworkUsageCollector(self.sqlite_handler)
        self.network_usage.start()
        self.rrd_handler = None
        if self.rrd_enabled:
            candidate_rrd_handler = RRDToolHandler(self.storage_dir)
            if candidate_rrd_handler.available and candidate_rrd_handler.rrd_path.exists():
                self.rrd_handler = candidate_rrd_handler
                logger.info("RRDtool metrics enabled")
            else:
                logger.warning("RRDtool requested but unavailable; using SQLite metrics fallback")
        else:
            logger.info("RRDtool metrics disabled; SQLite metrics fallback will be used")

        # Initialize MQTT handler only when at least one broker is configured
        self.mqtt_handler = None
        mqtt_brokers_config = config.get("mqtt_brokers", {}) or {}
        letsmesh_config = config.get("letsmesh", {}) or {}
        mqtt_config = config.get("mqtt", {}) or {}
        has_brokers_configured = (
            bool(mqtt_brokers_config.get("brokers")) or bool(letsmesh_config) or bool(mqtt_config)
        )
        if has_brokers_configured and local_identity:
            try:
                # Pass local_identity directly (supports both standard and firmware keys)
                self.mqtt_handler = MeshCoreToMqttPusher(
                    local_identity=local_identity,
                    config=config,
                    stats_provider=self._get_live_stats,
                    radio_stats_provider=self._get_radio_stats,
                    usage_recorder=self.network_usage.record_mqtt,
                )
                self.mqtt_handler.connect()

                public_key_hex = local_identity.get_public_key().hex()
                logger.info(f"MQTT handler initialized with public key: {public_key_hex[:16]}...")
            except Exception as e:
                logger.error(f"Failed to initialize MQTT handler: {e}")
                self.mqtt_handler = None
        else:
            logger.info("MQTT handler disabled - no brokers configured")

        # Initialize hardware stats collector
        from .hardware_stats import HardwareStatsCollector

        self.hardware_stats = HardwareStatsCollector()
        logger.info("Hardware stats collector initialized")

        # Initialize WebSocket handler for real-time updates
        self.websocket_available = False
        self.websocket_has_connected_clients = lambda: False
        # Wired by the daemon once the advert helper exists; returns the
        # rate-limit stats dict the sidebar's advert tier reads.
        self.advert_stats_getter = None
        self.modem_status_getter: Callable[[], list[str]] | None = None
        self._last_noise_floor_dbm: Optional[float] = None
        self._stats_broadcast_seq = 0
        self._ws_stats_broadcast_interval_sec: float = 5.0
        self._stats_stop_event = threading.Event()
        self._stats_thread: Optional[threading.Thread] = None
        try:
            from .websocket_handler import (
                broadcast_packet,
                broadcast_stats,
                has_connected_clients,
            )

            self.websocket_broadcast_packet = broadcast_packet
            self.websocket_broadcast_stats = broadcast_stats
            self.websocket_has_connected_clients = has_connected_clients
            self.websocket_available = True
            logger.info("WebSocket handler initialized for real-time updates")

            # Broadcast aggregate stats on a fixed cadence rather than inline on the
            # per-packet write path. get_packet_stats(24h) is a multi-second aggregate;
            # running it inside _record_packet_blocking made the storage writer thread
            # spend ~1-2s of every 5s on it, competing with packet inserts. A dedicated
            # tick keeps the writer doing only fast writes and only runs the aggregate
            # when a dashboard client is actually connected.
            self._stats_thread = threading.Thread(
                target=self._stats_broadcast_loop,
                name="stats-broadcast",
                daemon=True,
            )
            self._stats_thread.start()
        except ImportError:
            logger.debug("WebSocket handler not available")

    def _track_task(self, task: asyncio.Task):
        """Track background task for lifecycle management and error handling."""
        self._pending_tasks.add(task)

        def on_done(t: asyncio.Task):
            self._pending_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Background task error: {e}", exc_info=True)

        task.add_done_callback(on_done)

    def _schedule_background(self, coro_factory, *args, sync_fallback=None):
        """Schedule a coroutine if a loop exists; otherwise run sync fallback."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if sync_fallback is not None:
                sync_fallback(*args)
            return

        task = loop.create_task(coro_factory(*args))
        self._track_task(task)

    def _get_live_stats(self) -> dict:
        """Get live stats from RepeaterHandler"""
        if not self.repeater_handler:
            return {
                "uptime_secs": 0,
                "packets_sent": 0,
                "packets_received": 0,
                "errors": 0,
                "queue_len": 0,
            }

        uptime_secs = int(time.time() - self.repeater_handler.start_time)

        # Get airtime stats -- the node's, not the default radio's channel, so
        # a bridge's stored history covers every radio it transmits on.
        airtime_stats = _node_airtime_stats(self.repeater_handler) or {}

        # The default radio's last reading, from memory rather than the newest
        # row in the table. Every radio's sample is stored now and the default is
        # sampled first, so the newest row on a Fabric node is the *other*
        # radio's -- the opposite of the figure a status message reports. The
        # engine holds the default radio's reading, which is what /stats reads.
        noise_floor = None
        cached_noise_floor = getattr(self.repeater_handler, "get_cached_noise_floor", None)
        if callable(cached_noise_floor):
            try:
                noise_floor = cached_noise_floor()
            except Exception as e:
                logger.debug(f"Could not read cached noise floor: {e}")

        # Receive errors: CRC failures, the only error the node actually counts,
        # summed over its radios so this reconciles with radios[].errors below.
        # It was published as a literal 0 for as long as the field has existed,
        # so an observer could not tell a quiet node from a deaf one.
        errors = 0
        crc_error_count = getattr(self.repeater_handler, "get_crc_error_count", None)
        if callable(crc_error_count):
            try:
                errors = int(crc_error_count() or 0)
            except Exception as e:
                logger.debug(f"Could not read CRC error count: {e}")

        stats = {
            "uptime_secs": uptime_secs,
            "packets_sent": self.repeater_handler.forwarded_count,
            "packets_received": self.repeater_handler.rx_count,
            "errors": errors,
            "queue_len": 0,  # N/A for Python repeater
        }

        # Add airtime stats
        if airtime_stats:
            stats["tx_air_secs"] = int(airtime_stats["total_airtime_ms"] / 1000)
            stats["rx_air_secs"] = int(airtime_stats.get("total_rx_airtime_ms", 0) / 1000)
            stats["current_airtime_ms"] = airtime_stats["current_airtime_ms"]
            stats["utilization_percent"] = airtime_stats["utilization_percent"]

        # Add noise floor if available
        if noise_floor is not None:
            stats["noise_floor"] = noise_floor

        return stats

    def _get_radio_stats(self) -> dict:
        """``{radio_id: {...}}`` for the status message's radio map.

        Empty on a single-radio node, whose one radio is already what every
        figure in ``stats`` describes. On a bridge the node-wide figures cannot
        say which side is carrying the traffic or which side has gone deaf, so
        each radio reports its own, under the same field names ``stats`` uses.

        Each source contributes independently: a radio with a noise floor but no
        airtime budget still reports the noise floor. Airtime is whole seconds
        here too, matching the node-level counters.
        """
        if not self.repeater_handler:
            return {}

        radios: dict = {}

        def entry(radio_id) -> dict:
            return radios.setdefault(str(radio_id), {})

        for source, apply_to in (
            ("airtime_stats_by_radio", self._apply_radio_airtime),
            ("get_cached_noise_floor_by_radio", self._apply_radio_noise_floor),
            ("get_crc_error_count_by_radio", self._apply_radio_errors),
        ):
            getter = getattr(self.repeater_handler, source, None)
            if not callable(getter):
                continue
            try:
                apply_to(getter(), entry)
            except Exception as e:
                logger.debug(f"Could not read {source} for the status radio map: {e}")

        return radios

    @staticmethod
    def _apply_radio_airtime(per_radio: list, entry) -> None:
        for radio in per_radio or []:
            radio_id = radio.get("radio_id")
            if radio_id is None:
                continue
            entry(radio_id).update(
                {
                    "tx_air_secs": int(radio.get("total_airtime_ms", 0) / 1000),
                    "rx_air_secs": int(radio.get("total_rx_airtime_ms", 0) / 1000),
                    "current_airtime_ms": radio.get("current_airtime_ms", 0),
                    "utilization_percent": radio.get("utilization_percent", 0),
                }
            )

    @staticmethod
    def _apply_radio_noise_floor(by_radio: dict, entry) -> None:
        for radio_id, noise_floor_dbm in (by_radio or {}).items():
            if noise_floor_dbm is not None:
                entry(radio_id)["noise_floor"] = noise_floor_dbm

    @staticmethod
    def _apply_radio_errors(by_radio: dict, entry) -> None:
        for radio_id, count in (by_radio or {}).items():
            entry(radio_id)["errors"] = int(count)

    def record_packet(
        self,
        packet_record: dict,
        skip_mqtt: bool = False,
        tx_egress: Optional[list] = None,
    ):
        """Record a packet to storage and publish it.

        All blocking work — the SQLite write, the cumulative-counts aggregate, the
        RRD update, and network publishing — runs on the dedicated writer thread so
        it never blocks the asyncio event loop. Callers treat this as
        fire-and-forget (the previous synchronous version blocked the loop).

        Args:
            packet_record: Dictionary containing packet information
            skip_mqtt: The caller determined this packet is invalid (it could not
                be parsed); withhold it from the brokers. Classifying a packet is
                the caller's job — a drop_reason alone does not mean invalid, as
                duplicates, policy drops and traces all carry one.
            tx_egress: One entry per physical send on a node with two or more
                radios, stored beside the packet. Kept off packet_record so it
                does not ride along on every websocket, Glass and MQTT publish.
        """
        logger.debug(
            f"Recording packet: type={packet_record.get('type')}, "
            f"transmitted={packet_record.get('transmitted')}"
        )
        self._submit_db(self._record_packet_blocking, packet_record, skip_mqtt, tx_egress)

    def _submit_db(self, fn, *args):
        """Run a blocking storage operation on the dedicated writer thread.

        Falls back to running inline only if the executor has already been shut
        down (process teardown), so late records are not silently dropped.
        """
        try:
            self._db_executor.submit(self._run_db_task, fn, *args)
        except RuntimeError:
            self._run_db_task(fn, *args)

    def _run_db_task(self, fn, *args):
        """Execute a writer-thread task, logging (not raising) on failure."""
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"Storage writer task failed: {e}", exc_info=True)

    def _record_packet_blocking(
        self, packet_record: dict, skip_mqtt: bool, tx_egress: Optional[list] = None
    ):
        """Store, aggregate, update metrics, and publish one packet (writer thread)."""
        packet_id = self.sqlite_handler.store_packet(packet_record)
        if packet_id is not None:
            packet_record["id"] = packet_id
            if tx_egress:
                self.sqlite_handler.store_packet_egress(
                    packet_id, packet_record.get("timestamp", time.time()), tx_egress
                )

        if self.rrd_handler is not None:
            cumulative_counts = self.sqlite_handler.get_cumulative_counts()
            self.rrd_handler.update_packet_metrics(packet_record, cumulative_counts)

        self._publish_packet_sync(packet_record, skip_mqtt)

    def _publish_packet_sync(self, packet_record: dict, skip_mqtt: bool):
        """Publish a single packet (glass, per-packet WebSocket event, MQTT).

        Only fast, per-packet work runs here. The aggregate stats broadcast is
        driven separately by _stats_broadcast_loop so the writer thread is not
        held by the multi-second get_packet_stats(24h) query.

        ``skip_mqtt`` withholds a packet the caller judged invalid (a malformed
        advert, an empty payload, an over-long path) from the brokers only. It
        is still stored and still reaches Glass and the dashboard, which are
        the surfaces an operator debugs their own RF from; what it must not do
        is feed a network-wide observer a packet this node could not parse.

        The caller's judgement is taken as final here. Re-deriving it from
        ``drop_reason`` would silence traces, duplicates and policy drops,
        which all carry a reason and are all packets an observer wants.
        """
        self._publish_to_glass(packet_record, "packet")

        if self.websocket_available:
            try:
                self.websocket_broadcast_packet(packet_record)
            except Exception as e:
                logger.debug(f"WebSocket broadcast failed: {e}")

        if skip_mqtt:
            logger.debug(
                "Skipping mqtt publish for invalid packet: %s",
                packet_record.get("drop_reason"),
            )
            return

        self._publish_packet_to_mqtt(packet_record)

    # The heavy 24h SQL aggregate rides one beat in six (30s at the 5s
    # default): nobody reads a 24h cumulative at 5s resolution, and the
    # sidebar vitals it used to travel with are cheap in-memory scalars.
    PACKET_STATS_EVERY_N_BEATS = 6

    def _broadcast_stats_once(self) -> None:
        """Broadcast the sidebar vitals; the 24h aggregate is decimated.

        The beat carries scalars only — a few hundred bytes — so the
        dashboard sidebar can read every vital live on any page. Series
        (noise-floor history, charts) stay on their own slow HTTP paths.
        """
        system_stats: Dict[str, Any] = {
            "uptime_seconds": (
                time.time() - self.repeater_handler.start_time if self.repeater_handler else 0
            ),
            "mode": self.config.get("repeater", {}).get("mode", "forward"),
        }
        if self.modem_status_getter is not None:
            system_stats["modem_disconnected"] = self.modem_status_getter()
        airtime_stats = _node_airtime_stats(self.repeater_handler)
        if airtime_stats:
            system_stats["utilization_percent"] = airtime_stats["utilization_percent"]
        if self._last_noise_floor_dbm is not None:
            system_stats["noise_floor_dbm"] = self._last_noise_floor_dbm
        if self.advert_stats_getter is not None:
            try:
                tier = self.advert_stats_getter()
                system_stats["advert_tier"] = {
                    "current_tier": tier.get("adaptive", {}).get("current_tier"),
                    "adverts_allowed": tier.get("stats", {}).get("adverts_allowed", 0),
                    "adverts_dropped": tier.get("stats", {}).get("adverts_dropped", 0),
                    "active_penalties": len(tier.get("active_penalties") or {}),
                }
            except Exception as e:
                logger.debug(f"Advert stats unavailable for broadcast: {e}")

        payload: Dict[str, Any] = {"system_stats": system_stats}
        if self._stats_broadcast_seq % self.PACKET_STATS_EVERY_N_BEATS == 0:
            payload["packet_stats"] = self.sqlite_handler.get_packet_stats(
                hours=24, radio_profiles=self._radio_profiles()
            )
        self._stats_broadcast_seq += 1

        self.websocket_broadcast_stats(payload)

    def _stats_broadcast_loop(self) -> None:
        """Broadcast aggregate stats every interval while clients are connected.

        Runs on its own thread (off the event loop and off the storage writer) so
        the heavy get_packet_stats(24h) aggregate never sits in the packet write
        path. Skips the query entirely when no dashboard client is connected.
        """
        while not self._stats_stop_event.wait(self._ws_stats_broadcast_interval_sec):
            try:
                if self.websocket_has_connected_clients():
                    self._broadcast_stats_once()
            except Exception as e:
                logger.debug(f"Stats broadcast failed: {e}")

    def _publish_packet_to_mqtt(self, packet_record: dict):
        """Publish packet to mqtt broker if enabled and allowed.

        The ``duration`` field in the published JSON is sourced from
        ``packet_record['airtime_ms']``, populated upstream by
        RepeaterHandler._build_packet_record using the Semtech reference
        time-on-air formula. No recomputation is needed here.

        On a multi-radio node the payload also carries the ingress radio and
        every successful egress, which an observer resolves to frequencies
        through the ``radios`` map in this node's status message.
        """
        if not self.mqtt_handler:
            return

        try:
            packet_type = packet_record.get("type")
            if packet_type is None:
                logger.error("Cannot publish to mqtt: packet_record missing 'type' field")
                return

            node_name = self.config.get("repeater", {}).get("node_name", "Unknown")
            packet = PacketRecord.from_packet_record(
                packet_record,
                origin=node_name,
                origin_id=self.mqtt_handler.public_key,
                include_radio_ids=self._multi_radio,
            )

            if packet:
                self.mqtt_handler.publish_packet(packet.to_dict())
                logger.debug(f"Published packet type 0x{packet_type:02X} to mqtt")
            else:
                logger.debug("Skipped mqtt publish: packet missing raw_packet data")

        except Exception as e:
            logger.error(f"Failed to publish packet to mqtt: {e}", exc_info=True)

    def record_advert(self, advert_record: dict):
        """Record advert to storage and defer network publishing to background tasks."""
        self.sqlite_handler.store_advert(advert_record)
        self._schedule_background(
            self._deferred_publish_advert,
            advert_record,
            sync_fallback=self._publish_advert_sync,
        )

    async def _deferred_publish_advert(self, advert_record: dict):
        """Deferred background task for advert publishing."""
        try:
            self._publish_advert_sync(advert_record)
        except Exception as e:
            logger.error(f"Deferred advert publish failed: {e}", exc_info=True)

    def _publish_advert_sync(self, advert_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(advert_record, "advert")
        self._publish_to_glass(advert_record, "advert")

    def record_noise_floor(
        self,
        noise_floor_dbm: float,
        radio_id: Optional[str] = None,
        *,
        publish: bool = True,
    ):
        """Record noise floor to storage and defer network publishing to background tasks.

        ``radio_id`` is the radio the sample was read from, NULL on a
        single-radio node. Only the default radio's sample is published: the
        observer feed and Glass carry one noise floor per node, and doubling
        that cadence is a change of its own (per-radio publishing is not in this
        work). The published record keeps its existing shape.
        """
        noise_record = {"timestamp": time.time(), "noise_floor_dbm": noise_floor_dbm}
        self.sqlite_handler.store_noise_floor({**noise_record, "radio_id": radio_id})
        if not publish:
            return
        self._last_noise_floor_dbm = noise_floor_dbm
        self._schedule_background(
            self._deferred_publish_noise_floor,
            noise_record,
            sync_fallback=self._publish_noise_floor_sync,
        )

    async def _deferred_publish_noise_floor(self, noise_record: dict):
        """Deferred background task for noise floor publishing."""
        try:
            self._publish_noise_floor_sync(noise_record)
        except Exception as e:
            logger.error(f"Deferred noise floor publish failed: {e}", exc_info=True)

    def _publish_noise_floor_sync(self, noise_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(noise_record, "noise_floor")
        self._publish_to_glass(noise_record, "noise_floor")

    def record_crc_errors(
        self,
        count: int,
        radio_id: Optional[str] = None,
        *,
        publish: bool = True,
    ):
        """Record a batch of CRC errors detected since last poll and defer publishing.

        Publishing follows the same rule as ``record_noise_floor``: every radio's
        delta is stored, only the default radio's is published.
        """
        crc_record = {"timestamp": time.time(), "count": count}
        self.sqlite_handler.store_crc_errors({**crc_record, "radio_id": radio_id})
        if not publish:
            return
        self._schedule_background(
            self._deferred_publish_crc_errors,
            crc_record,
            sync_fallback=self._publish_crc_errors_sync,
        )

    async def _deferred_publish_crc_errors(self, crc_record: dict):
        """Deferred background task for CRC error publishing."""
        try:
            self._publish_crc_errors_sync(crc_record)
        except Exception as e:
            logger.error(f"Deferred CRC errors publish failed: {e}", exc_info=True)

    def _publish_crc_errors_sync(self, crc_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(crc_record, "crc_errors")
        self._publish_to_glass(crc_record, "crc_errors")

    def get_crc_error_count(self, hours: int = 24, radio_id: Optional[str] = None) -> int:
        return self.sqlite_handler.get_crc_error_count(hours, radio_id=radio_id)

    def get_crc_error_history(
        self,
        hours: int = 24,
        limit: int = None,
        radio_id: Optional[str] = None,
        radio_profiles: Optional[list] = None,
    ) -> list:
        return self.sqlite_handler.get_crc_error_history(
            hours, limit, radio_id=radio_id, radio_profiles=radio_profiles
        )

    def get_policy_event_counts(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
    ) -> list:
        return self.sqlite_handler.get_policy_event_counts(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bucket_seconds=bucket_seconds,
        )

    def get_lbt_diagnostics(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 300,
        severe_attempt_threshold: int = 4,
        radio_profiles: Optional[list] = None,
    ) -> dict:
        return self.sqlite_handler.get_lbt_diagnostics(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bucket_seconds=bucket_seconds,
            severe_attempt_threshold=severe_attempt_threshold,
            radio_profiles=radio_profiles,
        )

    def _radio_profiles(self) -> Optional[list]:
        """Air settings of the configured radios, read from the live config."""
        try:
            return build_radio_profiles(self.config)
        except Exception as e:
            logger.debug(f"Radio profiles unavailable for packet stats: {e}")
            return None

    def get_packet_stats(self, hours: int = 24, radio_profiles: Optional[list] = None) -> dict:
        return self.sqlite_handler.get_packet_stats(hours, radio_profiles=radio_profiles)

    def get_recent_packets(self, limit: int = 100) -> list:
        return self.sqlite_handler.get_recent_packets(limit)

    def get_filtered_packets(
        self,
        packet_type: Optional[int] = None,
        route: Optional[int] = None,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list:
        return self.sqlite_handler.get_filtered_packets(
            packet_type, route, start_timestamp, end_timestamp, limit, offset
        )

    def get_airtime_data(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 50000,
    ) -> list:
        return self.sqlite_handler.get_airtime_data(start_timestamp, end_timestamp, limit)

    def get_airtime_buckets(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
        sf: int = 9,
        bw_hz: int = 62500,
        cr: int = 5,
        preamble: int = 17,
        radio_profiles: Optional[list] = None,
    ) -> dict:
        return self.sqlite_handler.get_airtime_buckets(
            start_timestamp,
            end_timestamp,
            bucket_seconds,
            sf,
            bw_hz,
            cr,
            preamble,
            radio_profiles=radio_profiles,
        )

    def get_radio_packet_rates(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 3600,
        radio_profiles: Optional[list] = None,
    ) -> dict:
        return self.sqlite_handler.get_radio_packet_rates(
            start_timestamp, end_timestamp, bucket_seconds, radio_profiles=radio_profiles
        )

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_hash(packet_hash)

    def get_packet_by_id(self, packet_id: int) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_id(packet_id)

    def get_neighbor_link_history(
        self,
        *,
        peer_hash: str,
        path_hash_size: int,
        hours: int = 24,
        limit: int = 1000,
        bucket_seconds: Optional[int] = None,
        radio_id: Optional[str] = None,
        by_radio: bool = False,
    ) -> list:
        return self.sqlite_handler.get_neighbor_link_history(
            peer_hash=peer_hash,
            path_hash_size=path_hash_size,
            hours=hours,
            limit=limit,
            bucket_seconds=bucket_seconds,
            radio_id=radio_id,
            by_radio=by_radio,
        )

    def get_rrd_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> Optional[dict]:
        return self.get_metrics_data(start_time, end_time, resolution)

    def get_metrics_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> dict:
        if self.rrd_handler is not None:
            try:
                rrd_data = self.rrd_handler.get_data(start_time, end_time, resolution)
            except Exception as e:
                logger.warning(
                    f"RRDtool metrics read failed; using SQLite metrics fallback: {e}",
                    exc_info=True,
                )
            else:
                if self._metrics_data_is_valid(rrd_data):
                    rrd_data.setdefault("data_source", "rrd")
                    return rrd_data

                logger.warning(
                    "RRDtool metrics read returned no usable data; using SQLite metrics fallback"
                )

        sqlite_data = self.sqlite_handler.get_metrics_data(start_time, end_time, resolution)
        sqlite_data.setdefault("data_source", "sqlite")
        return sqlite_data

    def _metrics_data_is_valid(self, metrics_data: Optional[dict]) -> bool:
        if not isinstance(metrics_data, dict):
            return False
        if not isinstance(metrics_data.get("metrics"), dict):
            return False
        if not isinstance(metrics_data.get("timestamps"), list):
            return False
        return True

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        if self.rrd_handler is not None:
            try:
                rrd_stats = self.rrd_handler.get_packet_type_stats(hours)
            except Exception as e:
                logger.warning(
                    f"RRDtool packet type stats failed; using SQLite fallback: {e}",
                    exc_info=True,
                )
            else:
                if rrd_stats:
                    return rrd_stats

            logger.warning("Falling back to SQLite for packet type stats")
        return self.sqlite_handler.get_packet_type_stats(hours)

    def get_route_stats(self, hours: int = 24, radio_profiles: Optional[list] = None) -> dict:
        return self.sqlite_handler.get_route_stats(hours, radio_profiles=radio_profiles)

    def get_neighbors(self) -> dict:
        return self.sqlite_handler.get_neighbors()

    def get_neighbor_scopes(self) -> dict:
        return self.sqlite_handler.get_neighbor_scopes()

    def record_neighbor_scope(
        self,
        pubkey: str,
        status: str,
        scopes: Optional[str] = None,
        queried_at: Optional[float] = None,
    ) -> bool:
        return self.sqlite_handler.record_neighbor_scope(pubkey, status, scopes, queried_at)

    def get_daemon_state(self, key: str) -> Optional[dict]:
        return self.sqlite_handler.get_daemon_state(key)

    def set_daemon_state(self, key: str, value: dict) -> bool:
        return self.sqlite_handler.set_daemon_state(key, value)

    def get_node_name_by_pubkey(self, pubkey: str) -> Optional[str]:
        """
        Lookup node name from adverts table by public key.

        Args:
            pubkey: Public key in hex string format

        Returns:
            Node name if found, None otherwise
        """
        try:
            import sqlite3

            with sqlite3.connect(self.sqlite_handler.sqlite_path) as conn:
                result = conn.execute(
                    "SELECT node_name FROM adverts WHERE pubkey = ? AND node_name IS NOT NULL ORDER BY last_seen DESC LIMIT 1",
                    (pubkey,),
                ).fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.debug(f"Could not lookup node name for {pubkey[:8] if pubkey else 'None'}: {e}")
            return None

    def cleanup_old_data(self, days: int = 7, companion_events_days: Optional[int] = None):
        self.sqlite_handler.cleanup_old_data(days, companion_events_days=companion_events_days)

    def get_noise_floor_history(
        self,
        hours: int = 24,
        limit: int = None,
        offset: int = 0,
        radio_id: Optional[str] = None,
        radio_profiles: Optional[list] = None,
    ) -> list:
        return self.sqlite_handler.get_noise_floor_history(
            hours, limit, offset, radio_id=radio_id, radio_profiles=radio_profiles
        )

    def get_noise_floor_stats(self, hours: int = 24, radio_id: Optional[str] = None) -> dict:
        return self.sqlite_handler.get_noise_floor_stats(hours, radio_id=radio_id)

    def close(self):
        # Stop the stats broadcast thread.
        self._stats_stop_event.set()
        if self._stats_thread is not None:
            self._stats_thread.join(timeout=2)

        # Drain and stop the storage writer thread first so pending writes and
        # publishes complete before MQTT and the DB connections are torn down.
        self._db_executor.shutdown(wait=True)

        # Cancel all pending background tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()

        if self.mqtt_handler:
            try:
                self.mqtt_handler.disconnect()
                logger.info("MQTT handler disconnected")
            except Exception as e:
                logger.error(f"Error disconnecting MQTT handler: {e}")

        usage = getattr(self, "network_usage", None)
        if usage is not None:
            usage.stop()

    def set_glass_publisher(self, publish_callback):
        self.glass_publish_callback = publish_callback

    def _publish_to_glass(self, record: dict, record_type: str):
        if not self.glass_publish_callback:
            return
        try:
            self.glass_publish_callback(record_type, record)
        except Exception as e:
            logger.debug(f"Failed to publish telemetry to Glass MQTT: {e}")

    def create_transport_key(
        self,
        name: str,
        flood_policy: str,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> Optional[int]:
        return self.sqlite_handler.create_transport_key(
            name, flood_policy, transport_key, parent_id, last_used
        )

    def get_transport_keys(self) -> list:
        return self.sqlite_handler.get_transport_keys()

    def get_transport_key_by_id(self, key_id: int) -> Optional[dict]:
        return self.sqlite_handler.get_transport_key_by_id(key_id)

    def update_transport_key(
        self,
        key_id: int,
        name: Optional[str] = None,
        flood_policy: Optional[str] = None,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> bool:
        return self.sqlite_handler.update_transport_key(
            key_id, name, flood_policy, transport_key, parent_id, last_used
        )

    def delete_transport_key(self, key_id: int) -> bool:
        return self.sqlite_handler.delete_transport_key(key_id)

    def delete_advert(self, advert_id: int) -> bool:
        return self.sqlite_handler.delete_advert(advert_id)

    def delete_neighbors(self, pubkey_prefix: str | None = None) -> int:
        return self.sqlite_handler.delete_neighbors(pubkey_prefix)

    def get_hardware_stats(self) -> Optional[dict]:
        """Get current hardware statistics"""
        try:
            return self.hardware_stats.get_stats()
        except Exception as e:
            logger.error(f"Error getting hardware stats: {e}")
            return None

    def get_hardware_processes(self) -> Optional[list]:
        """Get current process summary"""
        try:
            return self.hardware_stats.get_processes_summary()
        except Exception as e:
            logger.error(f"Error getting hardware processes: {e}")
            return None
