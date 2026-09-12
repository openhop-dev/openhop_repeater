"""Storage utility classes and functions for data acquisition."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class PacketRecord:
    """
    Data class for packet record format.
    Converts internal packet_record format to standardized publish format.
    Reusable across MQTT and other handlers.
    """

    origin: str
    origin_id: str
    timestamp: str
    type: str
    direction: str
    time: str
    date: str
    len: str
    packet_type: str
    route: str
    payload_len: str
    raw: str
    SNR: str
    RSSI: str
    score: str
    duration: str
    hash: str
    # Multi-radio only: which interface carried this packet. A single-radio
    # node omits both, keeping its payload byte-identical to pre-Fabric
    # builds. The ids are the operator's ``radios[].id`` values, resolved
    # against the ``radios`` map in the status message.
    rx_radio_id: Optional[str] = None
    tx_radio_ids: Optional[List[str]] = None

    @classmethod
    def from_packet_record(
        cls,
        packet_record: dict,
        origin: str,
        origin_id: str,
        include_radio_ids: bool = False,
    ) -> Optional["PacketRecord"]:
        """
        Create PacketRecord from internal packet_record format.

        The ``duration`` field is sourced from ``packet_record['airtime_ms']``,
        which RepeaterHandler._build_packet_record populates using the
        Semtech-reference time-on-air formula on the active radio settings.
        Records produced by older code paths that pre-date that field fall
        back to 0 to preserve legacy behavior.

        ``include_radio_ids`` is set by callers running more than one radio.
        On a single-radio node the ids carry no information an observer cannot
        already read from the status message, so they are left off the wire.

        Args:
            packet_record: Internal packet record dictionary
            origin: Node name
            origin_id: Public key of the node
            include_radio_ids: Emit rx_radio_id / tx_radio_ids (multi-radio nodes)

        Returns:
            PacketRecord instance or None if raw_packet is missing
        """
        if "raw_packet" not in packet_record or not packet_record["raw_packet"]:
            return None

        # Extract timestamp and format date/time
        timestamp = packet_record.get("timestamp", 0)
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        # Format route type (1=Flood->F, 2=Direct->D, etc)
        route_map = {1: "F", 2: "D"}
        route = route_map.get(packet_record.get("route", 0), str(packet_record.get("route", 0)))

        airtime_ms = float(packet_record.get("airtime_ms", 0.0) or 0.0)

        rx_radio_id = None
        tx_radio_ids = None
        if include_radio_ids:
            rx_radio_id = packet_record.get("rx_radio_id") or None
            tx_radio_ids = cls._tx_radio_ids(packet_record)

        return cls(
            origin=origin,
            origin_id=origin_id,
            timestamp=dt.isoformat(),
            type="PACKET",
            direction="rx",
            time=dt.strftime("%H:%M:%S"),
            date=dt.strftime("%-d/%-m/%Y"),
            len=str(len(packet_record["raw_packet"]) // 2),
            packet_type=str(packet_record.get("type", 0)),
            route=route,
            payload_len=str(packet_record.get("payload_length", 0)),
            raw=packet_record["raw_packet"],
            SNR=str(packet_record.get("snr", 0)),
            RSSI=str(packet_record.get("rssi", 0)),
            score=str(int(packet_record.get("score", 0) * 1000)),
            duration=str(int(round(airtime_ms))),
            hash=packet_record.get("packet_hash", ""),
            rx_radio_id=rx_radio_id,
            tx_radio_ids=tx_radio_ids,
        )

    @staticmethod
    def _tx_radio_ids(packet_record: dict) -> Optional[List[str]]:
        """Every radio that successfully transmitted this packet, or None.

        Always a list, even for the single egress of a non-bridge TX, so a
        consumer never has to branch on the JSON type. A packet this node
        heard but did not repeat has no egress at all: the key is then absent
        rather than empty, which is the same statement the record makes.
        """
        ids = packet_record.get("tx_radio_ids")
        if isinstance(ids, (list, tuple)):
            resolved = [str(value) for value in ids if value]
            return resolved or None
        primary = packet_record.get("tx_radio_id")
        return [str(primary)] if primary else None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization.

        Unset optional fields are dropped so a single-radio node publishes the
        exact payload it did before multi-radio attribution existed.
        """
        return {key: value for key, value in asdict(self).items() if value is not None}
