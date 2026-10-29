"""Spectrum sweep engine: step the radio across a band and stream RSSI dwells.

Mirrors CADCalibrationEngine: a worker thread schedules one coroutine per
channel onto the daemon loop, messages go to a queue the SSE endpoint drains,
and the running flag is checked between channels so a stop lands at the next
boundary. The measurement itself is SX1262Radio.measure_rssi_dwell in
openhop_core; the repeater emits its raw samples and derives nothing.

While the mesh radio sweeps the mesh is off the air, so a sweep on that radio
is refused unless the request confirms it. On a multi-radio repeater a sweep
may target a radio other than the default TX radio, and the mesh keeps running.
"""

import asyncio
import logging
import threading
import time
from typing import Any

from repeater.config import NullRadio

from .cad_calibration_engine import CADCalibrationEngine

logger = logging.getLogger("HTTPServer")

DEFAULT_START_HZ = 902_000_000
DEFAULT_STOP_HZ = 928_000_000
DEFAULT_STEP_HZ = 200_000
DEFAULT_DWELL_S = 0.4
MAX_CHANNELS = 4096
CAD_SYMBOL_CHOICES = (1, 2, 4, 8, 16)  # the SX1262's own; measure_rssi_dwell rejects others
MAX_CAD_PER_STEP = 32  # the same cap as cad_manual_check's samples


