"""
Login/ANON_REQ packet handling helper for openHop Repeater.

This module processes login requests and manages authentication for all identities.
"""

import asyncio
import logging
import time

from openhop_core.node.handlers.anon_request import AnonRateLimiter, AnonRequestHandler
from openhop_core.node.handlers.login_server import LoginServerHandler
from openhop_core.protocol.constants import PAYLOAD_TYPE_ANON_REQ

logger = logging.getLogger("LoginHelper")


class LoginHelper:
    def __init__(
        self,
        identity_manager,
        packet_injector=None,
        log_fn=None,
        sqlite_handler=None,
        config=None,
    ):

        self.identity_manager = identity_manager
        self.packet_injector = packet_injector
        self.log_fn = log_fn or logger.info
        self.sqlite_handler = sqlite_handler
        self.config = config or {}

        self.handlers = {}
        self.acls = {}  # Per-identity ACLs keyed by hash_byte
        # The same ACLs by registered name ("repeater" or the room's name),
        # which, unlike the hash byte, cannot collide between identities.
        self.acls_by_name = {}
        # The repeater identity's ACL, kept so live config updates can re-apply
        # repeater.security without re-registering the identity.
        self._repeater_acl = None
        self._pending_tasks = set()
        # Shared across all identities so the node's total anon-reply rate is
        # bounded (mirrors firmware anon_limiter: ~4 requests / 2 min).
        self.anon_limiter = AnonRateLimiter()

    def _track_task(self, task: asyncio.Task) -> None:
        self._pending_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Background login task failed: {e}", exc_info=True)

        task.add_done_callback(_on_done)

    def register_identity(
        self, name: str, identity, identity_type: str = "room_server", config: dict = None
    ):
        config = config or {}

        hash_byte = identity.get_public_key()[0]

        # Create ACL for this identity
        from repeater.handler_helpers.acl import ACL, acl_identity_label

        # Get security config for this identity
        if identity_type == "room_server":
            # Room servers use passwords from their settings section only
            settings = config.get("settings", {})

            # Empty strings ('') are treated as "not set" by using 'or None'
            admin_password = settings.get("admin_password") or None
            guest_password = settings.get("guest_password") or None

            # Validate room servers have passwords configured
            if not admin_password and not guest_password:
                logger.error(
                    f"Room server '{name}' MUST have admin_password or guest_password configured. "
                    f"Add them to 'settings' section. Skipping registration."
                )
                return

            # Use configured passwords from settings
            final_security = {
                # Above firmware's 32: room servers store admins only, so their
                # other clients are sessions, which a busy room has many of.
                "max_clients": settings.get("max_clients", 50),
                "admin_password": admin_password,
                "guest_password": guest_password,
                "allow_read_only": settings.get("allow_read_only", True),
            }
        else:
            # Repeater uses security from repeater.security in config
            security = config.get("repeater", {}).get("security", {})
            admin_password = security.get("admin_password") or None
            guest_password = security.get("guest_password") or None
            final_security = {
                # Firmware MAX_CLIENTS. Admins survive restarts, and only another
                # admin grant evicts one, so a small table fills up with them.
                "max_clients": security.get("max_clients", 32),
                "admin_password": admin_password,
                "guest_password": guest_password,
                "allow_read_only": security.get("allow_read_only", False),
            }
            if not admin_password and not guest_password:
                logger.warning(
                    f"Repeater '{name}' has no admin/guest password configured; setup is required before login."
                )
            logger.debug(
                f"Repeater security config: admin_pw={'SET' if final_security['admin_password'] else 'NONE'}, "
                f"guest_pw={'SET' if final_security['guest_password'] else 'NONE'}, "
                f"max_clients={final_security['max_clients']}"
            )

        label = acl_identity_label(name, identity_type)
        existing = self.acls.get(hash_byte)
        if (
            existing is not None
            and not existing.detached
            and existing.identity_pubkey_hex == identity.get_public_key().hex()
        ):
            # A hot re-registration of the same identity (a rename, say). A new
            # ACL would drop every live session's replay watermark and activity,
            # and the room sync loop would stop pushing to logged-in clients.
            identity_acl = existing
            identity_acl.update_settings(
                final_security["max_clients"],
                final_security["admin_password"],
                final_security["guest_password"],
                final_security["allow_read_only"],
                identity_label=label,
            )
            self._drop_acl(identity_acl, keep_name=name)
        else:
            # Entries persist in the database, keyed by the identity's full
            # public key; firmware's repeater keeps every entry with
            # permissions, its room server only admins (saveFilter).
            identity_acl = ACL(
                max_clients=final_security["max_clients"],
                admin_password=final_security["admin_password"],
                guest_password=final_security["guest_password"],
                allow_read_only=final_security["allow_read_only"],
                store=self.sqlite_handler,
                local_identity=identity,
                identity_label=label,
                persist_filter=(lambda c: c.is_admin()) if identity_type == "room_server" else None,
                adopt_by_label=identity_type == "repeater",
            )
            # One ACL per identity: the one this replaces (the same name, or
            # the same stored rows after a key change) stops being listed and
            # stops writing, though its old handlers live until a restart.
            # Detached before the load, so nothing it writes is missed.
            for superseded in {
                self.acls_by_name.get(name),
                self._live_acl_for_store_key(identity_acl.store_key or ""),
            }:
                if superseded is not None and superseded is not identity_acl:
                    superseded.detach_store()
                    self._drop_acl(superseded)
            identity_acl.load()

        displaced = self.acls.get(hash_byte)
        if displaced is not None and displaced is not identity_acl and not displaced.detached:
            # Handlers are keyed by the hash byte, so the text and request
            # helpers can reach only one of two identities sharing it.
            logger.warning(
                f"'{name}' shares hash 0x{hash_byte:02X} with another identity; "
                f"messages and requests to that hash reach '{name}' only"
            )
        self.acls[hash_byte] = identity_acl
        self.acls_by_name[name] = identity_acl
        if identity_type != "room_server":
            self._repeater_acl = identity_acl
        logger.info(f"Created ACL for {identity_type} '{name}': hash=0x{hash_byte:02X}")

        # Create auth callback that uses this identity's ACL
        def auth_callback_with_context(
            client_identity, shared_secret, password, timestamp, sync_since=None
        ):
            success, permissions = identity_acl.authenticate_client(
                client_identity=client_identity,
                shared_secret=shared_secret,
                password=password,
                timestamp=timestamp,
                sync_since=sync_since,
                target_identity_hash=hash_byte,
                target_identity_name=name,
                # The registered type is authoritative: a room config without a
                # "type" key must not authenticate as a repeater.
                target_identity_config={**config, "type": identity_type},
            )
            if success and identity_type == "room_server" and self.sqlite_handler is not None:
                try:
                    sync_kwargs = {}
                    if sync_since is not None:
                        sync_kwargs["sync_since"] = sync_since
                    self.sqlite_handler.upsert_client_sync(
                        room_hash=f"0x{hash_byte:02X}",
                        client_pubkey=client_identity.get_public_key().hex(),
                        pending_ack_crc=0,
                        push_post_timestamp=0,
                        ack_timeout_time=0,
                        push_failures=0,
                        last_activity=time.time(),
                        **sync_kwargs,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to reset room sync guard state after login for hash=0x{hash_byte:02X}: {e}"
                    )
            return success, permissions

        handler = LoginServerHandler(
            local_identity=identity,
            log_fn=self.log_fn,
            authenticate_callback=auth_callback_with_context,
            is_room_server=(identity_type == "room_server"),
        )

        # Wrap the login handler in an anon-request dispatcher so anonymous
        # regions/owner/basic discovery queries are answered instead of being
        # mis-parsed as failed logins (MeshCore 1.16.0 discovery feature).
        anon_handler = AnonRequestHandler(
            local_identity=identity,
            log_fn=self.log_fn,
            login_handler=handler,
            anon_limiter=self.anon_limiter,
            region_names_fn=self._format_region_names,
            owner_info_fn=self._make_owner_info_fn(name, config),
            features_fn=self._make_features_fn(config),
            clock_fn=lambda: int(time.time()),
        )
        # Wires the send callback through to both the wrapper and login handler.
        anon_handler.set_send_packet_callback(self._send_packet_with_delay)

        self.handlers[hash_byte] = anon_handler

        logger.info(f"Registered {identity_type} '{name}' login handler: hash=0x{hash_byte:02X}")

    def refresh_repeater_security(self, config: dict = None) -> bool:
        """Re-apply ``repeater.security`` from config to the live repeater ACL.

        The ACL captures its passwords at registration, so without this a saved
        password change would only take effect after a restart. Room-server ACLs
        keep their per-identity ``settings`` passwords and are not touched.
        """
        acl = self._repeater_acl
        if acl is None:
            return False

        cfg = config if isinstance(config, dict) else self.config
        security = (cfg or {}).get("repeater", {}).get("security", {}) or {}

        try:
            max_clients = int(security.get("max_clients", acl.max_clients))
        except (TypeError, ValueError):
            max_clients = acl.max_clients
            logger.warning(
                "Ignoring invalid repeater.security.max_clients=%r during security refresh",
                security.get("max_clients"),
            )
        acl.update_settings(
            max_clients,
            security.get("admin_password"),
            security.get("guest_password"),
            bool(security.get("allow_read_only", False)),
        )

        logger.info("Refreshed repeater ACL security settings from config")
        return True

    def _format_region_names(self) -> str:
        """Build the comma-separated region-names string for an anon regions reply.

        Mirrors firmware ``RegionMap::exportNamesTo`` with ``REGION_DENY_FLOOD``:
        emit the ``*`` wildcard region first (unless unscoped flood is denied),
        then each allow-flood named region with a leading ``#`` stripped, with no
        trailing comma. The firmware wildcard is the always-present default flood
        scope; openhop_repeater models that via ``mesh.unscoped_flood_allow``
        (falling back to ``mesh.global_flood_allow``, default allow).
        """
        parts = []

        mesh_cfg = self.config.get("mesh", {}) if isinstance(self.config, dict) else {}
        unscoped_allow = mesh_cfg.get(
            "unscoped_flood_allow", mesh_cfg.get("global_flood_allow", True)
        )
        if unscoped_allow:
            parts.append("*")

        if self.sqlite_handler:
            try:
                keys = self.sqlite_handler.get_transport_keys()
            except Exception as e:
                logger.warning(f"Failed to read transport keys for regions reply: {e}")
                keys = []
            for rec in keys or []:
                if rec.get("flood_policy", "deny") != "allow":
                    continue
                name = (rec.get("name") or "").strip()
                if not name or name == "*":
                    continue  # wildcard handled above
                parts.append(name[1:] if name.startswith("#") else name)

        return ",".join(parts)

    @staticmethod
    def _make_owner_info_fn(name: str, config: dict):
        """Build an owner-info callback returning ``(node_name, owner_info)``."""

        def owner_info_fn():
            cfg = config or {}
            repeater_cfg = cfg.get("repeater", {})
            node_name = repeater_cfg.get("node_name") or name or "pyMC"
            owner = repeater_cfg.get("owner_info", "") or ""
            return (node_name, owner)

        return owner_info_fn

    @staticmethod
    def _make_features_fn(config: dict):
        """Build a feature-flags callback (bit0 = bridge, bit7 = forwarding disabled)."""

        def features_fn():
            cfg = config or {}
            mode = cfg.get("repeater", {}).get("mode", "forward")
            features = 0
            if mode != "forward":  # monitor / no_tx => not forwarding
                features |= 0x80
            return features

        return features_fn

    async def process_login_packet(self, packet):

        try:
            if len(packet.payload) < 1:
                return False

            dest_hash = packet.payload[0]

            handler = self.handlers.get(dest_hash)
            if handler:
                logger.debug(f"Routing login to identity: hash=0x{dest_hash:02X}")
                # The handler authenticates only when the request decrypted for
                # this identity. Otherwise the dest hash collided with ours but the
                # ANON_REQ is not really for us — do not consume it, so the engine
                # can still forward/re-flood it (#353).
                result = await handler(packet)
                if not result.authenticated:
                    logger.debug(
                        f"ANON_REQ dest 0x{dest_hash:02X} did not decrypt for a local "
                        f"identity (hash collision), allowing forward"
                    )
                    return False
                packet.mark_do_not_retransmit()
                return True
            else:
                # ANON_REQ to other nodes (e.g. another repeater's regions/owner
                # query overheard on-air) is normal; log at DEBUG so the dest is
                # visible when diagnosing "why didn't my repeater answer".
                ptype = getattr(packet, "get_payload_type", lambda: None)()
                if ptype == PAYLOAD_TYPE_ANON_REQ:
                    logger.debug(
                        f"ANON_REQ for hash 0x{dest_hash:02X} not addressed to a local "
                        f"identity ({sorted(f'0x{h:02X}' for h in self.handlers)}); ignoring"
                    )
                else:
                    logger.debug(
                        f"No login handler registered for hash 0x{dest_hash:02X}, allowing forward"
                    )
                return False

        except Exception as e:
            logger.error(f"Error processing login packet: {e}")
            return False

    def _send_packet_with_delay(self, packet, delay_ms: int):

        if self.packet_injector:
            task = asyncio.create_task(self._delayed_send(packet, delay_ms))
            self._track_task(task)
        else:
            logger.error("No packet injector configured, cannot send login response")

    async def _delayed_send(self, packet, delay_ms: int):

        await asyncio.sleep(delay_ms / 1000.0)
        try:
            await self.packet_injector(packet, wait_for_ack=False)
            logger.debug(f"Sent login response after {delay_ms}ms delay")
        except Exception as e:
            logger.error(f"Error sending login response: {e}")

    def get_acl_dict(self):
        """Return dictionary of ACLs keyed by identity hash."""
        return self.acls

    def get_acl_for_identity(self, hash_byte: int):
        """Get ACL for a specific identity."""
        return self.acls.get(hash_byte)

    def get_acl_by_name(self, name: str):
        """The ACL of the identity registered under ``name`` ("repeater" or a room's name)."""
        return self.acls_by_name.get(name)

    def move_room_acl(
        self,
        old_name: str,
        old_pubkey_hex: str,
        new_pubkey_hex: str,
        new_name: str,
        commit=None,
    ) -> None:
        """Move a room server's stored ACL to its new key and name around ``commit``.

        ``update_identity`` calls this when it changes a room's key or name,
        with ``commit`` saving the config; see ``move_identity_acl``. Room
        servers are not adopted by label, so without the move a new key would
        start with an empty ACL. The live ACL writes to the moved rows after,
        so changes made before the restart that applies the new key are kept.
        """
        from repeater.handler_helpers.acl import acl_identity_label, move_identity_acl

        label = acl_identity_label(new_name, "room_server")
        if self.sqlite_handler is None and old_pubkey_hex.lower() != new_pubkey_hex.lower():
            # A key change with nowhere to move the entries would save a key
            # whose access list is gone.
            raise RuntimeError("no ACL store is available to move the room's access list")
        live = self.acls_by_name.get(old_name)
        if live is None or live.store_key != old_pubkey_hex.lower():
            # The name index can lag the config (a hot reload refused), but
            # the store key names the rows, and one ACL at most holds it.
            live = self._live_acl_for_store_key(old_pubkey_hex)
        if live is not None:
            live.move_store(new_pubkey_hex, label, commit)
            return
        move_identity_acl(
            self.sqlite_handler,
            old_pubkey_hex,
            new_pubkey_hex,
            acl_identity_label(old_name, "room_server"),
            label,
            commit,
        )

    def forget_identity_acl(self, name: str, pubkey_hex: str) -> int:
        """Drop a deleted identity's stored ACL, and stop its live ACL writing it back."""
        from repeater.handler_helpers.acl import acl_identity_label

        live = self.acls_by_name.get(name)
        if live is None or live.store_key != pubkey_hex.lower():
            # As in move_room_acl: a refused hot reload leaves the live ACL
            # under its old name, still writing rows for this key.
            live = self._live_acl_for_store_key(pubkey_hex)
        if live is not None:
            live.detach_store()
            self._drop_acl(live)
        if self.sqlite_handler is None:
            return 0
        # Every key: a rekey whose cleanup failed left rows under this label at
        # the old one. Names are unique, so no other room holds this label.
        return self.sqlite_handler.delete_acl_label(acl_identity_label(name, "room_server"))

    def unregister_identity(self, identity) -> bool:
        """Stop answering logins for ``identity``: it was deleted or given a new key.

        Only its own handler goes; another identity now registered on the
        same hash byte keeps its. Its ACL is detached and unlisted.
        """
        pubkey = identity.get_public_key()
        hash_byte = pubkey[0]
        handler = self.handlers.get(hash_byte)
        owner = getattr(handler, "local_identity", None)
        removed = False
        if owner is not None and owner.get_public_key() == pubkey:
            del self.handlers[hash_byte]
            removed = True
        # Both indexes: one whose hash byte another identity took is only
        # listed by name.
        listed = [*self.acls.values(), *self.acls_by_name.values()]
        for acl in {a for a in listed if a.identity_pubkey_hex == pubkey.hex()}:
            acl.detach_store()
            self._drop_acl(acl)
        return removed

    def _drop_acl(self, acl, keep_name: str = None) -> None:
        """Remove ``acl`` from both indexes, except under ``keep_name``."""
        for name, entry in list(self.acls_by_name.items()):
            if entry is acl and name != keep_name:
                del self.acls_by_name[name]
        if keep_name is None:
            for hash_byte, entry in list(self.acls.items()):
                if entry is acl:
                    del self.acls[hash_byte]

    def _live_acl_for_store_key(self, pubkey_hex: str):
        key = pubkey_hex.lower()
        return next((acl for acl in self.acls_by_name.values() if acl.store_key == key), None)

    def list_authenticated_clients(self, hash_byte: int = None):
        """List authenticated clients for a specific identity or all identities."""
        if hash_byte is not None:
            acl = self.acls.get(hash_byte)
            return acl.get_all_clients() if acl else []

        # Return clients from all ACLs
        all_clients = []
        for acl in self.acls.values():
            all_clients.extend(acl.get_all_clients())
        return all_clients
