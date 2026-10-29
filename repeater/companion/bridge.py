"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Mapping
from enum import Enum
from typing import Any, Callable, Optional

from openhop_core.companion import CompanionBridge
from openhop_core.companion.constants import TXT_TYPE_CLI_COMMAND, TXT_TYPE_CLI_DATA
from openhop_core.companion.models import MessageEvent

from repeater import __version__ as repeater_version

logger = logging.getLogger("RepeaterCompanionBridge")

# How long stop() lets queued history rows be written: inside the daemon's 5 s
# shutdown step.
_HISTORY_FLUSH_TIMEOUT_SEC = 3.0


def _prefs_bytes_from_json(value: Any) -> bytes:
    """Restore a ``bytes`` NodePrefs field from JSON (hex string from :func:`_to_json_safe`)."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return b""
        try:
            return bytes.fromhex(s)
        except ValueError:
            logger.debug("Invalid hex for prefs bytes field (prefix %r)", s[:32])
            return b""
    return b""


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form (avoids TypeError from enums, bytes, etc.)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


class RepeaterCompanionBridge(CompanionBridge):
    """CompanionBridge that persists and loads prefs (full NodePrefs) via SQLite JSON blob."""

    def __init__(
        self,
        identity,
        packet_injector: Callable[..., Any],
        node_name: str = "pyMC",
        adv_type: int = 1,
        max_contacts: int = 1000,
        max_channels: int = 40,
        offline_queue_size: int = 512,
        radio_config: Optional[dict] = None,
        authenticate_callback: Optional[Callable[..., tuple[bool, int]]] = None,
        initial_contacts: Optional[Any] = None,
        *,
        radio_settings_getter: Optional[Callable[[], Mapping[str, Any]]] = None,
        max_tx_power_getter: Optional[Callable[[], Optional[int]]] = None,
        sqlite_handler=None,
        companion_hash: str = "",
        on_prefs_saved: Optional[Callable[[str], None]] = None,
        message_history: bool = False,
    ) -> None:
        self._sqlite_handler = sqlite_handler
        self._companion_hash = companion_hash
        self._on_prefs_saved = on_prefs_saved
        self.message_history = bool(message_history and sqlite_handler)
        # History rows wait here while another connection holds SQLite's write lock
        # (a VACUUM, a purge): as many as the offline queue holds by default.
        self._history_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._history_writer: Optional[asyncio.Task] = None
        super().__init__(
            identity=identity,
            packet_injector=packet_injector,
            node_name=node_name,
            adv_type=adv_type,
            max_contacts=max_contacts,
            max_channels=max_channels,
            offline_queue_size=offline_queue_size,
            radio_config=radio_config,
            authenticate_callback=authenticate_callback,
            initial_contacts=initial_contacts,
            radio_settings_getter=radio_settings_getter,
            max_tx_power_getter=max_tx_power_getter,
        )
        # The companion CLI's `ver` names the openHop software; add the repeater
        # ahead of the core. An older core has no companion CLI to tell.
        add_version = getattr(getattr(self, "cli", None), "add_software_version", None)
        if callable(add_version):
            try:
                add_version("repeater", repeater_version)
            except Exception as e:
                # Cosmetic: never let it stop the companion loading.
                logger.warning("Could not register the repeater version with the CLI: %s", e)
        if self.message_history:
            # Subscribed here rather than in the frame server's persistence hook:
            # core calls that only for a message the offline queue kept, and the
            # queue keeps none at offline_queue_size 0 or when full of direct ones.
            self.on_message_event(self._record_history)
            self.on_channel_message_event(self._record_history)
            self.on_channel_data_event(self._record_history)
            # What every client sends. An older core does not report it.
            on_message_sent = getattr(self, "on_message_sent", None)
            if callable(on_message_sent):
                on_message_sent(self._record_sent)

    def _record_history(self, event) -> None:
        """Record a received MessageEvent, ChannelMessageEvent or ChannelDataEvent.

        A field the event type lacks takes the value the frame queue stores for it.
        """
        # CLI traffic is remote administration, not conversation, and a reply can
        # echo a secret: MeshCore's CommonCLI answers "password <new>" with
        # "password now: <new>". Core does not report the CLI commands sent.
        if getattr(event, "txt_type", 0) in (TXT_TYPE_CLI_DATA, TXT_TYPE_CLI_COMMAND):
            return
        msg = {
            "sender_key": getattr(event, "sender_key", b""),
            "txt_type": getattr(event, "txt_type", 0),
            "timestamp": getattr(event, "timestamp", 0),
            "text": getattr(event, "text", ""),
            "is_channel": not isinstance(event, MessageEvent),
            "channel_idx": getattr(event, "channel_idx", 0),
            "path_len": event.path_len,
            "sender_prefix": getattr(event, "sender_prefix", b""),
            "snr": event.snr,
            "rssi": event.rssi,
            "channel_data_type": getattr(event, "data_type", 0),
            "channel_data_payload": getattr(event, "payload", b""),
            "packet_hash": event.packet_hash,
        }
        self._queue_history(msg)

    def _record_sent(self, event) -> None:
        """Record a SentMessageEvent, whose sender is this companion."""
        msg = {
            "sender_key": self.get_public_key(),
            "recipient_key": event.recipient_key or b"",
            "txt_type": event.txt_type,
            "timestamp": event.timestamp,
            "text": event.text,
            "is_channel": event.channel_idx is not None,
            "channel_idx": event.channel_idx or 0,
            "expected_ack": event.expected_ack,
        }
        self._queue_history(msg)

    def _queue_history(self, msg: dict) -> None:
        """Queue a row for the history writer.

        Synchronous so that core, which awaits each callback in turn, sends the
        packet and runs later callbacks without waiting for SQLite.
        """
        if self._history_writer is None:
            self._history_writer = asyncio.create_task(self._write_history())
        try:
            self._history_queue.put_nowait((self.get_public_key(), msg))
        except asyncio.QueueFull:
            logger.warning(
                "Companion %s: message history queue full (%d), dropping a message",
                self._companion_hash,
                self._history_queue.maxsize,
            )

    async def _write_history(self) -> None:
        """Write queued rows in order."""
        while True:
            public_key, msg = await self._history_queue.get()
            await asyncio.to_thread(self._sqlite_handler.companion_record_history, public_key, msg)
            self._history_queue.task_done()

    async def stop(self) -> None:
        """Stop, then give queued history rows _HISTORY_FLUSH_TIMEOUT_SEC to be written.

        The writer is left running: the router still delivers messages while it
        drains, after the bridges stop, and asyncio.run() ends the writer with the loop.
        """
        await super().stop()
        if self._history_writer is None:
            return
        try:
            await asyncio.wait_for(self._history_queue.join(), _HISTORY_FLUSH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "Companion %s: %d message(s) not written to the history before stopping",
                self._companion_hash,
                # The writer holds the row it is trying to write.
                self._history_queue.qsize() + 1,
            )

    def _save_prefs(self) -> None:
        """Persist full NodePrefs as JSON to SQLite."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            prefs_dict = dataclasses.asdict(self.prefs)
            prefs_safe = _to_json_safe(prefs_dict)
            self._sqlite_handler.companion_save_prefs(str(self._companion_hash), prefs_safe)
            if self._on_prefs_saved:
                try:
                    self._on_prefs_saved(self.prefs.node_name)
                except Exception as e:
                    logger.warning("Failed to sync node_name to config: %s", e)
        except Exception as e:
            logger.warning("Failed to persist companion prefs: %s", e)

    def _load_prefs(self) -> None:
        """Load prefs from SQLite JSON and merge into self.prefs (only known keys)."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            stored = self._sqlite_handler.companion_load_prefs(self._companion_hash)
            if not stored or not isinstance(stored, dict):
                return
            for key, value in stored.items():
                if not hasattr(self.prefs, key):
                    continue
                current = getattr(self.prefs, key)
                try:
                    if value is None:
                        continue
                    if isinstance(current, bytes):
                        setattr(self.prefs, key, _prefs_bytes_from_json(value))
                        continue
                    if isinstance(current, bool):
                        setattr(self.prefs, key, bool(value))
                    elif isinstance(current, int):
                        setattr(self.prefs, key, int(value))
                    elif isinstance(current, float):
                        setattr(self.prefs, key, float(value))
                    elif isinstance(current, str):
                        setattr(self.prefs, key, str(value))
                    else:
                        setattr(self.prefs, key, value)
                except (TypeError, ValueError) as e:
                    logger.debug("Skip prefs key %r: %s", key, e)
        except Exception as e:
            logger.warning("Failed to load companion prefs: %s", e)
