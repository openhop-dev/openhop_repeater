import concurrent.futures
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class MeshCLI:
    def __init__(
        self,
        config_path: str,
        config: Dict[str, Any],
        config_manager,  # ConfigManager instance for save & live updates
        identity_type: str = "repeater",
        enable_regions: bool = True,
        send_advert_callback: Optional[Callable] = None,
        identity=None,
        storage_handler=None,
    ):

        self.config_path = Path(config_path)
        self.config = config
        self.config_manager = config_manager
        self.identity_type = identity_type
        self.enable_regions = enable_regions
        self.send_advert_callback = send_advert_callback
        self.identity = identity
        self.storage_handler = storage_handler

        # Store event loop reference for thread-safe scheduling
        import asyncio

        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

        # Get repeater config shortcut
        self.repeater_config = config.get("repeater", {})
        self.mesh_config = config.setdefault("mesh", {})

    def _save_config_and_apply(self, sections=None) -> bool:
        """Persist the config, then live-apply the changed sections.

        ``ConfigManager.save_to_file`` returns a bare bool; the tuple form is
        tolerated for older manager doubles. Live update only runs after a
        successful save so a failed write never half-applies a change. Pass
        no sections to stage a change: saved to disk, applied on restart
        (radio parameters, matching firmware's reboot-to-apply).
        """
        result = self.config_manager.save_to_file()
        saved = result[0] if isinstance(result, tuple) else bool(result)
        if not saved:
            return False
        if sections:
            self.config_manager.live_update_daemon(sections)
        return True

    def _get_security_config(self) -> Dict[str, Any]:
        """Return the repeater login security section (the one LoginHelper reads)."""
        security = self.repeater_config.get("security")
        return security if isinstance(security, dict) else {}

    def _get_node_name(self) -> str:
        """Return the configured node name, preferring the newer key when present."""
        return self.repeater_config.get("node_name") or self.repeater_config.get("name", "Unknown")

    def _set_node_name(self, value: str) -> None:
        """Persist node name to both legacy and current config keys for compatibility."""
        self.repeater_config["node_name"] = value
        self.repeater_config["name"] = value

    def _get_local_pubkey_hex(self) -> Optional[str]:
        """Return local node public key (hex) when available."""
        try:
            if self.identity and hasattr(self.identity, "get_public_key"):
                pubkey = self.identity.get_public_key()
                if isinstance(pubkey, (bytes, bytearray)):
                    return bytes(pubkey).hex().lower()
                if isinstance(pubkey, str):
                    normalized = pubkey.strip().lower()
                    if normalized.startswith("0x"):
                        normalized = normalized[2:]
                    if normalized:
                        return normalized
        except Exception as exc:
            logger.debug("Unable to read local identity pubkey: %s", exc)

        key = self.repeater_config.get("identity_key")
        if isinstance(key, (bytes, bytearray)):
            return bytes(key).hex().lower()
        if isinstance(key, str):
            normalized = key.strip().lower()
            if normalized.startswith("0x"):
                normalized = normalized[2:]
            if normalized and all(ch in "0123456789abcdef" for ch in normalized):
                return normalized

        return None

    def _is_local_pubkey(self, pubkey_hex: str) -> bool:
        """Return True when a discovery result pubkey matches the local node."""
        candidate = (pubkey_hex or "").strip().lower()
        if not candidate:
            return False
        if candidate.startswith("0x"):
            candidate = candidate[2:]

        local_pubkey = self._get_local_pubkey_hex()
        if not local_pubkey:
            return False

        # Prefix-only discovery may return fewer bytes than full identity pubkey.
        return local_pubkey.startswith(candidate) or candidate.startswith(local_pubkey)

    def _auto_add_discovery_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Persist discovered neighbors automatically, excluding this node itself."""
        enriched = dict(result)
        pub_key = str(enriched.get("pub_key") or "").strip().lower()
        if not pub_key:
            return enriched

        if self._is_local_pubkey(pub_key):
            enriched["is_self"] = True
            enriched["known_neighbor"] = True
            return enriched

        if not self.storage_handler:
            return enriched

        from repeater.handler_helpers.discovery import persist_discovery_result

        if persist_discovery_result(self.storage_handler, enriched):
            enriched["known_neighbor"] = True
            enriched["auto_added"] = True

        return enriched

    def handle_command(self, sender_pubkey: bytes, command: str, is_admin: bool) -> str:

        # Check admin permission first
        if not is_admin:
            return "Error: Admin permission required"

        logger.debug(f"handle_command received: '{command}' (len={len(command)})")

        # Extract optional sequence prefix (XX|)
        prefix = ""
        if len(command) > 4 and command[2] == "|":
            prefix = command[:3]
            command = command[3:]
            logger.debug(f"Extracted prefix: '{prefix}', remaining command: '{command}'")

        # Strip leading/trailing whitespace
        command = command.strip()
        logger.debug(f"After strip: '{command}'")

        # Route to appropriate handler
        reply = self._route_command(command)

        # Add prefix back to reply if present
        if prefix:
            return prefix + reply
        return reply

    def _route_command(self, command: str) -> str:

        # Help
        if command == "help" or command.startswith("help "):
            return self._cmd_help(command)

        # Named physical-radio commands deliberately precede the legacy
        # get/set parser.  ``radio.local.get`` must select the configured
        # Fabric endpoint, never pass through to the default endpoint.
        elif command.startswith("radio."):
            return self._cmd_named_radio(command)

        # System commands
        elif command == "reboot":
            return self._cmd_reboot()
        elif command == "advert":
            return self._cmd_advert()
        elif command.startswith("clock"):
            return self._cmd_clock(command)
        elif command.startswith("time "):
            return self._cmd_time(command)
        elif command == "http start":
            return self._cmd_http_start()
        elif command == "http stop":
            return self._cmd_http_stop()
        elif command == "start ota":
            return "Error: OTA not supported in Python repeater"
        elif command.startswith("password "):
            return self._cmd_password(command)
        elif command == "clear stats":
            return self._cmd_clear_stats()
        elif command == "ver":
            return self._cmd_version()

        # Get commands
        elif command.startswith("get "):
            return self._cmd_get(command[4:])

        # Set commands
        elif command.startswith("set "):
            return self._cmd_set(command[4:])

        # ACL commands
        elif command.startswith("setperm "):
            return self._cmd_setperm(command)
        elif command == "get acl":
            return "Error: Use 'get acl' via serial console only"

        # Region commands (repeaters only)
        elif command.startswith("region"):
            if self.enable_regions:
                return self._cmd_region(command)
            else:
                return "Error: Region commands not available for room servers"

        # Neighbor commands
        elif command == "neighbors":
            return self._cmd_neighbors()
        elif command.startswith("neighbor.remove "):
            return self._cmd_neighbor_remove(command)
        elif command.startswith("discover.scopes"):
            return self._cmd_discover_scopes(command)
        elif command.startswith("discover.neighbors"):
            return self._cmd_discover_neighbors(command)

        # Temporary radio params
        elif command.startswith("tempradio "):
            return self._cmd_tempradio(command)

        # Sensor commands
        elif command.startswith("sensor "):
            return "Error: Sensor commands not implemented in Python repeater"

        # GPS commands
        elif command.startswith("gps"):
            return "Error: GPS commands not implemented in Python repeater"

        # Logging commands
        elif command.startswith("log "):
            return self._cmd_log(command)

        # Statistics commands
        elif command.startswith("stats-"):
            return "Error: Stats commands not fully implemented yet"

        else:
            return "Unknown command"

    # ==================== Help Command ====================

    def _cmd_help(self, command: str) -> str:
        """Show available commands or detailed help for a specific command."""
        parts = command.split(None, 1)
        if len(parts) == 2:
            return self._help_detail(parts[1])

        lines = [
            "=== openHop CLI Commands ===",
            "",
            "System:",
            "  reboot              Restart the repeater service",
            "  advert              Send self advertisement",
            "  clock               Show current UTC time",
            "  clock sync          Sync clock (no-op, uses system time)",
            "  http start          Start the HTTP server",
            "  http stop           Stop the HTTP server",
            "  ver                 Show version info",
            "  password <pw>       Change admin password",
            "  clear stats         Clear statistics",
            "",
            "Get:",
            "  get name            Node name",
            "  get radio           Radio params (freq,bw,sf,cr)",
            "  get freq            Frequency (MHz)",
            "  get tx              TX power",
            "  get af              Airtime factor",
            "  get repeat          Repeat mode (on/off)",
            "  get lat / get lon   GPS coordinates",
            "  get role            Identity role",
            "  get owner.info      Owner info text",
            "  get guest.password  Guest password",
            "  get allow.read.only Read-only access setting",
            "  get advert.interval Advert interval (minutes)",
            "  get flood.advert.interval  Flood advert interval (hours)",
            "  get flood.max       Max flood hops",
            "  get path.hash.mode  Flood advert path hash mode (0-2)",
            "  get loop.detect     Flood loop detection mode",
            "  get rxdelay         RX delay base",
            "  get txdelay         TX delay factor",
            "  get direct.txdelay  Direct TX delay factor",
            "  get multi.acks      Multi-ack count",
            "  get int.thresh      Interference threshold",
            "  get agc.reset.interval  AGC reset interval",
            "",
            "Set:  (use 'help set' for details)",
            "  set <param> <value>",
            "",
            "Named radios (live only; <id> is radios[].id):",
            "  radio.<id>.get                         Read primary radio params",
            "  radio.<id>.set <MHz> <kHz> <sf> <cr>   Set primary radio params",
            "  radio.<id>.tx [dBm]                    Read/set TX power",
            "  radio.<id>.radio2 [MHz kHz sf cr mode preamble]",
            "  radio.<id>.tempradio2 [MHz kHz sf cr minutes mode preamble]",
            "  radio.<id>.status                      Read available live settings",
            "  IDs are exact (and may contain dots); named commands do not save YAML.",
            "",
            "Other:",
            "  neighbors           List neighbors",
            "  neighbor.remove <key>  Remove neighbor by pubkey",
            "  discover.neighbors  Send zero-hop neighbor discovery",
            "  discover.scopes     Discover neighbor scopes, publish to MQTT",
            "  tempradio <freq> <bw> <sf> <cr> <timeout_mins>",
            "  setperm <pubkey> <perm>  Set ACL permissions",
            "  log start|stop|erase    Logging control",
        ]
        if self.enable_regions:
            lines.append("  region ...          Region commands")
        lines += ["", "Type 'help <command>' for details on a specific command."]
        return "\n".join(lines)

    def _help_detail(self, topic: str) -> str:
        """Return detailed help for a specific command topic."""
        topic = topic.strip()
        details = {
            "set": (
                "Set commands \u2014 set <param> <value>:\n"
                "  set name <name>        Set node name\n"
                "  set radio <f> <bw> <sf> <cr>  Set radio (restart required)\n"
                "  set freq <mhz>         Set frequency (restart required)\n"
                "  set tx <power>         Set TX power\n"
                "  set af <factor>        Airtime factor\n"
                "  set repeat on|off      Enable/disable repeating\n"
                "  set lat <deg>          Latitude\n"
                "  set lon <deg>          Longitude\n"
                "  set guest.password <pw> Guest password\n"
                "  set owner.info <text>  Owner info text\n"
                "  set allow.read.only on|off  Read-only access\n"
                "  set advert.interval <min>   60-240 minutes\n"
                "  set flood.advert.interval <hr>  3-168 hours\n"
                "  set flood.max <hops>   Max flood hops (max 64; 0 = unlimited, unlike firmware)\n"
                "  set path.hash.mode <0-2>  Path hash mode (0=1B,1=2B,2=3B)\n"
                "  set loop.detect <off|minimal|moderate|strict>  Flood loop detection\n"
                "  set rxdelay <val>      RX delay base (0-20)\n"
                "  set txdelay <val>      TX delay factor (0-2)\n"
                "  set direct.txdelay <val>  Direct TX delay (0-2)\n"
                "  set multi.acks <n>     Multi-ack count\n"
                "  set int.thresh <dbm>   Interference threshold\n"
                "  set agc.reset.interval <n>  AGC reset (rounded to x4)"
            ),
            "get": "Get commands \u2014 type 'help' to see all 'get' parameters.",
            "radio": (
                "Named physical-radio commands use radio.<id>.<command>:\n"
                "  radio.<id>.get\n"
                "  radio.<id>.set <freq_mhz> <bw_khz> <sf> <cr>\n"
                "  radio.<id>.tx [dbm]\n"
                "  radio.<id>.radio2 [freq_mhz bw_khz sf cr off|rx|rxtx preamble]\n"
                "  radio.<id>.tempradio2 <freq_mhz> <bw_khz> <sf> <cr> <minutes>\n"
                "                            [off|rx|rxtx] [preamble]\n"
                "  radio.<id>.status\n"
                "  <id> is the exact radios[].id value. These are live modem controls\n"
                "  and do not persist or change the default radio."
            ),
            "reboot": "Restart the repeater service via systemd.",
            "advert": "Trigger a self-advertisement flood packet.",
            "clock": "'clock' shows UTC time. 'clock sync' is a no-op (system time used).",
            "http": "http start|stop - Control the HTTP server.",
            "http start": "Start the HTTP server.",
            "http stop": "Stop the HTTP server.",
            "ver": "Show repeater version and identity type.",
            "password": "password <new_password> \u2014 Change the admin password.",
            "tempradio": (
                "tempradio <freq_mhz> <bw_khz> <sf> <cr> <timeout_mins>\n"
                "  Apply temporary radio parameters that revert after timeout.\n"
                "  freq: 300-2500 MHz, bw: 7-500 kHz, sf: 5-12, cr: 5-8"
            ),
            "neighbors": "List known neighbor nodes from the routing table.",
            "discover.neighbors": "Send a neighbor discovery request.",
            "discover.scopes": (
                "discover.scopes\n"
                "  Refresh the zero-hop neighbor table, query each neighbor for its\n"
                "  region scopes, then publish the table to the MQTT neighbors topic.\n"
                "  Requires a broker configured with neighbors: true."
            ),
            "setperm": "setperm <pubkey_hex> <permission_int> \u2014 Set ACL permissions for a node.",
            "log": "log start|stop|erase \u2014 Control logging.",
        }
        return details.get(topic, f"No detailed help for '{topic}'. Type 'help' for command list.")

    # ==================== System Commands ====================

    def _cmd_reboot(self) -> str:
        """Reboot the repeater process."""
        from repeater.service_utils import restart_service

        logger.warning("Reboot command received via mesh CLI")
        success, message = restart_service()

        if success:
            return f"OK - {message}"
        else:
            return f"Error: {message}"

    def _cmd_advert(self) -> str:
        """Send self advertisement."""
        if not self.send_advert_callback:
            logger.warning("Advert command received but no callback configured")
            return "Error: Advert functionality not configured"

        try:
            import asyncio

            async def delayed_advert():
                """Delay advert to let CLI response send first (matches C++ 1500ms delay)."""
                await asyncio.sleep(1.5)
                await self.send_advert_callback()

            if self._event_loop and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(delayed_advert(), self._event_loop)
            else:
                return "Error: Event loop not available"

            logger.info("Advert scheduled for sending (1.5s delay)")
            return "OK - Advert sent"
        except Exception as e:
            logger.error(f"Failed to schedule advert: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_clock(self, command: str) -> str:
        """Handle clock commands."""
        if command == "clock":
            # Display current time
            import datetime

            dt = datetime.datetime.utcnow()
            return f"{dt.hour:02d}:{dt.minute:02d} - {dt.day}/{dt.month}/{dt.year} UTC"
        elif command == "clock sync":
            # Clock sync happens automatically via sender_timestamp in protocol
            return "OK - clock sync not needed (system time used)"
        else:
            return "Unknown clock command"

    def _cmd_time(self, command: str) -> str:
        """Set time - not supported in Python (use system time)."""
        return "Error: Time setting not supported (system time is used)"

    def _cmd_http_start(self) -> str:
        """Start HTTP server."""
        from repeater.service_utils import start_http_server

        daemon_instance = getattr(self.config_manager, "daemon", None)
        success, message = start_http_server(daemon_instance)
        if success:
            return f"OK - {message}"
        return f"Error: {message}"

    def _cmd_http_stop(self) -> str:
        """Stop HTTP server."""
        from repeater.service_utils import stop_http_server

        daemon_instance = getattr(self.config_manager, "daemon", None)
        success, message = stop_http_server(daemon_instance)
        if success:
            return f"OK - {message}"
        return f"Error: {message}"

    def _cmd_password(self, command: str) -> str:
        """Change admin password."""
        new_password = command[9:].strip()

        if not new_password:
            return "Error: Password cannot be empty"

        # LoginHelper authenticates from repeater.security.admin_password.
        self.repeater_config.setdefault("security", {})["admin_password"] = new_password

        # Save config and live update
        try:
            if not self._save_config_and_apply(["repeater"]):
                logger.error("Failed to save password: config save failed")
                return "Error: Failed to save config"
            return f"password now: {new_password}"
        except Exception as e:
            logger.error(f"Failed to save password: {e}")
            return "Error: Failed to save password"

    def _cmd_clear_stats(self) -> str:
        """Clear statistics."""
        # TODO: Implement stats clearing
        return "Error: Not yet implemented"

    def _cmd_version(self) -> str:
        """Get version information."""
        role = "room_server" if self.identity_type == "room_server" else "repeater"
        version = self.config.get("version", "13")
        return f"openHop_{role} v{version}"

    # ==================== Named Radio Commands ====================

    def _named_radio_endpoints(self) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Return explicitly addressable live radio endpoints.

        A FabricRadio forwards unknown attributes to its default endpoint.  That
        is useful for legacy callers but unsafe for a command that names a
        physical radio, so this method only returns the Fabric registry (or an
        unambiguous single-radio stack).  It deliberately never falls back to
        the default radio for an unknown id.
        """
        daemon = getattr(self.config_manager, "daemon", None)
        if daemon is None:
            return None, "Error: Radio runtime is not available"

        root = getattr(daemon, "radio", None)
        if root is None:
            return None, "Error: Radio runtime is not available"

        fabric = getattr(root, "fabric", None)
        radios = getattr(fabric, "radios", None)
        if hasattr(radios, "items"):
            endpoints = {str(radio_id): radio for radio_id, radio in radios.items()}
            if endpoints:
                return endpoints, None

        meta = getattr(daemon, "radio_stack_meta", {})
        radio_ids = meta.get("radio_ids") if isinstance(meta, dict) else None
        if isinstance(radio_ids, list) and len(radio_ids) == 1:
            return {str(radio_ids[0]): root}, None

        return None, "Error: No named radios are available"

    def _resolve_named_radio(
        self, selector: str
    ) -> tuple[Optional[str], Optional[Any], Optional[str], Optional[str]]:
        """Resolve ``radio.<id>.<command>`` using the longest exact id match."""
        endpoints, error = self._named_radio_endpoints()
        if error:
            return None, None, None, error

        remainder = selector[len("radio.") :]
        matches = [radio_id for radio_id in endpoints if remainder.startswith(f"{radio_id}.")]
        if not matches:
            available = ", ".join(endpoints)
            return (
                None,
                None,
                None,
                f"Error: Unknown radio in '{selector}'. Available: {available}",
            )

        # Radio IDs may include dots (for example, ``local.link``), so the
        # first dot is not a safe separator.  Longest-match preserves the
        # configured id verbatim and makes overlapping ids deterministic.
        radio_id = max(matches, key=len)
        action = remainder[len(radio_id) + 1 :]
        if not action:
            return None, None, None, "Error: Expected radio.<id>.<command>"
        return radio_id, endpoints[radio_id], action.lower(), None

    @staticmethod
    def _parse_named_air_params(
        values: list[str], *, usage: str
    ) -> tuple[Optional[tuple[int, int, int, int]], Optional[str]]:
        """Parse the standard MHz/kHz/SF/CR tuple into modem units."""
        if len(values) < 4:
            return None, f"Error: Expected {usage}"
        try:
            frequency_mhz = float(values[0])
            bandwidth_khz = float(values[1])
            spreading_factor = int(values[2])
            coding_rate = int(values[3])
        except ValueError:
            return None, "Error: Invalid radio parameters"

        if not (
            150.0 <= frequency_mhz <= 2500.0
            and 7.0 <= bandwidth_khz <= 500.0
            and 5 <= spreading_factor <= 12
            and 5 <= coding_rate <= 8
        ):
            return None, "Error: Invalid radio parameters"

        return (
            int(round(frequency_mhz * 1_000_000)),
            int(round(bandwidth_khz * 1_000)),
            spreading_factor,
            coding_rate,
        ), None

    @staticmethod
    def _parse_named_mode(value: str) -> tuple[Optional[int], Optional[str]]:
        modes = {"off": 0, "rx": 1, "rxtx": 2, "0": 0, "1": 1, "2": 2}
        mode = modes.get(value.lower())
        if mode is None:
            return None, "Error: Mode must be off, rx, or rxtx"
        return mode, None

    @staticmethod
    def _format_primary_radio_config(config: dict[str, Any]) -> Optional[str]:
        """Format a runtime config in the CLI's MHz/kHz convention."""
        try:
            frequency = float(config["frequency"]) / 1_000_000
            bandwidth = float(config["bandwidth"]) / 1_000
            spreading_factor = int(config["spreading_factor"])
            coding_rate = int(config["coding_rate"])
        except (KeyError, TypeError, ValueError):
            return None
        return f"{frequency:g},{bandwidth:g},{spreading_factor},{coding_rate}"

    @staticmethod
    def _format_radio2_config(config: dict[str, Any], *, temporary: bool = False) -> Optional[str]:
        primary = MeshCLI._format_primary_radio_config(config)
        if primary is None:
            return None
        try:
            mode = {0: "off", 1: "rx", 2: "rxtx"}.get(int(config.get("mode", 2)), "unknown")
            preamble = int(config.get("preamble_length", 0))
        except (TypeError, ValueError):
            return None
        if temporary:
            if not config.get("active", mode != "off"):
                return "off"
            try:
                remaining = int(config["remaining_minutes"])
            except (KeyError, TypeError, ValueError):
                return None
            return f"{primary},{mode},{remaining},{preamble}"
        return f"{primary},{mode},{preamble}"

    def _read_named_primary_config(
        self, radio_id: str, radio: Any
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        getter = getattr(radio, "get_radio_config", None)
        if callable(getter):
            try:
                config = getter()
            except Exception as exc:
                logger.warning("Unable to read primary config from radio %s: %s", radio_id, exc)
                return None, f"Error: Failed to read radio '{radio_id}'"
            if isinstance(config, dict):
                return config, None
            return None, f"Error: Failed to read radio '{radio_id}'"

        fields = ("frequency", "bandwidth", "spreading_factor", "coding_rate")
        config = {field: getattr(radio, field, None) for field in fields}
        if all(value is not None for value in config.values()):
            return config, None
        return None, f"Error: Radio '{radio_id}' does not expose primary radio settings"

    def _read_named_tx_power(
        self, radio_id: str, radio: Any
    ) -> tuple[Optional[int], Optional[str]]:
        getter = getattr(radio, "get_tx_power", None)
        if callable(getter):
            try:
                power = getter()
            except Exception as exc:
                logger.warning("Unable to read TX power from radio %s: %s", radio_id, exc)
                return None, f"Error: Failed to read TX power from radio '{radio_id}'"
            if power is not None:
                try:
                    return int(power), None
                except (TypeError, ValueError):
                    pass

        power = getattr(radio, "tx_power", None)
        try:
            return int(power), None
        except (TypeError, ValueError):
            return None, f"Error: Radio '{radio_id}' does not expose TX power"

    def _set_named_primary_config(self, radio_id: str, radio: Any, values: list[str]) -> str:
        parsed, error = self._parse_named_air_params(values, usage="freq_mhz bw_khz sf cr")
        if error:
            return error
        frequency, bandwidth, spreading_factor, coding_rate = parsed

        configure = getattr(radio, "configure_radio", None)
        if callable(configure):
            try:
                success = configure(frequency, bandwidth, spreading_factor, coding_rate)
            except Exception as exc:
                logger.warning("Unable to configure radio %s: %s", radio_id, exc)
                return f"Error: Failed to configure radio '{radio_id}'"
            return "OK" if success else f"Error: Failed to configure radio '{radio_id}'"

        setters = (
            ("set_frequency", frequency),
            ("set_bandwidth", bandwidth),
            ("set_spreading_factor", spreading_factor),
            ("set_coding_rate", coding_rate),
        )
        if not all(callable(getattr(radio, name, None)) for name, _ in setters):
            return f"Error: Radio '{radio_id}' does not support live primary configuration"
        for name, value in setters:
            try:
                if not getattr(radio, name)(value):
                    return f"Error: Failed to configure radio '{radio_id}'"
            except Exception as exc:
                logger.warning("Unable to configure radio %s: %s", radio_id, exc)
                return f"Error: Failed to configure radio '{radio_id}'"
        return "OK"

    def _set_named_radio2_config(
        self, radio_id: str, radio: Any, values: list[str], *, temporary: bool = False
    ) -> str:
        action_name = "tempradio2" if temporary else "radio2"
        method_name = "set_temporary_radio2_config" if temporary else "set_radio2_config"
        setter = getattr(radio, method_name, None)
        if not callable(setter):
            return f"Error: Radio '{radio_id}' does not support {action_name}"

        if temporary and len(values) == 1 and values[0].lower() == "off":
            try:
                success = setter(0, 0, 0, 0, 0, mode=0, preamble_length=0)
            except Exception as exc:
                logger.warning("Unable to cancel tempradio2 on radio %s: %s", radio_id, exc)
                return f"Error: Failed to configure {action_name} on radio '{radio_id}'"
            return (
                "OK"
                if success
                else f"Error: Failed to configure {action_name} on radio '{radio_id}'"
            )

        suffix = "minutes [mode] [preamble]" if temporary else "mode [preamble]"
        parsed, error = self._parse_named_air_params(
            values, usage=f"freq_mhz bw_khz sf cr {suffix}"
        )
        if error:
            return error
        frequency, bandwidth, spreading_factor, coding_rate = parsed

        if temporary:
            if len(values) not in (5, 6, 7):
                return "Error: Expected freq_mhz bw_khz sf cr minutes [mode] [preamble]"
            if len(values) == 5:
                mode = 2
                duration_value = values[4]
                preamble_value = "0"
            else:
                # Keep the common tempradio tuple unchanged: its timeout is
                # immediately after freq/bw/sf/cr.  Also accept the earlier
                # mode-before-minutes shape so existing KISS-oriented callers
                # do not need a flag day.
                if values[4].lower() in {"off", "rx", "rxtx"}:
                    mode, error = self._parse_named_mode(values[4])
                    duration_value = values[5]
                else:
                    duration_value = values[4]
                    mode, error = self._parse_named_mode(values[5])
                if error:
                    return error
                preamble_value = values[6] if len(values) == 7 else "0"
            try:
                duration = int(duration_value)
                preamble = int(preamble_value)
            except ValueError:
                return "Error: Invalid tempradio2 duration or preamble"
            if not (1 <= duration <= 10_080) or not (0 <= preamble <= 65_535):
                return "Error: Invalid tempradio2 duration or preamble"
            try:
                success = setter(
                    frequency,
                    bandwidth,
                    spreading_factor,
                    coding_rate,
                    duration,
                    mode=mode,
                    preamble_length=preamble,
                )
            except Exception as exc:
                logger.warning("Unable to configure tempradio2 on radio %s: %s", radio_id, exc)
                return f"Error: Failed to configure {action_name} on radio '{radio_id}'"
        else:
            if len(values) not in (4, 5, 6):
                return "Error: Expected freq_mhz bw_khz sf cr [mode] [preamble]"
            mode = 2
            if len(values) >= 5:
                mode, error = self._parse_named_mode(values[4])
                if error:
                    return error
            try:
                preamble = int(values[5]) if len(values) == 6 else 0
            except ValueError:
                return "Error: Invalid radio2 preamble"
            if not 0 <= preamble <= 65_535:
                return "Error: Invalid radio2 preamble"
            try:
                success = setter(
                    frequency,
                    bandwidth,
                    spreading_factor,
                    coding_rate,
                    mode=mode,
                    preamble_length=preamble,
                )
            except Exception as exc:
                logger.warning("Unable to configure radio2 on radio %s: %s", radio_id, exc)
                return f"Error: Failed to configure {action_name} on radio '{radio_id}'"

        return (
            "OK"
            if success
            else f"Error: Failed to configure {action_name} on radio '{radio_id}'"
        )

    def _get_named_radio2_config(
        self, radio_id: str, radio: Any, *, temporary: bool = False
    ) -> str:
        action_name = "tempradio2" if temporary else "radio2"
        method_name = "get_temporary_radio2_config" if temporary else "get_radio2_config"
        getter = getattr(radio, method_name, None)
        if not callable(getter):
            return f"Error: Radio '{radio_id}' does not support {action_name}"
        try:
            config = getter()
        except Exception as exc:
            logger.warning("Unable to read %s from radio %s: %s", action_name, radio_id, exc)
            return f"Error: Failed to read {action_name} from radio '{radio_id}'"
        if not isinstance(config, dict):
            return f"Error: Failed to read {action_name} from radio '{radio_id}'"
        formatted = self._format_radio2_config(config, temporary=temporary)
        if formatted is None:
            return f"Error: Invalid {action_name} response from radio '{radio_id}'"
        return f"> {formatted}"

    def _cmd_named_radio_status(self, radio_id: str, radio: Any) -> str:
        """Show the available live settings without treating absent profiles as errors."""
        primary, error = self._read_named_primary_config(radio_id, radio)
        if error:
            return error
        primary_text = self._format_primary_radio_config(primary)
        if primary_text is None:
            return f"Error: Invalid primary response from radio '{radio_id}'"

        lines = [f"radio.{radio_id}", f"primary: {primary_text}"]
        tx_power, _ = self._read_named_tx_power(radio_id, radio)
        if tx_power is not None:
            lines.append(f"tx: {tx_power}")

        for action_name, method_name, temporary in (
            ("radio2", "get_radio2_config", False),
            ("tempradio2", "get_temporary_radio2_config", True),
        ):
            getter = getattr(radio, method_name, None)
            if not callable(getter):
                continue
            try:
                config = getter()
            except Exception as exc:
                logger.debug("Unable to read %s from radio %s: %s", action_name, radio_id, exc)
                continue
            if isinstance(config, dict):
                text = self._format_radio2_config(config, temporary=temporary)
                if text is not None:
                    lines.append(f"{action_name}: {text}")
        return "\n".join(lines)

    def _cmd_named_radio(self, command: str) -> str:
        """Handle direct ``radio.<configured-id>.<command>`` controls."""
        selector, _, argument_text = command.partition(" ")
        radio_id, radio, action, error = self._resolve_named_radio(selector)
        if error:
            return error
        values = argument_text.split()

        if action == "get":
            if values:
                return "Error: radio.<id>.get does not take arguments"
            config, error = self._read_named_primary_config(radio_id, radio)
            if error:
                return error
            formatted = self._format_primary_radio_config(config)
            if formatted is None:
                return f"Error: Invalid primary response from radio '{radio_id}'"
            return f"> {formatted}"

        if action == "set":
            return self._set_named_primary_config(radio_id, radio, values)

        if action == "tx":
            if not values:
                power, error = self._read_named_tx_power(radio_id, radio)
                return error if error else f"> {power}"
            if len(values) != 1:
                return "Error: Expected one TX power value"
            try:
                power = int(values[0])
            except ValueError:
                return "Error: Invalid TX power"
            setter = getattr(radio, "set_tx_power", None)
            if not callable(setter):
                return f"Error: Radio '{radio_id}' does not support TX power control"
            try:
                success = setter(power)
            except Exception as exc:
                logger.warning("Unable to set TX power on radio %s: %s", radio_id, exc)
                return f"Error: Failed to set TX power on radio '{radio_id}'"
            return "OK" if success else f"Error: Failed to set TX power on radio '{radio_id}'"

        if action == "radio2":
            if not values:
                return self._get_named_radio2_config(radio_id, radio)
            return self._set_named_radio2_config(radio_id, radio, values)

        if action == "tempradio2":
            if not values:
                return self._get_named_radio2_config(radio_id, radio, temporary=True)
            return self._set_named_radio2_config(radio_id, radio, values, temporary=True)

        if action == "status":
            if values:
                return "Error: radio.<id>.status does not take arguments"
            return self._cmd_named_radio_status(radio_id, radio)

        return (
            f"Error: Unknown radio command '{action}'. "
            "Use get, set, tx, radio2, tempradio2, or status"
        )

    # ==================== Get Commands ====================

    def _cmd_get(self, param: str) -> str:
        """Handle get commands."""
        param = param.strip()
        logger.debug(f"_cmd_get called with param: '{param}' (len={len(param)})")

        if param == "af":
            af = self.repeater_config.get("airtime_factor", 1.0)
            return f"> {af}"

        elif param == "name":
            return f"> {self._get_node_name()}"

        elif param == "repeat":
            mode = self.repeater_config.get("mode", "forward")
            return f"> {'on' if mode == 'forward' else 'off'}"

        elif param == "lat":
            lat = self.repeater_config.get("latitude", 0.0)
            return f"> {lat}"

        elif param == "lon":
            lon = self.repeater_config.get("longitude", 0.0)
            return f"> {lon}"

        elif param == "radio":
            radio = self.config.get("radio", {})
            freq_hz = radio.get("frequency", 915000000)
            bw_hz = radio.get("bandwidth", 125000)
            sf = radio.get("spreading_factor", 7)
            cr = radio.get("coding_rate", 5)
            # Convert Hz to MHz for freq, Hz to kHz for bandwidth (match C++ ftoa output)
            freq_mhz = freq_hz / 1_000_000.0
            bw_khz = bw_hz / 1_000.0
            return f"> {freq_mhz},{bw_khz},{sf},{cr}"

        elif param == "freq":
            freq_hz = self.config.get("radio", {}).get("frequency", 915000000)
            freq_mhz = freq_hz / 1_000_000.0
            return f"> {freq_mhz}"

        elif param == "tx":
            power = self.config.get("radio", {}).get("tx_power", 20)
            return f"> {power}"

        elif param == "public.key":
            if not self.identity:
                return "Error: Identity not available"
            try:
                pubkey = self.identity.get_public_key()
                pubkey_hex = pubkey.hex()
                return f"> {pubkey_hex}"
            except Exception as e:
                logger.error(f"Failed to get public key: {e}")
                return f"Error: {e}"

        elif param == "role":
            role = "room_server" if self.identity_type == "room_server" else "repeater"
            return f"> {role}"

        elif param == "guest.password":
            guest_pw = self._get_security_config().get("guest_password") or ""
            return f"> {guest_pw}"

        elif param == "owner.info":
            owner_info = self.repeater_config.get("owner_info", "")
            return f"> {owner_info}"

        elif param == "allow.read.only":
            allow = self._get_security_config().get("allow_read_only", False)
            return f"> {'on' if allow else 'off'}"

        elif param == "advert.interval":
            interval = self.repeater_config.get("advert_interval_minutes", 120)
            return f"> {interval}"

        elif param == "flood.advert.interval":
            # The engine's flood-advert timer consumes send_advert_interval_hours.
            interval = self.repeater_config.get("send_advert_interval_hours", 10)
            return f"> {interval}"

        elif param == "flood.max":
            max_flood = self.repeater_config.get("max_flood_hops", 64)
            return f"> {max_flood}"

        elif param == "path.hash.mode":
            path_hash_mode = self.mesh_config.get("path_hash_mode", 0)
            return f"> {path_hash_mode}"

        elif param == "loop.detect":
            loop_detect = self.mesh_config.get("loop_detect", "off")
            return f"> {loop_detect}"

        elif param == "rxdelay":
            # The flood RX delay base lives in the delays section (the value
            # the dispatcher and web API use), not the repeater section.
            delay = self.config.get("delays", {}).get("rx_delay_base", 0.0)
            return f"> {delay}"

        elif param == "txdelay":
            # The TX delay factor lives in the delays section (the value the
            # engine consumes), not the repeater section.
            delay = self.config.get("delays", {}).get("tx_delay_factor", 1.0)
            return f"> {delay}"

        elif param == "direct.txdelay":
            # The direct TX delay factor lives in the delays section (the
            # value the engine consumes), not the repeater section.
            delay = self.config.get("delays", {}).get("direct_tx_delay_factor", 0.5)
            return f"> {delay}"

        elif param == "multi.acks":
            acks = self.repeater_config.get("multi_acks", 0)
            return f"> {acks}"

        elif param == "int.thresh":
            thresh = self.repeater_config.get("interference_threshold", -120)
            return f"> {thresh}"

        elif param == "agc.reset.interval":
            interval = self.repeater_config.get("agc_reset_interval", 0)
            return f"> {interval}"

        else:
            return f"??: {param}"

    # ==================== Set Commands ====================

    def _cmd_set(self, param: str) -> str:
        """Handle set commands."""
        parts = param.split(None, 1)
        if len(parts) < 2:
            return "Error: Missing value"

        key, value = parts[0], parts[1]

        try:
            if key == "af":
                self.repeater_config["airtime_factor"] = float(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "name":
                self._set_node_name(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "repeat":
                self.repeater_config["mode"] = "forward" if value.lower() == "on" else "monitor"
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return f"OK - repeat is now {'ON' if self.repeater_config['mode'] == 'forward' else 'OFF'}"

            elif key == "lat":
                self.repeater_config["latitude"] = float(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "lon":
                self.repeater_config["longitude"] = float(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "radio":
                # Format: freq(MHz) bw(kHz) sf cr — the config stores Hz.
                radio_parts = value.split()
                if len(radio_parts) != 4:
                    return "Error: Expected freq bw sf cr"

                freq_mhz = float(radio_parts[0])
                bw_khz = float(radio_parts[1])
                sf = int(radio_parts[2])
                cr = int(radio_parts[3])
                if not (
                    150.0 <= freq_mhz <= 2500.0
                    and 7.0 <= bw_khz <= 500.0
                    and 5 <= sf <= 12
                    and 5 <= cr <= 8
                ):
                    return "Error, invalid radio params"

                radio_config = self.config.setdefault("radio", {})
                radio_config["frequency"] = int(round(freq_mhz * 1_000_000))
                radio_config["bandwidth"] = int(round(bw_khz * 1_000))
                radio_config["spreading_factor"] = sf
                radio_config["coding_rate"] = cr
                if not self._save_config_and_apply():
                    return "Error: Failed to save config"
                return "OK - restart repeater to apply"

            elif key == "freq":
                # CLI input is MHz (firmware parity); the config stores Hz.
                freq_mhz = float(value)
                self.config.setdefault("radio", {})["frequency"] = int(round(freq_mhz * 1_000_000))
                if not self._save_config_and_apply():
                    return "Error: Failed to save config"
                return "OK - restart repeater to apply"

            elif key == "tx":
                self.config.setdefault("radio", {})["tx_power"] = int(value)
                if not self._save_config_and_apply():
                    return "Error: Failed to save config"
                return "OK - restart repeater to apply"

            elif key == "guest.password":
                # LoginHelper authenticates from repeater.security.guest_password.
                self.repeater_config.setdefault("security", {})["guest_password"] = value
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "owner.info":
                self.repeater_config["owner_info"] = value.replace("|", "\n")
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "allow.read.only":
                # LoginHelper reads repeater.security.allow_read_only.
                self.repeater_config.setdefault("security", {})["allow_read_only"] = (
                    value.lower() == "on"
                )
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "advert.interval":
                mins = int(value)
                if mins > 0 and (mins < 60 or mins > 240):
                    return "Error: interval range is 60-240 minutes"
                self.repeater_config["advert_interval_minutes"] = mins
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "flood.advert.interval":
                hours = int(value)
                if (hours > 0 and hours < 3) or hours > 168:
                    return "Error: interval range is 3-168 hours"
                # The engine's flood-advert timer consumes send_advert_interval_hours.
                self.repeater_config["send_advert_interval_hours"] = hours
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "flood.max":
                max_val = int(value)
                if max_val > 64:
                    return "Error: max 64"
                self.repeater_config["max_flood_hops"] = max_val
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "path.hash.mode":
                mode = int(value)
                if mode not in (0, 1, 2):
                    return "Error: path.hash.mode must be 0, 1, or 2"
                self.mesh_config["path_hash_mode"] = mode
                if not self._save_config_and_apply(["mesh"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "loop.detect":
                mode = str(value).strip().lower()
                if mode not in ("off", "minimal", "moderate", "strict"):
                    return "Error: loop.detect must be off, minimal, moderate, or strict"
                self.mesh_config["loop_detect"] = mode
                if not self._save_config_and_apply(["mesh"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "rxdelay":
                delay = float(value)
                if delay < 0 or delay > 20.0:
                    return "Error, must be 0-20"
                self.config.setdefault("delays", {})["rx_delay_base"] = delay
                if not self._save_config_and_apply(["repeater", "delays"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "txdelay":
                delay = float(value)
                if delay < 0 or delay > 2.0:
                    return "Error, must be 0-2"
                self.config.setdefault("delays", {})["tx_delay_factor"] = delay
                if not self._save_config_and_apply(["repeater", "delays"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "direct.txdelay":
                delay = float(value)
                if delay < 0 or delay > 2.0:
                    return "Error, must be 0-2"
                self.config.setdefault("delays", {})["direct_tx_delay_factor"] = delay
                if not self._save_config_and_apply(["repeater", "delays"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "multi.acks":
                self.repeater_config["multi_acks"] = int(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "int.thresh":
                self.repeater_config["interference_threshold"] = int(value)
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return "OK"

            elif key == "agc.reset.interval":
                interval = int(value)
                # Round to nearest multiple of 4
                rounded = (interval // 4) * 4
                self.repeater_config["agc_reset_interval"] = rounded
                if not self._save_config_and_apply(["repeater"]):
                    return "Error: Failed to save config"
                return f"OK - interval rounded to {rounded}"

            else:
                return f"unknown config: {key}"

        except ValueError as e:
            return f"Error: invalid value - {e}"
        except Exception as e:
            logger.error(f"Set command error: {e}")
            return f"Error: {e}"

    # ==================== ACL Commands ====================

    def _cmd_setperm(self, command: str) -> str:
        """Set permissions for a public key."""
        # Format: setperm {pubkey-hex} {permissions-int}
        parts = command[8:].split()
        if len(parts) < 2:
            return "Err - bad params"

        pubkey_hex = parts[0]
        try:
            permissions = int(parts[1])
        except ValueError:
            return "Err - invalid permissions"

        # TODO: Apply permissions via ACL
        logger.info(f"setperm command: {pubkey_hex} -> {permissions}")
        return "Error: Not yet implemented - use config file"

    # ==================== Region Commands ====================

    def _cmd_region(self, command: str) -> str:
        """Handle region commands with MeshCore-compatible response shapes."""
        parts = command.split()

        if len(parts) == 1:
            return self._region_export_tree()

        subcommand = parts[1]

        if subcommand == "load":
            return "Err - region load not supported"
        if subcommand == "def":
            return "Err - region def not supported"
        if subcommand == "save":
            return self._region_save()
        if subcommand == "allowf" and len(parts) >= 3:
            return self._region_set_flood(parts[2], allow=True)
        if subcommand == "denyf" and len(parts) >= 3:
            return self._region_set_flood(parts[2], allow=False)
        if subcommand == "get" and len(parts) >= 3:
            return self._region_get(parts[2])
        if subcommand == "home":
            if len(parts) >= 3:
                return self._region_home_set(parts[2])
            return self._region_home_get()
        if subcommand == "default":
            if len(parts) >= 3:
                return self._region_default_set(parts[2])
            return self._region_default_get()
        if subcommand == "put" and len(parts) >= 3:
            parent = parts[3] if len(parts) >= 4 else "*"
            return self._region_put(parts[2], parent)
        if subcommand == "remove" and len(parts) >= 3:
            return self._region_remove(parts[2])
        if subcommand == "list" and len(parts) >= 3:
            return self._region_list(parts[2])

        return "Err - ??"

    def _region_storage_available(self) -> bool:
        return bool(
            self.storage_handler
            and hasattr(self.storage_handler, "get_transport_keys")
            and callable(getattr(self.storage_handler, "get_transport_keys"))
        )

    def _region_load_records(self) -> list[dict]:
        if not self._region_storage_available():
            return []
        records = self.storage_handler.get_transport_keys()
        return records if isinstance(records, list) else []

    @staticmethod
    def _region_display_name(raw_name: str) -> str:
        name = str(raw_name or "").strip()
        if name.startswith("#"):
            return name[1:]
        return name

    def _region_find_prefix(self, query: str) -> Optional[dict]:
        q = str(query or "").strip()
        if not q:
            return None
        if q == "*":
            return {
                "id": 0,
                "name": "*",
                "display_name": "*",
                "flood_policy": "allow" if self._region_unscoped_allow() else "deny",
                "parent_id": None,
            }

        q_lower = q.lower()
        for rec in self._region_load_records():
            display = self._region_display_name(rec.get("name", ""))
            if display.lower().startswith(q_lower):
                return {**rec, "display_name": display}
        return None

    def _region_find_exact(self, query: str) -> Optional[dict]:
        q = str(query or "").strip()
        if not q:
            return None
        if q == "*":
            return {
                "id": 0,
                "name": "*",
                "display_name": "*",
                "flood_policy": "allow" if self._region_unscoped_allow() else "deny",
                "parent_id": None,
            }

        q_lower = q.lower()
        for rec in self._region_load_records():
            display = self._region_display_name(rec.get("name", ""))
            if display.lower() == q_lower:
                return {**rec, "display_name": display}
        return None

    def _region_unscoped_allow(self) -> bool:
        return bool(
            self.mesh_config.get(
                "unscoped_flood_allow",
                self.mesh_config.get("global_flood_allow", True),
            )
        )

    def _region_set_unscoped_allow(self, allow: bool) -> bool:
        self.mesh_config["unscoped_flood_allow"] = bool(allow)
        self.mesh_config["global_flood_allow"] = bool(allow)
        save_result = self.config_manager.save_to_file()
        saved = save_result[0] if isinstance(save_result, tuple) else bool(save_result)
        self.config_manager.live_update_daemon(["mesh"])
        return bool(saved)

    def _region_get_default_name(self) -> Optional[str]:
        default_name = self.mesh_config.get("default_region")
        text = str(default_name).strip() if default_name is not None else ""
        return text or None

    def _region_set_default_name(self, value: Optional[str]) -> bool:
        self.mesh_config["default_region"] = value
        save_result = self.config_manager.save_to_file()
        saved = save_result[0] if isinstance(save_result, tuple) else bool(save_result)
        self.config_manager.live_update_daemon(["mesh"])
        return bool(saved)

    def _region_export_tree(self) -> str:
        records = self._region_load_records()
        children_by_parent: Dict[int, list[dict]] = {}
        for rec in records:
            parent_id = rec.get("parent_id")
            parent_key = int(parent_id) if isinstance(parent_id, int) and parent_id > 0 else 0
            children_by_parent.setdefault(parent_key, []).append(rec)

        for parent_list in children_by_parent.values():
            parent_list.sort(key=lambda r: str(r.get("name", "")).lower())

        home_name = str(self.repeater_config.get("region_home") or "").strip().lower()

        lines: list[str] = []

        def append_line(indent: int, display_name: str, flood_policy: str):
            home_mark = "^" if home_name and display_name.lower() == home_name else ""
            flood_mark = " F" if flood_policy == "allow" else ""
            lines.append(f"{' ' * indent}{display_name}{home_mark}{flood_mark}")

        append_line(0, "*", "allow" if self._region_unscoped_allow() else "deny")

        def walk(parent_id: int, indent: int):
            for rec in children_by_parent.get(parent_id, []):
                display_name = self._region_display_name(rec.get("name", ""))
                append_line(indent, display_name, str(rec.get("flood_policy", "deny")))
                walk(int(rec.get("id", 0)), indent + 1)

        walk(0, 1)
        return "\n".join(lines)

    def _region_save(self) -> str:
        save_result = self.config_manager.save_to_file()
        saved = save_result[0] if isinstance(save_result, tuple) else bool(save_result)
        return "OK" if saved else "Err - save failed"

    def _region_set_flood(self, name_prefix: str, allow: bool) -> str:
        region = self._region_find_prefix(name_prefix)
        if not region:
            return "Err - unknown region"

        if region.get("id") == 0:
            return "OK" if self._region_set_unscoped_allow(allow) else "Err - save failed"

        update_fn = getattr(self.storage_handler, "update_transport_key", None)
        if not callable(update_fn):
            return "Error: Region commands not supported by storage backend"

        ok = update_fn(int(region["id"]), flood_policy="allow" if allow else "deny")
        return "OK" if ok else "Err - unknown region"

    def _region_get(self, name_prefix: str) -> str:
        region = self._region_find_prefix(name_prefix)
        if not region:
            return "Err - unknown region"

        display_name = str(
            region.get("display_name") or self._region_display_name(region.get("name", ""))
        )
        flood_suffix = "F" if region.get("flood_policy") == "allow" else ""

        parent_name = None
        parent_id = region.get("parent_id")
        if isinstance(parent_id, int) and parent_id > 0:
            for rec in self._region_load_records():
                if int(rec.get("id", -1)) == parent_id:
                    parent_name = self._region_display_name(rec.get("name", ""))
                    break

        if parent_name:
            return f" {display_name} ({parent_name}) {flood_suffix}".rstrip()
        return f" {display_name} {flood_suffix}".rstrip()

    def _region_home_get(self) -> str:
        home = str(self.repeater_config.get("region_home") or "").strip()
        return f" home is {home or '*'}"

    def _region_home_set(self, name_prefix: str) -> str:
        region = self._region_find_prefix(name_prefix)
        if not region:
            return "Err - unknown region"

        display_name = str(region.get("display_name") or "*")
        self.repeater_config["region_home"] = display_name
        save_result = self.config_manager.save_to_file()
        saved = save_result[0] if isinstance(save_result, tuple) else bool(save_result)
        return f" home is now {display_name}" if saved else "Err - save failed"

    def _region_default_get(self) -> str:
        default_region = self._region_get_default_name()
        if default_region is None:
            return " default scope is <null>"
        return f" default scope is {default_region}"

    def _region_default_set(self, value: str) -> str:
        text = str(value or "").strip()
        if text == "<null>":
            saved = self._region_set_default_name(None)
            return " default scope is now <null>" if saved else "Err - save failed"

        region = self._region_find_prefix(text)
        if region:
            display_name = str(region.get("display_name") or text)
            if region.get("id") not in (None, 0):
                update_fn = getattr(self.storage_handler, "update_transport_key", None)
                if callable(update_fn):
                    update_fn(int(region["id"]), flood_policy="allow")
            saved = self._region_set_default_name(display_name)
            return f" default scope is now {display_name}" if saved else "Err - save failed"

        put_result = self._region_put(text, "*")
        if not put_result.startswith("OK"):
            return "Err - region table full"

        saved = self._region_set_default_name(text)
        return f" default scope is now {text}" if saved else "Err - save failed"

    def _region_put(self, name: str, parent_name: str) -> str:
        region_name = str(name or "").strip()
        if not region_name:
            return "Err - unable to put"

        parent = self._region_find_prefix(parent_name)
        if not parent:
            return "Err - unknown parent"

        parent_id = int(parent.get("id", 0))
        parent_storage_id = None if parent_id == 0 else parent_id

        existing = self._region_find_exact(region_name)
        if existing and existing.get("id") != 0:
            update_fn = getattr(self.storage_handler, "update_transport_key", None)
            if not callable(update_fn):
                return "Err - unable to put"
            ok = update_fn(
                int(existing["id"]),
                flood_policy="allow",
                parent_id=parent_storage_id,
            )
            return "OK - (flood allowed)" if ok else "Err - unable to put"

        create_fn = getattr(self.storage_handler, "create_transport_key", None)
        if not callable(create_fn):
            return "Err - unable to put"

        key_id = create_fn(
            region_name,
            "allow",
            None,
            parent_storage_id,
            None,
        )
        return "OK - (flood allowed)" if key_id else "Err - unable to put"

    def _region_remove(self, name: str) -> str:
        region = self._region_find_exact(name)
        if not region or region.get("id") == 0:
            return "Err - not found"

        region_id = int(region["id"])
        for rec in self._region_load_records():
            if int(rec.get("parent_id") or 0) == region_id:
                return "Err - not empty"

        delete_fn = getattr(self.storage_handler, "delete_transport_key", None)
        if not callable(delete_fn):
            return "Err - not found"

        ok = delete_fn(region_id)
        if not ok:
            return "Err - not found"

        removed_name = str(region.get("display_name") or "")
        if (
            str(self.repeater_config.get("region_home") or "").strip().lower()
            == removed_name.lower()
        ):
            self.repeater_config["region_home"] = ""
        default_name = self._region_get_default_name()
        if str(default_name or "").strip().lower() == removed_name.lower():
            self.mesh_config["default_region"] = None
        return "OK"

    def _region_list(self, filter_name: str) -> str:
        mode = str(filter_name or "").strip().lower()
        if mode not in ("allowed", "denied"):
            return "Err - use 'allowed' or 'denied'"

        names: list[str] = []
        unscoped_allowed = self._region_unscoped_allow()
        if (mode == "allowed" and unscoped_allowed) or (mode == "denied" and not unscoped_allowed):
            names.append("*")

        records = sorted(
            self._region_load_records(),
            key=lambda r: self._region_display_name(r.get("name", "")).lower(),
        )
        for rec in records:
            flood_policy = str(rec.get("flood_policy", "deny")).lower()
            allowed = flood_policy == "allow"
            if (mode == "allowed" and allowed) or (mode == "denied" and not allowed):
                names.append(self._region_display_name(rec.get("name", "")))

        return ",".join(names) if names else "-none-"

    # ==================== Neighbor Commands ====================

    def _cmd_neighbors(self) -> str:
        """List neighbors."""
        if not self.storage_handler:
            return "Error: Storage not available"

        try:
            neighbors = self.storage_handler.get_neighbors()

            if not neighbors:
                return "No neighbors discovered yet"

            # Match MeshCore behavior: show only zero-hop repeaters.
            filtered_neighbors = {
                pubkey: info
                for pubkey, info in neighbors.items()
                if info.get("is_repeater", False) and info.get("zero_hop", False)
            }

            if not filtered_neighbors:
                return "No zero hop repeaters discovered yet"

            # Format output similar to C++ version
            # Format: "<pubkey_prefix> heard Xs ago"
            import time

            current_time = int(time.time())

            lines = []
            for pubkey, info in filtered_neighbors.items():
                last_seen = info.get("last_seen", 0)
                seconds_ago = int(current_time - last_seen)

                # Get first 4 bytes of pubkey as hex (match C++ format)
                pubkey_short = pubkey[:8] if len(pubkey) >= 8 else pubkey
                snr = info.get("snr", 0) or 0

                # Format: <4byte_hex>:<seconds_ago>:<snr> (matches C++ format)
                lines.append(f"{pubkey_short}:{seconds_ago}:{int(snr)}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Failed to list neighbors: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_neighbor_remove(self, command: str) -> str:
        """Remove a neighbor."""
        raw_suffix = command[16:]
        pubkey_hex = raw_suffix.strip()

        # Keep MeshCore parity: plain empty is invalid, whitespace-only means remove all.
        if raw_suffix == "":
            return "ERR: Missing pubkey"

        if not self.storage_handler:
            return "Error: Storage not available"

        delete_fn = getattr(self.storage_handler, "delete_neighbors_by_pubkey_prefix", None)
        if not callable(delete_fn):
            return "Error: neighbor.remove not supported by storage backend"

        try:
            if pubkey_hex == "":
                delete_fn(None)
                return "OK"

            if any(ch not in "0123456789abcdefABCDEF" for ch in pubkey_hex):
                return "ERR: bad pubkey"

            delete_fn(pubkey_hex)
            return "OK"
        except Exception as e:
            logger.error(f"neighbor.remove failed: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_discover_neighbors(self, command: str) -> str:
        """Send a discovery request for nearby repeaters."""
        sub = command[18:]
        if sub.strip():
            return "Err - discover.neighbors has no options"

        daemon_instance = getattr(self.config_manager, "daemon", None)
        discovery_helper = getattr(daemon_instance, "discovery_helper", None)
        if not discovery_helper:
            return "Error: Discovery helper not available"

        import asyncio

        loop = self._event_loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is None or not loop.is_running():
            return "Error: Event loop not available"

        try:
            discovery_helper.cleanup_sessions()
            session = discovery_helper.create_session(
                timeout=5,
                filter_mask=(1 << 2),
                since=0,
                prefix_only=False,
                result_enricher=self._auto_add_discovery_result,
            )
            loop.call_soon_threadsafe(discovery_helper.start_session_task, session["session_id"])
            return "OK - Discover sent"
        except Exception as e:
            logger.error(f"discover.neighbors failed: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_discover_scopes(self, command: str) -> str:
        """Refresh the neighbour table, query each neighbour's scopes, publish once.

        Manual trigger for the periodic MQTT neighbors cycle (firmware
        ``discover.scopes``). The cycle takes minutes -- a node-discovery window
        plus one serialized scope query per neighbour -- so this schedules it and
        returns immediately rather than holding the CLI reply open.
        """
        sub = command[15:]
        if sub.strip():
            return "Err - discover.scopes has no options"

        daemon_instance = getattr(self.config_manager, "daemon", None)
        publisher = getattr(daemon_instance, "neighbors_publisher", None)
        if not publisher:
            return "Error: Neighbors publisher not available"

        if not publisher.enabled():
            return "Err - neighbors publishing is disabled (no broker opted in)"

        import asyncio

        loop = self._event_loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is None or not loop.is_running():
            return "Error: Event loop not available"

        try:
            # trigger_cycle owns the "already running" check and tracks the task
            # so shutdown can cancel it. Doing it here instead would race: this
            # runs on the CLI thread, the cycle starts on the event loop.
            started = concurrent.futures.Future()

            def _start():
                try:
                    started.set_result(publisher.trigger_cycle())
                except Exception as exc:  # pragma: no cover - defensive
                    started.set_exception(exc)

            loop.call_soon_threadsafe(_start)
            if started.result(timeout=5):
                return "OK - neighbor scope discovery started"
            return "Err - neighbors cycle already active"
        except Exception as e:
            logger.error(f"discover.scopes failed: {e}", exc_info=True)
            return f"Error: {e}"

    # ==================== Temporary Radio Commands ====================

    def _cmd_tempradio(self, command: str) -> str:
        """Apply temporary radio parameters."""
        # Format: tempradio {freq} {bw} {sf} {cr} {timeout_mins}
        parts = command[10:].split()

        if len(parts) < 5:
            return "Error: Expected freq bw sf cr timeout_mins"

        try:
            freq = float(parts[0])
            bw = float(parts[1])
            sf = int(parts[2])
            cr = int(parts[3])
            timeout_mins = int(parts[4])

            # Validate
            if not (300.0 <= freq <= 2500.0):
                return "Error: invalid frequency"
            if not (7.0 <= bw <= 500.0):
                return "Error: invalid bandwidth"
            if not (5 <= sf <= 12):
                return "Error: invalid spreading factor"
            if not (5 <= cr <= 8):
                return "Error: invalid coding rate"
            if timeout_mins <= 0:
                return "Error: invalid timeout"

            # TODO: Apply temporary radio parameters
            logger.info(f"tempradio: {freq}MHz {bw}kHz SF{sf} CR4/{cr} for {timeout_mins}min")
            return "Error: Not yet implemented"

        except ValueError:
            return "Error, invalid params"

    # ==================== Logging Commands ====================

    def _cmd_log(self, command: str) -> str:
        """Handle log commands."""
        if command == "log start":
            # TODO: Enable logging
            return "Error: Not yet implemented"
        elif command == "log stop":
            # TODO: Disable logging
            return "Error: Not yet implemented"
        elif command == "log erase":
            # TODO: Clear log file
            return "Error: Not yet implemented"
        elif command == "log":
            return "Error: Use journalctl to view logs"
        else:
            return "Unknown log command"