class SpectrumSweepEngine:
    def __init__(self, daemon_instance=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.running = False
        self.progress = {"current": 0, "total": 0}
        self.results: list[dict] = []
        self.errors = 0
        self.last_error: str | None = None
        self.sweep_thread: threading.Thread | None = None
        self.session_config: dict[str, Any] = {}
        self.message_queue: list = []
        self.radio_id: str | None = None
        self.mesh_suspended = False

    def select_radio(self, radio_id: str | None = None) -> tuple[Any, str, bool]:
        """The physical radio a sweep uses, its id, and whether that suspends the mesh."""
        root = getattr(self.daemon_instance, "radio", None) if self.daemon_instance else None
        if root is None:
            raise LookupError("no radio available")
        fabric = getattr(root, "fabric", None)
        if fabric is not None and hasattr(fabric, "get_radio"):
            default_id = getattr(fabric, "default_radio_id", None)
            rid = str(radio_id) if radio_id else default_id
            if not rid:
                raise LookupError("fabric has no radios registered")
            try:
                return fabric.get_radio(rid), rid, rid == default_id
            except KeyError as exc:
                raise LookupError(f"unknown radio_id: {rid}") from exc
        if radio_id and radio_id != "radio0":
            raise LookupError(f"unknown radio_id: {radio_id}")
        return getattr(root, "_radio", root), "radio0", True

    def describe(self, radio_id: str | None = None) -> dict:
        """Whether a sweep can run here, and what it would cost the mesh."""
        info: dict[str, Any] = {
            "supported": False,
            "reason": None,
            "radio_id": None,
            "mesh_suspended": None,
        }
        radio_status = getattr(self.daemon_instance, "radio_status", None)
        if radio_status not in (None, "ok"):
            info["reason"] = f"radio is {radio_status}"
            return info
        try:
            radio, rid, suspended = self.select_radio(radio_id)
        except LookupError as exc:
            info["reason"] = str(exc)
            return info
        info["radio_id"] = rid
        info["mesh_suspended"] = suspended
        if isinstance(radio, NullRadio) or not callable(getattr(radio, "measure_rssi_dwell", None)):
            info["reason"] = "this radio cannot retune and read RSSI on demand"
            return info
        info["supported"] = True
        return info

    @staticmethod
    def build_plan(start_hz: int, stop_hz: int, step_hz: int) -> list[int]:
        """Channel centres from start (inclusive) to stop (exclusive) every step_hz."""
        start_hz, stop_hz, step_hz = int(start_hz), int(stop_hz), int(step_hz)
        if step_hz < 1_000:
            raise ValueError("step_hz must be at least 1000")
        if stop_hz <= start_hz:
            raise ValueError("stop_hz must be greater than start_hz")
        if (stop_hz - start_hz + step_hz - 1) // step_hz > MAX_CHANNELS:
            raise ValueError(f"more than {MAX_CHANNELS} channels")
        return list(range(start_hz, stop_hz, step_hz))

    def start_sweep(self, config: dict | None = None) -> tuple[bool, Any]:
        if self.running:
            return False, "Sweep already running"
        if self.event_loop is None:
            return False, "Event loop not available"
        cfg = config or {}
        radio_id = cfg.get("radio_id") or None
        described = self.describe(radio_id)
        if not described["supported"]:
            return False, described["reason"]
        try:
            plan = self.build_plan(
                cfg.get("start_hz", DEFAULT_START_HZ),
                cfg.get("stop_hz", DEFAULT_STOP_HZ),
                cfg.get("step_hz", DEFAULT_STEP_HZ),
            )
        except (TypeError, ValueError) as exc:
            return False, f"invalid plan: {exc}"
        try:
            dwell_s = min(5.0, max(0.05, float(cfg.get("dwell_s", DEFAULT_DWELL_S))))
        except (TypeError, ValueError):
            dwell_s = DEFAULT_DWELL_S
        passes = CADCalibrationEngine._normalize_int(cfg.get("passes", 1), 1, 1, 42)
        cad_count = CADCalibrationEngine._normalize_int(
            cfg.get("cad_count", 0), 0, 0, MAX_CAD_PER_STEP
        )
        cad_symbols = cfg.get("cad_symbols")  # None: the radio's own setting
        if cad_symbols is not None:
            cad_symbols = CADCalibrationEngine._normalize_int(cad_symbols, 0, 0, 16)
            if cad_symbols not in CAD_SYMBOL_CHOICES:
                return False, f"cad_symbols must be one of {list(CAD_SYMBOL_CHOICES)}"
        confirmed = CADCalibrationEngine._normalize_bool(cfg.get("confirm_mesh_suspend"), False)
        if described["mesh_suspended"] and not confirmed:
            return False, (
                "this sweep uses the mesh radio and takes the mesh off the air; "
                "set confirm_mesh_suspend to true to proceed"
            )

        self.session_config = {
            "start_hz": plan[0],
            "stop_hz": int(cfg.get("stop_hz", DEFAULT_STOP_HZ)),
            "step_hz": int(cfg.get("step_hz", DEFAULT_STEP_HZ)),
            "dwell_s": dwell_s,
            "passes": passes,
            "cad_symbols": cad_symbols,
            "cad_count": cad_count,
            "channels": len(plan),
        }
        self.radio_id = described["radio_id"]
        self.mesh_suspended = bool(described["mesh_suspended"])
        self.running = True
        self.results = []
        self.errors = 0
        self.last_error = None
        self.progress = {"current": 0, "total": len(plan) * passes}
        self.clear_message_queue()
        self.sweep_thread = threading.Thread(
            target=self.sweep_worker, args=(plan,), name="spectrum-sweep", daemon=True
        )
        self.sweep_thread.start()
        return True, self.status()

    def stop_sweep(self) -> None:
        self.running = False
        thread = self.sweep_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    def status(self, radio_id: str | None = None) -> dict:
        return {
            "running": self.running,
            "progress": dict(self.progress),
            "session": dict(self.session_config),
            "radio_id": self.radio_id,
            "mesh_suspended": self.mesh_suspended,
            "results": len(self.results),
            "errors": self.errors,
            "last_error": self.last_error,
            "capability": self.describe(radio_id),
        }

    def broadcast_to_clients(self, data: Any) -> None:
        self.message_queue.append(data)

    def clear_message_queue(self) -> None:
        self.message_queue.clear()

    def sweep_worker(self, plan: list[int]) -> None:
        cfg = self.session_config
        passes = int(cfg["passes"])
        dwell_s = float(cfg["dwell_s"])
        # Only asked for when wanted, so a core without the CAD arguments still sweeps.
        cad_kwargs = {"cad_count": cfg["cad_count"]} if cfg.get("cad_count") else {}
        if cad_kwargs and cfg.get("cad_symbols") is not None:
            cad_kwargs["cad_symbols"] = cfg["cad_symbols"]
        # Generous on purpose: a CAD the core gives up on costs it about a second.
        step_budget_s = dwell_s + 10.0 + 2.0 * float(cfg.get("cad_count") or 0)
        total = len(plan) * passes
        started = time.monotonic()
        stopped = False
        try:
            radio, rid, _ = self.select_radio(self.radio_id)
            self.broadcast_to_clients(
                {
                    "type": "status",
                    "message": f"Sweep started: {len(plan)} channels x {passes} pass(es)",
                    "radio_id": rid,
                    "mesh_suspended": self.mesh_suspended,
                    "session": dict(cfg),
                }
            )
            current = 0
            for pass_index in range(1, passes + 1):
                for freq_hz in plan:
                    if not self.running:
                        stopped = True
                        break
                    current += 1
                    self.progress = {"current": current, "total": total}
                    self.broadcast_to_clients(
                        {
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "pass_index": pass_index,
                            "freq_hz": int(freq_hz),
                        }
                    )
                    future = asyncio.run_coroutine_threadsafe(
                        radio.measure_rssi_dwell(int(freq_hz), dwell_s=dwell_s, **cad_kwargs),
                        self.event_loop,
                    )
                    try:
                        result = future.result(timeout=step_budget_s)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    if not isinstance(result, dict) or result.get("error"):
                        self.errors += 1
                        reason = result.get("error") if isinstance(result, dict) else "no result"
                        self.last_error = str(reason)
                        self.broadcast_to_clients(
                            {
                                "type": "error",
                                "message": f"dwell failed at {freq_hz}: {reason}",
                                "freq_hz": int(freq_hz),
                                "pass_index": pass_index,
                            }
                        )
                        continue
                    row = {"pass_index": pass_index, **result}
                    self.results.append(row)
                    self.broadcast_to_clients({"type": "result", **row})
                if stopped:
                    break
            elapsed = time.monotonic() - started
            if stopped:
                self.broadcast_to_clients(
                    {"type": "status", "message": "Sweep stopped", "results": len(self.results)}
                )
            else:
                self.broadcast_to_clients(
                    {
                        "type": "completed",
                        "message": "Sweep complete",
                        "channels": len(plan),
                        "passes": passes,
                        "results": len(self.results),
                        "errors": self.errors,
                        "elapsed_s": elapsed,
                    }
                )
        except Exception as exc:
            logger.error(f"Spectrum sweep worker error: {exc}")
            self.last_error = str(exc)
            self.broadcast_to_clients({"type": "error", "message": str(exc)})
        finally:
            self.running = False
