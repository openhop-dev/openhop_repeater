import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from openhop_core.protocol.packet_utils import calculate_lora_airtime_ms

logger = logging.getLogger("AirtimeManager")


class AirtimeManager:
    def __init__(
        self,
        config: dict,
        radio_config: Optional[dict] = None,
        max_airtime_per_minute: Optional[float] = None,
    ):
        """Meter one configured duty-cycle budget.

        ``radio_config`` and ``max_airtime_per_minute`` override the top-level
        ``radio`` and ``duty_cycle`` sections for a Fabric node. Omitted, both
        come from the config as they always have.
        """
        self.config = config
        self.radio_config = radio_config if radio_config is not None else config.get("radio", {})
        self.max_airtime_per_minute = (
            max_airtime_per_minute
            if max_airtime_per_minute is not None
            else config.get("duty_cycle", {}).get("max_airtime_per_minute", 3600)
        )

        # Store radio settings for airtime calculations
        self.refresh_radio_params(self.radio_config)

        # Track airtime in rolling window
        self.tx_history = []  # [(timestamp, airtime_ms), ...]
        self.window_size = 60  # seconds
        self.total_airtime_ms = 0
        self.total_rx_airtime_ms = 0

    def refresh_radio_params(self, radio_config: Optional[dict] = None) -> None:
        """Reload cached modulation params used by airtime estimation.

        Call after a successful live radio reconfiguration. Does not reset
        TX/RX history or duty-cycle totals.
        """
        if radio_config is None:
            radio_config = self.config.get("radio", {}) or {}
        self.radio_config = radio_config
        self.spreading_factor = self.radio_config.get("spreading_factor", 7)
        self.bandwidth = self.radio_config.get("bandwidth", 125000)
        self.coding_rate = self.radio_config.get("coding_rate", 5)
        self.preamble_length = self.radio_config.get("preamble_length", 8)

    def calculate_airtime(
        self,
        payload_len: int,
        spreading_factor: int = None,
        bandwidth_hz: int = None,
        coding_rate: int = None,
        preamble_len: int = None,
        crc_enabled: bool = True,
        explicit_header: bool = True,
    ) -> float:
        """
        Calculate LoRa packet airtime via the shared core estimator.

        Delegates to ``calculate_lora_airtime_ms``, which matches RadioLib's
        ``getTimeOnAir`` (the firmware reference), including its symbol-time
        low-data-rate-optimization auto rule. Coding rate accepts either the
        denominator form (5..8) or the legacy index form (1..4).

        Args:
            payload_len: Payload length in bytes
            spreading_factor: SF7-SF12 (uses config value if None)
            bandwidth_hz: Bandwidth in Hz (uses config value if None)
            coding_rate: CR denominator, 5=4/5, 6=4/6, 7=4/7, 8=4/8 (uses config value if None)
            preamble_len: Preamble symbols (uses config value if None)
            crc_enabled: Whether CRC is enabled (default: True)
            explicit_header: Whether explicit header mode is used (default: True)

        Returns:
            Airtime in milliseconds
        """
        return calculate_lora_airtime_ms(
            payload_len,
            spreading_factor or self.spreading_factor,
            bandwidth_hz or self.bandwidth,
            coding_rate or self.coding_rate,
            preamble_len or self.preamble_length,
            crc_enabled=crc_enabled,
            explicit_header=explicit_header,
        )

    def can_transmit(self, airtime_ms: float) -> Tuple[bool, float]:
        enforcement_enabled = self.config.get("duty_cycle", {}).get("enforcement_enabled", True)
        if not enforcement_enabled:
            # Duty cycle enforcement disabled - always allow
            return True, 0.0

        now = time.time()

        # Remove old entries outside window
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        # Calculate current airtime in window
        current_airtime = sum(at for _, at in self.tx_history)

        if current_airtime + airtime_ms <= self.max_airtime_per_minute:
            return True, 0.0

        # Calculate wait time until oldest entry expires
        if self.tx_history:
            oldest_ts, oldest_at = self.tx_history[0]
            wait_time = (oldest_ts + self.window_size) - now
            return False, max(0, wait_time)

        return False, 1.0

    def record_tx(self, airtime_ms: float):
        self.tx_history.append((time.time(), airtime_ms))
        self.total_airtime_ms += airtime_ms
        logger.debug(f"TX recorded: {airtime_ms: .1f}ms (total: {self.total_airtime_ms: .0f}ms)")

    def record_rx(self, airtime_ms: float):
        """Record received packet airtime (for total RX airtime stats)."""
        self.total_rx_airtime_ms += airtime_ms

    def get_stats(self) -> dict:
        now = time.time()
        self.tx_history = [(ts, at) for ts, at in self.tx_history if now - ts < self.window_size]

        current_airtime = sum(at for _, at in self.tx_history)
        utilization = (current_airtime / self.max_airtime_per_minute) * 100

        return {
            "current_airtime_ms": current_airtime,
            "max_airtime_ms": self.max_airtime_per_minute,
            "utilization_percent": utilization,
            "total_airtime_ms": self.total_airtime_ms,
            "total_rx_airtime_ms": self.total_rx_airtime_ms,
        }


class _RadioAirtimeBudget:
    """A radio-specific calculator backed by a possibly shared ledger."""

    def __init__(
        self,
        ledger: AirtimeManager,
        calculator: AirtimeManager,
        lock: threading.RLock,
    ) -> None:
        self._ledger = ledger
        self._calculator = calculator
        self._lock = lock

    def _replace(self, ledger: AirtimeManager, calculator: AirtimeManager) -> None:
        self._ledger = ledger
        self._calculator = calculator

    def calculate_airtime(self, *args, **kwargs) -> float:
        with self._lock:
            return self._calculator.calculate_airtime(*args, **kwargs)

    def can_transmit(self, airtime_ms: float) -> Tuple[bool, float]:
        with self._lock:
            return self._ledger.can_transmit(airtime_ms)

    def record_tx(self, airtime_ms: float) -> None:
        with self._lock:
            self._ledger.record_tx(airtime_ms)

    def record_rx(self, airtime_ms: float) -> None:
        with self._lock:
            self._ledger.record_rx(airtime_ms)

    def get_stats(self) -> dict:
        with self._lock:
            return self._ledger.get_stats()

    @property
    def max_airtime_per_minute(self) -> float:
        with self._lock:
            return self._ledger.max_airtime_per_minute

    @property
    def tx_history(self) -> list:
        with self._lock:
            return self._ledger.tx_history

    @property
    def total_airtime_ms(self) -> float:
        with self._lock:
            return self._ledger.total_airtime_ms

    @property
    def total_rx_airtime_ms(self) -> float:
        with self._lock:
            return self._ledger.total_rx_airtime_ms

    @property
    def radio_config(self) -> dict:
        with self._lock:
            return self._calculator.radio_config

    @property
    def spreading_factor(self):
        with self._lock:
            return self._calculator.spreading_factor

    @property
    def bandwidth(self):
        with self._lock:
            return self._calculator.bandwidth

    @property
    def coding_rate(self):
        with self._lock:
            return self._calculator.coding_rate

    @property
    def preamble_length(self):
        with self._lock:
            return self._calculator.preamble_length


def _window_ms(ledger: AirtimeManager) -> float:
    """Airtime still inside the rolling window, without mutating the ledger."""
    cutoff = time.time() - ledger.window_size
    return sum(at for ts, at in ledger.tx_history if ts > cutoff)


def _snapshot(ledger: AirtimeManager) -> Tuple[list, float, float]:
    return (list(ledger.tx_history), ledger.total_airtime_ms, ledger.total_rx_airtime_ms)


@dataclass
class _BudgetState:
    by_radio: dict
    order: list
    by_budget: dict
    budget_of: dict
    scope: str
    profile_backed: bool
    default_radio_id: Optional[str]
    default: _RadioAirtimeBudget


class AirtimeBudgets:
    """Radio-aware airtime calculators backed by configurable duty-cycle ledgers.

    Every radio always calculates packet airtime from its own modulation. Which
    radios debit the same rolling ledger is an independent deployment policy:
    per radio, per channel, node-wide, or an explicit named group. ``channel``
    remains the compatibility default and ``shared_budget`` remains a legacy
    spelling for the node-wide scope.

    A single-radio node still gets exactly one manager built from the top-level
    sections, which is what it had before RF Fabric accounting existed.
    """

    def __init__(self, config: dict):
        self.config = config
        self._lock = threading.RLock()
        # One ledger per resolved budget key, retained across rebuilds and after
        # the last radio leaves it until its rolling window has expired.
        self._ledgers: dict = {}
        # Lifetime totals of budgets dropped once their window emptied, so the
        # node total still counts airtime the node really did transmit.
        self._retired_tx = 0.0
        self._retired_rx = 0.0
        # The air settings each radio is actually being metered with, which is
        # not always what the config says: a non-default radio's modulation only
        # reaches the hardware on a restart.
        self._metered_air: dict = {}
        # None means "adopt whatever the config says", which is what a fresh
        # build does. refresh() narrows it to the radios that were really
        # retuned.
        self._adopt_air: Optional[set] = None
        self._state = self._build_state()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_state(self) -> _BudgetState:
        from .config import (
            build_metering_profiles,
            duty_cycle_budget_groups,
            validate_duty_cycle_config,
        )

        scope = validate_duty_cycle_config(self.config)
        groups = duty_cycle_budget_groups(self.config) if scope == "explicit" else {}

        try:
            profiles = build_metering_profiles(self.config)
        except Exception as exc:  # pragma: no cover - metering must not fail to start
            logger.warning("Could not read radio profiles for duty cycle: %s", exc)
            profiles = []

        if not profiles:
            duty_cycle = self.config.get("duty_cycle", {}) or {}
            ledger = self._ledger_for(
                None,
                self.config.get("radio", {}) or {},
                duty_cycle.get("max_airtime_per_minute", 3600),
            )
            self._prune_retired({None})
            view = _RadioAirtimeBudget(ledger, ledger, self._lock)
            return _BudgetState(
                by_radio={None: view},
                order=[None],
                by_budget={None: ledger},
                budget_of={None: None},
                scope=scope,
                profile_backed=False,
                default_radio_id=None,
                default=view,
            )

        order = []
        budget_of = {}
        effective = {}
        for profile in profiles:
            radio_id = str(profile["radio_id"])
            order.append(radio_id)
            air = self._effective_air(profile)
            effective[radio_id] = air
            budget_of[radio_id] = self._budget_key(scope, radio_id, air, groups)
        self._metered_air = effective

        by_budget = {}
        by_radio = {}
        for profile in profiles:
            radio_id = str(profile["radio_id"])
            key = budget_of[radio_id]
            ledger = by_budget.get(key)
            if ledger is None:
                ledger = self._ledger_for(
                    key,
                    effective[radio_id],
                    self._budget_limit(
                        [rid for rid, rid_key in budget_of.items() if rid_key == key]
                    ),
                )
                by_budget[key] = ledger
            calculator = AirtimeManager(
                self.config,
                radio_config=effective[radio_id],
                max_airtime_per_minute=ledger.max_airtime_per_minute,
            )
            by_radio[radio_id] = _RadioAirtimeBudget(ledger, calculator, self._lock)

        self._prune_retired(set(by_budget))
        default_radio_id = self._default_radio_id(by_radio, order)
        return _BudgetState(
            by_radio=by_radio,
            order=order,
            by_budget=by_budget,
            budget_of=budget_of,
            scope=scope,
            profile_backed=True,
            default_radio_id=default_radio_id,
            default=by_radio[default_radio_id],
        )

    @staticmethod
    def _budget_key(scope: str, radio_id: str, air: dict, groups: dict):
        """Stable, namespaced key for the configured accounting policy."""
        if scope == "radio":
            return ("radio", radio_id)
        if scope == "shared":
            return ("shared",)
        if scope == "explicit":
            return ("explicit", groups[radio_id])
        # Keyed on what the radio is really transmitting with, so a pending
        # restart-required retune does not move it onto a channel it is not on.
        return ("channel", air.get("frequency"), air.get("bandwidth"))

    def _ledger_for(
        self,
        key,
        radio_config: Optional[dict],
        max_airtime_per_minute: Optional[float],
    ) -> AirtimeManager:
        """The ledger for one resolved budget key, kept across rebuilds.

        The ledger persists and is simply re-tuned when a radio on it changes
        modulation or limit. Nothing is copied between keys: copying is what
        let a split hand the same history to both sides and a later merge add
        the copies together, so a node could double its recorded spend by
        retuning a radio back and forth.
        """
        ledger = self._ledgers.get(key)
        if ledger is not None:
            if radio_config is not None:
                ledger.refresh_radio_params(radio_config)
            if max_airtime_per_minute is not None:
                ledger.max_airtime_per_minute = max_airtime_per_minute
            return ledger

        ledger = AirtimeManager(
            self.config,
            radio_config=radio_config,
            max_airtime_per_minute=max_airtime_per_minute,
        )
        seed = self._seed_for(key)
        if seed is not None:
            ledger.tx_history = list(seed[0])
            ledger.total_airtime_ms = seed[1]
            ledger.total_rx_airtime_ms = seed[2]
        self._ledgers[key] = ledger
        return ledger

    def _seed_for(self, key):
        """What a budget key with no ledger of its own should start from.

        A new, identified key starts empty. It represents a separate accounting
        domain selected by the configured policy.

        The exception is a channel nobody can name. ``None`` is the key used
        when the radio profiles cannot be read, and it stands for "some channel,
        we cannot say which" -- so it inherits the busiest window the node is
        keeping, and a named channel inherits from it in turn. Starting either
        at zero mid-window would hand the node a second full allowance.
        """
        if key is None:
            return self._busiest_snapshot(exclude=None)
        unidentified = self._ledgers.get(None)
        if unidentified is not None and _window_ms(unidentified) > 0:
            return _snapshot(unidentified)
        return None

    def _busiest_snapshot(self, exclude=None):
        """The retained budget with the most airtime still inside the window."""
        candidates = [ledger for channel, ledger in self._ledgers.items() if channel is not exclude]
        if not candidates:
            return None
        return _snapshot(max(candidates, key=_window_ms))

    def _prune_retired(self, present_keys: set) -> None:
        """Drop budgets nobody uses once their window has emptied.

        Their lifetime totals move to the node's running totals rather than
        vanishing -- the node did transmit that airtime -- but the ledger itself
        goes, so a node whose radios are retuned repeatedly does not accumulate
        one per frequency ever configured.
        """
        for key in list(self._ledgers):
            if key in present_keys:
                continue
            ledger = self._ledgers[key]
            if _window_ms(ledger) > 0:
                continue
            self._retired_tx += ledger.total_airtime_ms
            self._retired_rx += ledger.total_rx_airtime_ms
            del self._ledgers[key]

    def _absent_totals(self, present) -> Tuple[float, float]:
        """Lifetime totals of retained budgets with no radio currently on them."""
        total_tx = self._retired_tx
        total_rx = self._retired_rx
        for key, ledger in self._ledgers.items():
            if key in present:
                continue
            total_tx += ledger.total_airtime_ms
            total_rx += ledger.total_rx_airtime_ms
        return total_tx, total_rx

    def _air_settings(self, profile: dict) -> dict:
        """A profile in the shape AirtimeManager reads air settings from.

        Laid over the top-level ``radio`` block rather than replacing it, so a
        field this radio could not report inherits the value the node would have
        metered with anyway instead of an unrelated built-in default.
        """
        settings = dict(self.config.get("radio", {}) or {})
        for key, value in (
            ("frequency", profile.get("frequency_hz")),
            ("spreading_factor", profile.get("spreading_factor")),
            ("bandwidth", profile.get("bandwidth_hz")),
            ("coding_rate", profile.get("coding_rate")),
            ("preamble_length", profile.get("preamble_length")),
        ):
            if value is not None:
                settings[key] = value
        return settings

    def _effective_air(self, profile: dict) -> dict:
        """The air settings this radio is metering with, which may not be the
        configured ones.

        Only the default radio can be retuned without a restart. A change to any
        other radio sits in ``radios[]`` marked restart-required, and the
        hardware goes on transmitting with what it has -- so adopting the new
        modulation would meter a 62.5 kHz radio at 500 kHz until someone
        restarted the service. That is exactly what an unrelated duty-cycle save
        used to do, because it rebuilds from the same config.

        The limit is a different matter and is always adopted: it is policy, not
        hardware, and takes effect the moment it is saved.
        """
        settings = self._air_settings(profile)
        radio_id = str(profile["radio_id"])
        if self._adopt_air is not None and radio_id not in self._adopt_air:
            previous = self._metered_air.get(radio_id)
            if previous is not None:
                return dict(previous)
        return settings

    def _budget_limit(self, radio_ids: list) -> Optional[float]:
        """The limit for a ledger several radios may share: the strictest one.

        Taking the first radio's would let a second radio configured for a 1%
        band transmit against a 10% allowance, which is the wrong direction to
        be wrong in about a legal limit.
        """
        budgets = [self._budget_for(radio_id) for radio_id in radio_ids]
        stated = [budget for budget in budgets if budget is not None]
        if not stated:
            return None
        node_wide = self.config.get("duty_cycle", {}).get("max_airtime_per_minute", 3600)
        # A radio that states nothing is held to the node-wide limit, so it
        # counts towards the minimum rather than being ignored.
        if len(stated) != len(budgets):
            stated.append(node_wide)
        return min(stated)

    def _budget_for(self, radio_id: str) -> Optional[float]:
        """Per-radio ``duty_cycle.max_airtime_per_minute``, else the node's.

        Bands differ: 868.0-868.6 MHz allows 1% where 869.4-869.65 allows 10%,
        so a bridge spanning two sub-bands has two different legal limits and
        one number cannot describe both.
        """
        radios = self.config.get("radios")
        if not isinstance(radios, list):
            return None
        for entry in radios:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or entry.get("radio_id") or "") != radio_id:
                continue
            duty_cycle = entry.get("duty_cycle")
            if isinstance(duty_cycle, dict) and "max_airtime_per_minute" in duty_cycle:
                return duty_cycle["max_airtime_per_minute"]
        return None

    # ------------------------------------------------------------------
    # Rebuilding on a live config change
    # ------------------------------------------------------------------

    def refresh(self, adopt_air_for: Optional[set] = None) -> None:
        """Rebuild the budgets from the current config, keeping what was spent.

        A live radio change moves a channel, and the new modulation has to meter
        the next send. What is already on the air stays where it was spent: a
        retune is not a fresh minute.

        ``adopt_air_for`` names the radios whose modulation really reached the
        hardware; every other radio goes on being metered with what it is
        transmitting now. Omitted, every radio's configured settings are
        adopted, which is right at boot and wrong after a live save.

        Nothing is carried between channels, because each channel keeps its own
        ledger and a rebuild only re-tunes it. That is what makes this safe to
        call repeatedly. Carrying copies between channels is what let a split
        hand the same history to both sides and a later merge add those copies
        together -- six clicks in the web UI turned 500 ms of real airtime into
        4000 ms and stopped the node forwarding -- while a channel whose last
        radio left lost its ledger entirely, so a radio retuning onto it started
        from zero on spectrum that had just carried 3000 ms.
        """
        with self._lock:
            previous = self._state
            from .config import validate_duty_cycle_config

            next_scope = validate_duty_cycle_config(self.config)
            if next_scope != previous.scope:
                raise ValueError(
                    "Changing duty_cycle.budget_scope requires a service restart "
                    "so the active rolling window is not regrouped mid-flight"
                )
            self._adopt_air = None if adopt_air_for is None else set(adopt_air_for)
            try:
                rebuilt = self._build_state()
            finally:
                self._adopt_air = None
            self._reuse_views(previous, rebuilt)
            self._state = rebuilt

    @staticmethod
    def _reuse_views(previous: _BudgetState, rebuilt: _BudgetState) -> None:
        """Keep radio handles valid across the atomic state swap."""
        default_id = rebuilt.default_radio_id
        for radio_id, new_view in list(rebuilt.by_radio.items()):
            if radio_id == default_id:
                old_view = previous.default
            else:
                old_view = previous.by_radio.get(radio_id)
                if old_view is previous.default:
                    old_view = None
            if old_view is None or old_view is new_view:
                continue
            old_view._replace(new_view._ledger, new_view._calculator)
            rebuilt.by_radio[radio_id] = old_view
        rebuilt.default = rebuilt.by_radio[default_id]

    def _default_radio_id(self, by_radio: dict, order: list) -> Optional[str]:
        """The radio Fabric transmits on by default.

        Mirrors build_radio_stack's rule -- ``fabric.default_radio`` when set,
        otherwise the first configured radio. Reading ``radios[0]`` instead
        silently meters a node whose default_radio is its second entry against
        the wrong channel entirely.
        """
        fabric = self.config.get("fabric")
        fabric = fabric if isinstance(fabric, dict) else {}
        configured = fabric.get("default_radio") or fabric.get("default_radio_id")
        if configured and str(configured) in by_radio:
            return str(configured)
        return order[0] if order else None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @property
    def default(self) -> _RadioAirtimeBudget:
        """The manager everything that reports one channel's figures reads."""
        with self._lock:
            return self._state.default

    @property
    def default_radio_id(self) -> Optional[str]:
        """The radio the fabric transmits on by default, as metering sees it."""
        with self._lock:
            return self._state.default_radio_id

    @property
    def multi(self) -> bool:
        with self._lock:
            return len(self._state.order) > 1

    @property
    def profile_backed(self) -> bool:
        """Whether these budgets were built from per-radio profiles.

        True for one configured ``radios[]`` entry as much as for five. A node
        with a single entry still has that entry's own modulation and its own
        band's limit, and overwriting them with the top-level block -- which is
        what happens if a caller tests ``multi`` and takes the legacy path --
        undoes the whole point on every radio save from the web UI.
        """
        with self._lock:
            return self._state.profile_backed

    @property
    def budget_scope(self) -> str:
        """Resolved accounting scope, including the legacy shared-budget mapping."""
        with self._lock:
            return self._state.scope

    def radio_ids(self) -> list:
        """Configured radio ids, in order. ``[None]`` when nothing was profiled."""
        with self._lock:
            return list(self._state.order)

    def shares_budget(self, first_radio_id: Optional[str], second_radio_id: Optional[str]) -> bool:
        """Whether two radios debit the same configured ledger."""
        with self._lock:
            first = self.for_radio(first_radio_id)
            second = self.for_radio(second_radio_id)
            return first._ledger is second._ledger

    def for_radio(self, radio_id: Optional[str]) -> _RadioAirtimeBudget:
        """The budget a send on this radio is charged to.

        An unknown id falls back to the default radio rather than going
        unmetered: an unrecognised label is a reason to be careful, not a reason
        to transmit freely.
        """
        with self._lock:
            if radio_id is None:
                return self._state.default
            manager = self._state.by_radio.get(str(radio_id))
            if manager is not None:
                return manager
            logger.debug("No duty-cycle budget for radio %s; metering on the default", radio_id)
            return self._state.default

    def per_radio_stats(self) -> list:
        """``[{radio_id, ...stats}]`` on a multi-radio node, else an empty list.

        Radios sharing a configured ledger report the same figures. Scope and
        budget id make that relationship explicit to operators.
        """
        with self._lock:
            state = self._state
            if len(state.order) <= 1:
                return []
            return [
                {
                    "radio_id": radio_id,
                    "budget_scope": state.scope,
                    "budget_id": self._display_budget_key(state.budget_of[radio_id]),
                    **state.by_radio[radio_id].get_stats(),
                }
                for radio_id in state.order
            ]

    @staticmethod
    def _display_budget_key(key) -> str:
        """Stable human-readable identifier for API statistics."""
        if key is None:
            return "default"
        if key[0] == "channel":
            return f"{key[1]}:{key[2]}"
        if key[0] == "shared":
            return "shared"
        return str(key[1])

    def node_stats(self) -> dict:
        """One set of figures for the whole node, in AirtimeManager's shape.

        For the callers that have always reported a single number: the MeshCore
        wire field ``total_air_time_secs``, companion stats, the RRD and SQLite
        history, and ``/stats``. Reading the default radio's manager instead
        would have each of them describe only one budget of a node that transmits
        through several, which is a quiet regression in figures people have
        been watching for months.

        Totals and the current window are summed across distinct budgets because
        the node really did spend all of it. Utilisation is the highest of them,
        not the ratio of the sums: if one budget is exhausted, averaging it
        against an idle budget would hide that a radio must stop transmitting.

        On a single-radio node every figure is that one manager's, unchanged.
        """
        with self._lock:
            present = self._state.by_budget
            managers = list(present.values())
            # Airtime the node transmitted against budgets no radio uses any
            # more. A retune does not un-transmit it, and these are lifetime
            # counters, so it still belongs in the node's total.
            absent_tx, absent_rx = self._absent_totals(set(present))
            if len(managers) == 1 and not absent_tx and not absent_rx:
                return managers[0].get_stats()

            stats = [manager.get_stats() for manager in managers]
            return {
                # The window and limit describe active budgets. A retired budget
                # cannot be used for a current transmission.
                "current_airtime_ms": sum(s["current_airtime_ms"] for s in stats),
                "max_airtime_ms": sum(s["max_airtime_ms"] for s in stats),
                "utilization_percent": max(s["utilization_percent"] for s in stats),
                "total_airtime_ms": sum(s["total_airtime_ms"] for s in stats) + absent_tx,
                "total_rx_airtime_ms": sum(s["total_rx_airtime_ms"] for s in stats) + absent_rx,
            }
