import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from openhop_core.protocol import Identity
from openhop_core.protocol.constants import PUB_KEY_SIZE

# ACL roles come from openhop_core, which mirrors firmware
# ``src/helpers/ClientACL.h``: the role is the LOW TWO BITS of the permissions
# byte and ADMIN is 3 — it is not "the 0x02 bit".
#
# This import is deliberately fail-closed. A core without these symbols still
# builds the login reply's is_admin byte from ``permissions & 0x02``, which
# also matches READ_WRITE (2); pairing it with this module would silently
# announce a room server's read-write clients as admins. Refusing to start is
# the safe failure.
try:
    from openhop_core.protocol.constants import (
        PERM_ACL_ADMIN,
        PERM_ACL_GUEST,
        PERM_ACL_READ_ONLY,
        PERM_ACL_READ_WRITE,
        PERM_ACL_ROLE_MASK,
    )
    from openhop_core.protocol.constants import acl_is_admin as is_admin_permissions
    from openhop_core.protocol.constants import acl_role as role_of
except ImportError as exc:  # pragma: no cover - exercised by the install, not tests
    raise ImportError(
        "openhop_core is too old: it does not export PERM_ACL_* / acl_is_admin. "
        "Install openhop_core with the ACL role fix (fix/login-perms or later) — "
        "an older core encodes admin as the 0x02 bit and would announce "
        "read-write clients as admins."
    ) from exc

logger = logging.getLogger("ACL")

_ROLE_NAMES = {
    PERM_ACL_GUEST: "guest",
    PERM_ACL_READ_ONLY: "read_only",
    PERM_ACL_READ_WRITE: "read_write",
    PERM_ACL_ADMIN: "admin",
}


def role_name(permissions: int) -> str:
    """Human-readable role name for logs and the web API."""
    return _ROLE_NAMES[role_of(permissions)]


def acl_identity_label(name: str, identity_type: str) -> str:
    """Label naming the owner of an identity's stored ACL.

    There is one repeater identity whatever it is named, so its label is
    fixed, and it is the only one adopted by label after a key change.
    Other identities are told apart by their configured name.
    """
    if identity_type == "repeater":
        return "repeater"
    return f"{identity_type}:{name}"


def move_identity_acl(
    store,
    old_key: str,
    new_key: str,
    old_label: str,
    new_label: str,
    commit=None,
) -> None:
    """Move an identity's stored ACL to a new key and label, around committing them.

    Only entries under ``old_label`` move; others at the old key belong to
    another identity. For a key change this copies the entries to
    ``new_key``, calls ``commit`` (saving the config that names the new key),
    then deletes the old entries. Every failure leaves the key the config
    names holding its entries: if the copy or the commit fails the old ones
    are untouched (the copies are dropped); if only the final delete fails,
    the old key keeps a stale copy, which only an identity with this label
    and that key would load. Retrying the change replaces a stale copy.

    For a rename (same key) the entries are relabelled, then committed, and
    the old label is put back if the commit fails: an identity loads only
    entries under its own label, so the label must follow the saved name.
    Raises what the copy, relabel or commit raised.
    """
    old_key = old_key.lower()
    new_key = new_key.lower()
    if store is None:
        if commit is not None:
            commit()
        return

    if old_label != new_label:
        # Names are unique, so no live identity holds the label this one is
        # taking: rows already under it are leftovers of a deleted identity
        # whose cleanup failed, and must not become this one's grants. This
        # must succeed; the move (and the rename) fails otherwise.
        store.delete_acl_label(new_label)

    if old_key == new_key:
        if old_label == new_label:
            if commit is not None:
                commit()
            return
        # Leftovers under the old label elsewhere would keep it once the
        # room stops carrying it; they belong to no one.
        _sweep_leftovers(store, old_label, new_key)
        store.relabel_acl_identity(new_key, old_label, new_label)
        if commit is not None:
            try:
                commit()
            except Exception:
                try:
                    store.relabel_acl_identity(new_key, new_label, old_label)
                except Exception as e:
                    logger.warning(f"Could not put back the ACL label of '{old_label}': {e}")
                raise
        return

    store.copy_acl_identity(old_key, new_key, old_label, new_label)
    if commit is not None:
        try:
            commit()
        except Exception:
            try:
                store.delete_acl_identity(new_key, new_label)
            except Exception as e:
                logger.warning(
                    f"Could not drop the ACL copied to {new_key[:8]}...: {e}. "
                    f"Retrying the change replaces it"
                )
            raise
    try:
        store.delete_acl_identity(old_key, old_label)
    except Exception as e:
        logger.warning(
            f"Could not drop the ACL left under the old key {old_key[:8]}...: {e}. "
            f"It is swept by the room's next rename or key change, or its deletion"
        )
    if old_label != new_label:
        _sweep_leftovers(store, old_label, None)
    _sweep_leftovers(store, new_label, new_key)


def _sweep_leftovers(store, label: str, current_key: Optional[str]) -> None:
    """Best-effort: drop rows under ``label`` at any key but ``current_key``.

    A room owns only the rows under its label at its current key. Others
    are left by a cleanup that failed; a failure here is retried by the
    next sweep. Not run on load: a room whose key was edited by hand in the
    config would lose rows that setting the key back recovers.
    """
    sweep = getattr(store, "delete_acl_label", None)
    if sweep is None:
        return
    try:
        swept = sweep(label, current_key)
    except Exception as e:
        logger.warning(f"Could not sweep leftover ACL entries of '{label}': {e}")
        return
    if swept:
        logger.info(f"Swept {swept} leftover ACL entr{'y' if swept == 1 else 'ies'} of '{label}'")


class ClientInfo:
    """Represents an authenticated client in the access control list."""

    def __init__(self, identity: Identity, permissions: int = 0):
        self.id = identity
        self.permissions = permissions
        self.shared_secret = b""
        self.last_timestamp = 0
        self.last_activity = 0
        self.last_login_success = 0
        self.out_path_len = -1
        self.out_path = bytearray()
        self.sync_since = 0  # For room servers - timestamp of last synced message

    def is_admin(self) -> bool:
        return is_admin_permissions(self.permissions)

    def is_guest(self) -> bool:
        return role_of(self.permissions) == PERM_ACL_GUEST

    def role_name(self) -> str:
        """Role name ("guest"/"read_only"/"read_write"/"admin") for logs and the API."""
        return role_name(self.permissions)


class ACLStoreError(RuntimeError):
    """An ACL change could not be written to the store and was not applied."""


class ACL:
    """Per-identity access control list, firmware ``ClientACL``.

    With a ``store`` the entries firmware writes to ``/s_contacts`` survive a
    restart: every entry with non-zero permissions that ``persist_filter``
    accepts. The repeater passes no filter; a room server keeps admins only,
    as firmware's ``saveFilter``. Permissions are written when they change;
    a login by a stored entry writes only its login time, one small update,
    so the web UI shows when each admin last logged in across a restart.

    The table is changed from the event loop (logins) and from web request
    threads (setperm, removal), so each change and its write happen under one
    lock: otherwise a removal racing a grant could leave the grant stored.
    """

    def __init__(
        self,
        max_clients: int = 50,
        admin_password: Optional[str] = None,
        guest_password: Optional[str] = None,
        allow_read_only: bool = True,
        store=None,
        local_identity=None,
        identity_label: Optional[str] = None,
        persist_filter: Optional[Callable[["ClientInfo"], bool]] = None,
        adopt_by_label: bool = False,
    ):
        self.max_clients = max_clients
        self.admin_password = admin_password or ""
        self.guest_password = guest_password or ""
        self.allow_read_only = allow_read_only
        self.clients: Dict[bytes, ClientInfo] = {}

        self._lock = threading.RLock()
        self._store = store
        self._local_identity = local_identity
        self._identity_label = identity_label or ""
        self._persist_filter = persist_filter
        self._adopt_by_label = adopt_by_label
        # Rows are stored under this key: the identity's public key at
        # construction, until move_store() moves them.
        self._store_key: Optional[str] = (
            bytes(local_identity.get_public_key()[:PUB_KEY_SIZE]).hex()
            if local_identity is not None
            else None
        )
        # Permissions as last written to the store, keyed like ``clients``.
        self._persisted: Dict[bytes, int] = {}
        # Replay watermarks of evicted clients: a key that comes back must not
        # accept a login it already used. RAM only and capped, like the table.
        self._evicted_watermarks: Dict[bytes, int] = {}
        # Evicted entries still stored, keyed to the client that displaced them
        # (None for one that is never stored): each row is deleted once that
        # client's grant is stored, never on an unrelated write.
        self._pending_evictions: Dict[bytes, Optional[bytes]] = {}
        # Login times as last written to the store, so a login in the same
        # second as the stored one costs no write.
        self._persisted_login: Dict[bytes, int] = {}
        # Set when load() could not read the store: the table is then not the
        # stored one, and the web API says so rather than showing it as empty.
        self.load_error: Optional[str] = None
        self._detached = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persistence_enabled(self) -> bool:
        return self._store is not None and self._store_key is not None

    def _derive_secret(self, pub_key: bytes) -> bytes:
        """Shared secret with a client, recomputed rather than read from disk."""
        if self._local_identity is None:
            return b""
        try:
            return Identity(pub_key).calc_shared_secret(self._local_identity.get_private_key())
        except Exception as e:
            logger.warning(f"Could not derive shared secret for {pub_key[:6].hex()}...: {e}")
            return b""

    def load(self) -> int:
        """Fill the table from the store. Returns the number of entries loaded.

        Loaded entries have ``last_activity`` 0, as in firmware: known, not
        active, until the client logs in again. The replay watermark also
        starts at 0, as firmware keeps it in RAM only. ``last_login_success``
        is restored from the store; firmware does not keep it, but it tells
        an operator whether a grant is still in use.

        Every stored entry is loaded even past ``max_clients``: lowering the
        limit must not silently revoke provisioned grants. An over-full table
        still evicts non-admins for newcomers; once all are admins, only an
        admin grant evicts one (see ``_put_client``).

        A failed read is recorded in ``load_error`` and retried by the next
        login or change, so stored keys work again once the store recovers.
        """
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> int:
        if not self._persistence_enabled():
            return 0
        try:
            rows = self._store.load_acl_entries(
                self._store_key,
                identity_label=self._identity_label or None,
                adopt_label=self._identity_label if self._adopt_by_label else None,
            )
        except Exception as e:
            self.load_error = str(e)
            logger.error(
                f"Failed to load the stored ACL for '{self._identity_label}': {e}. "
                f"Stored entries cannot log in until it loads."
            )
            return 0
        self.load_error = None

        loaded = 0
        for row in rows:
            try:
                pub_key = bytes.fromhex(row["client_pubkey"])
                permissions = int(row["permissions"]) & 0xFF
                last_login = int(row.get("last_login") or 0)
                if len(pub_key) != PUB_KEY_SIZE or permissions == 0:
                    continue
                identity = Identity(pub_key)
            except Exception:
                logger.warning(f"Skipping malformed ACL row for '{self._identity_label}': {row}")
                continue
            self._persisted[pub_key] = permissions
            self._persisted_login[pub_key] = last_login
            live = self.clients.get(pub_key)
            if live is not None:
                # Joined while the store could not be read (a retried load).
                # Keep the session; a guest session takes its stored grant.
                if live.permissions == 0:
                    live.permissions = permissions
                # The later login wins: one made while the entry was not known
                # to be stored went unrecorded, and is written now rather than
                # at its next login; an entry that has not logged in since
                # (setperm during the outage) shows the stored time.
                live.last_login_success = max(live.last_login_success, last_login)
                self._record_login(pub_key, live)
            else:
                client = ClientInfo(identity, permissions)
                client.shared_secret = self._derive_secret(pub_key)
                client.last_login_success = last_login
                self.clients[pub_key] = client
            loaded += 1

        if loaded:
            logger.info(
                f"Loaded {loaded} ACL entr{'y' if loaded == 1 else 'ies'} for '{self._identity_label}'"
            )
        if loaded > self.max_clients:
            logger.warning(
                f"ACL for '{self._identity_label}' holds {loaded} stored entries, over "
                f"max_clients={self.max_clients}; raise max_clients or remove entries"
            )
        return loaded

    def _retry_failed_load_locked(self) -> None:
        if self.load_error is not None:
            self._load_locked()

    def move_store(self, identity_pubkey_hex: str, identity_label: str, commit=None) -> None:
        """Move the stored entries to a new identity key and label, and write there after.

        See ``move_identity_acl``. Holds the lock throughout, so no change can
        be written under the old key after its rows have gone, or lost while
        the config is committed. The live identity is unchanged, so
        ``identity_pubkey_hex`` still names the key secrets are derived with.
        """
        new_key = identity_pubkey_hex.lower()
        with self._lock:
            move_identity_acl(
                self._store if self._persistence_enabled() else None,
                self._store_key or new_key,
                new_key,
                self._identity_label,
                identity_label,
                commit,
            )
            self._store_key = new_key
            self._identity_label = identity_label

    def update_settings(
        self,
        max_clients: int,
        admin_password: Optional[str],
        guest_password: Optional[str],
        allow_read_only: bool,
        identity_label: Optional[str] = None,
    ) -> None:
        """Apply new security settings in one step, so a login never sees half of them."""
        with self._lock:
            self.max_clients = max_clients
            self.admin_password = admin_password or ""
            self.guest_password = guest_password or ""
            self.allow_read_only = allow_read_only
            if identity_label is not None:
                self._identity_label = identity_label

    def set_label(self, identity_label: str) -> None:
        """The label written with later changes, as after a rename."""
        with self._lock:
            self._identity_label = identity_label

    def detach_store(self) -> None:
        """Stop writing to the store: the identity was deleted or replaced.

        Its handlers stay registered until a restart, and a login there must
        not write back the entries the delete removed.
        """
        with self._lock:
            self._store = None
            self._persisted.clear()
            self._persisted_login.clear()
            self.load_error = None
            self._detached = True

    @property
    def detached(self) -> bool:
        """True once detach_store() has run: this ACL no longer owns stored rows."""
        return self._detached

    @property
    def store_key(self) -> Optional[str]:
        return self._store_key

    @property
    def identity_pubkey_hex(self) -> Optional[str]:
        """Public key of the local identity this ACL derives secrets with."""
        if self._local_identity is None:
            return None
        return bytes(self._local_identity.get_public_key()[:PUB_KEY_SIZE]).hex()

    def _should_persist(self, client: "ClientInfo") -> bool:
        if client.permissions == 0:
            return False
        return self._persist_filter is None or bool(self._persist_filter(client))

    def _sync_entry(self, pub_key: bytes) -> None:
        """Write one entry's current state to the store if it changed.

        Raises ACLStoreError when the write fails, leaving the record of what
        is stored untouched so the next change retries.
        """
        if not self._persistence_enabled():
            return
        client = self.clients.get(pub_key)
        wanted = client.permissions if client is not None and self._should_persist(client) else None
        if self._persisted.get(pub_key) == wanted:
            return
        # A client that logged in before it was stored (a guest promoted by
        # setperm) keeps its login time; None leaves a stored one alone.
        last_login = (client.last_login_success or None) if client is not None else None
        try:
            if wanted is None:
                self._store.delete_acl_entry(self._store_key, pub_key.hex())
            else:
                self._store.upsert_acl_entry(
                    self._store_key,
                    self._identity_label,
                    pub_key.hex(),
                    wanted,
                    last_login=last_login,
                )
        except Exception as e:
            logger.error(
                f"Failed to save ACL entry {pub_key[:6].hex()}... for '{self._identity_label}': {e}"
            )
            raise ACLStoreError(str(e)) from e
        if wanted is None:
            self._persisted.pop(pub_key, None)
            self._persisted_login.pop(pub_key, None)
        else:
            self._persisted[pub_key] = wanted
            if last_login is not None:
                self._persisted_login[pub_key] = last_login

    def _record_login(self, pub_key: bytes, client: "ClientInfo") -> None:
        """Store a stored entry's login time. Call under the lock, after ``_sync_entry``.

        Best-effort: the time is decoration, and a failed write must not fail
        the login it records.
        """
        if not self._persistence_enabled() or pub_key not in self._persisted:
            return
        if not client.last_login_success:
            return
        if self._persisted_login.get(pub_key) == client.last_login_success:
            return
        try:
            self._store.touch_acl_login(self._store_key, pub_key.hex(), client.last_login_success)
        except Exception as e:
            logger.warning(
                f"Could not save the login time of {pub_key[:6].hex()}... "
                f"for '{self._identity_label}': {e}"
            )
            return
        self._persisted_login[pub_key] = client.last_login_success

    def is_persisted(self, pub_key: bytes) -> bool:
        """Whether this entry is in the store, so it survives a restart."""
        return bytes(pub_key[:PUB_KEY_SIZE]) in self._persisted

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------

    def _put_client(
        self,
        identity: Identity,
        evicted: Optional[list] = None,
        admin_grant: bool = False,
        keep_grants: bool = False,
    ) -> Optional["ClientInfo"]:
        """Find or add a client, firmware ``putClient``. Call under the lock.

        When the table is full the least recently active non-admin is evicted.
        When every entry is an admin, firmware evicts its last slot, whoever
        the newcomer is. Here only an authenticated admin grant (``admin_grant``:
        the admin password, or setperm to an admin role) may evict an admin, so
        a blank-password or guest login cannot strip provisioned admins one by
        one; any other newcomer is refused. The admin evicted is the one seen
        least recently, by its last login when it has not been active since a
        restart: the stored order is not the order entries were added, so a
        "last slot" would be arbitrary here.

        ``keep_grants`` (a blank-password newcomer, who proved nothing) evicts
        only entries that are not stored, so anonymous logins cannot churn
        provisioned grants out of the table; with none to evict it is refused.

        Given ``evicted``, the evicted ``(key, client)`` is appended there and
        its stored entry left for the caller to delete once the newcomer is
        stored, so a failed write can put it back.
        """
        pub_key = bytes(identity.get_public_key()[:PUB_KEY_SIZE])
        client = self.clients.get(pub_key)
        if client is not None:
            return client

        # Read before an eviction below can prune it.
        watermark = self._evicted_watermarks.get(pub_key, 0)
        if len(self.clients) >= self.max_clients:
            if not self.clients:
                logger.error(f"ACL for '{self._identity_label}' has max_clients={self.max_clients}")
                return None
            candidates = [
                (k, c)
                for k, c in self.clients.items()
                if not c.is_admin() and not (keep_grants and self._should_persist(c))
            ]
            if keep_grants and not candidates:
                logger.warning(
                    f"ACL for '{self._identity_label}' is full of stored grants "
                    f"(max_clients={self.max_clients}): refused a blank-password newcomer"
                )
                return None
            if candidates:
                evict_key, evict_client = min(candidates, key=lambda kc: kc[1].last_activity)
                logger.info(f"ACL full, evicted least active client {evict_key[:6].hex()}...")
            elif not admin_grant:
                logger.warning(
                    f"ACL for '{self._identity_label}' is full of admins "
                    f"(max_clients={self.max_clients}): refused a non-admin newcomer"
                )
                return None
            else:
                evict_key, evict_client = min(
                    self.clients.items(),
                    key=lambda kc: max(kc[1].last_activity, kc[1].last_login_success),
                )
                logger.warning(
                    f"ACL for '{self._identity_label}' is full of admins "
                    f"(max_clients={self.max_clients}): evicted the least recently "
                    f"seen, {evict_key[:6].hex()}..."
                )
            del self.clients[evict_key]
            self._evicted_watermarks.pop(evict_key, None)
            self._evicted_watermarks[evict_key] = evict_client.last_timestamp
            if evicted is not None:
                evicted.append((evict_key, evict_client))
            else:
                self._drop_stored_eviction(evict_key)

        client = ClientInfo(identity, 0)
        client.last_timestamp = watermark
        self.clients[pub_key] = client
        return client

    def _undo_put(self, pub_key: bytes, is_new: bool, evicted: list) -> None:
        """Take back a refused newcomer and put back whoever it displaced."""
        if is_new:
            self.clients.pop(pub_key, None)
        for evict_key, evict_client in evicted:
            self.clients[evict_key] = evict_client
            self._evicted_watermarks.pop(evict_key, None)  # back with its own

    def _defer_evictions(self, evicted: list, replacement: Optional[bytes]) -> None:
        for evict_key, _ in evicted:
            # Its own outstanding evictions pass to whoever displaced it.
            for key, owner in self._pending_evictions.items():
                if owner == evict_key:
                    self._pending_evictions[key] = replacement
            if evict_key in self._persisted:  # nothing to delete otherwise
                self._pending_evictions[evict_key] = replacement
        self._trim_watermarks()

    def _trim_watermarks(self) -> None:
        # Only after a login or grant is accepted, so a refused one can never
        # push out the watermark that refuses its replay. Past the cap the
        # oldest go: that key's next login is then checked as a newcomer's, as
        # firmware checks every evicted client's. A pending eviction dropped
        # here just stays stored, and returns after a restart.
        cap = max(self.max_clients, 1) * 8
        for table in (self._evicted_watermarks, self._pending_evictions):
            while len(table) > cap:
                del table[next(iter(table))]

    def _commit_evictions(self, evicted: list, replacement: Optional[bytes] = None) -> None:
        """Delete the entries ``replacement`` displaced, now that it is stored.

        Called once ``replacement``'s grant is written (None: it is never
        stored), so this also completes its evictions that an earlier failed
        write left stored. Another client's evictions are left alone.
        """
        self._defer_evictions(evicted, replacement)
        for evict_key, owner in list(self._pending_evictions.items()):
            if owner != replacement:
                continue
            # Writes whatever it is now: deleted if gone, its current grant if
            # it came back (as a guest, its stored admin row is deleted too).
            try:
                self._sync_entry(evict_key)
            except ACLStoreError:
                continue  # still stored; the next write of this grant retries
            del self._pending_evictions[evict_key]

    def _drop_stored_eviction(self, evict_key: bytes) -> None:
        try:
            self._sync_entry(evict_key)
        except ACLStoreError:
            # Still stored; it returns after a restart. Evicting it from
            # memory must not block the newcomer.
            pass

    def apply_permissions(self, pub_key: bytes, permissions: int) -> bool:
        """``setperm``, firmware ``ClientACL::applyPermissions``.

        A guest role deletes the first entry whose key starts with ``pub_key``,
        so a prefix is enough. Any other role needs the full key, finds or adds
        the entry, and stores the whole permissions byte, not just the role.

        Returns False for invalid parameters, as firmware. Raises ACLStoreError
        when the change cannot be stored; the table is then left as it was.
        """
        permissions &= 0xFF
        pub_key = bytes(pub_key)
        with self._lock:
            self._retry_failed_load_locked()
            if role_of(permissions) == PERM_ACL_GUEST:
                # Firmware matches an empty prefix against the first entry and
                # deletes it. Refuse instead: "setperm  0" should not drop someone.
                if not pub_key:
                    return False
                match = next((k for k in self.clients if k.startswith(pub_key)), None)
                if match is None:
                    return False
                removed = self.clients.pop(match)
                try:
                    self._sync_entry(match)
                except ACLStoreError:
                    self.clients[match] = removed
                    raise
                logger.info(f"setperm: removed {match[:6].hex()}... from ACL")
                return True

            if len(pub_key) < PUB_KEY_SIZE:
                return False
            pub_key = pub_key[:PUB_KEY_SIZE]
            try:
                identity = Identity(pub_key)
            except Exception:
                # Not a valid ed25519 key. Firmware stores any 32 bytes, but such
                # an entry could never log in, and Identity() refuses it.
                logger.info(f"setperm: {pub_key[:6].hex()}... is not a valid public key")
                return False
            existing = self.clients.get(pub_key)
            previous = existing.permissions if existing is not None else None
            evicted = []
            client = self._put_client(
                identity, evicted, admin_grant=is_admin_permissions(permissions)
            )
            if client is None:
                return False
            client.permissions = permissions
            client.shared_secret = self._derive_secret(pub_key) or client.shared_secret
            try:
                self._sync_entry(pub_key)
            except ACLStoreError:
                if previous is None:
                    # The grant failed, so nobody made room for it.
                    self._undo_put(pub_key, True, evicted)
                else:
                    client.permissions = previous
                raise
            self._commit_evictions(evicted, bytes(pub_key))
            logger.info(f"setperm: {pub_key[:6].hex()}... permissions=0x{permissions:02X}")
            return True

    def format_acl_lines(self) -> List[str]:
        """Rows for ``get acl``: ``"%02X <pubkey>"`` for each entry with permissions."""
        with self._lock:
            return [
                f"{client.permissions:02X} {key.hex().upper()}"
                for key, client in self.clients.items()
                if client.permissions != 0
            ]

    def _is_replay(self, client: ClientInfo, timestamp: int) -> bool:
        if timestamp <= client.last_timestamp:
            logger.warning(
                f"Possible replay attack! timestamp={timestamp}, last={client.last_timestamp}"
            )
            return True
        return False

    def _touch_client_session(
        self,
        client: ClientInfo,
        shared_secret: bytes,
        timestamp: int,
        sync_since: int = None,
    ) -> None:
        now = int(time.time())
        # Monotonic: the replay watermark must never move backwards, even if
        # another accepted request advanced it between the replay check and
        # this write.
        client.last_timestamp = max(client.last_timestamp, timestamp)
        client.last_activity = now
        client.last_login_success = now
        client.shared_secret = shared_secret
        if sync_since is not None:
            client.sync_since = sync_since
            logger.debug(f"Stored sync_since={sync_since} for client")

    def authenticate_client(
        self,
        client_identity: Identity,
        shared_secret: bytes,
        password: str,
        timestamp: int,
        sync_since: int = None,
        target_identity_hash: int = None,
        target_identity_name: str = None,
        target_identity_config: dict = None,
    ) -> tuple[bool, int]:
        with self._lock:
            self._retry_failed_load_locked()
            return self._authenticate_client_locked(
                client_identity,
                shared_secret,
                password,
                timestamp,
                sync_since=sync_since,
                target_identity_name=target_identity_name,
                target_identity_config=target_identity_config,
            )

    def _authenticate_client_locked(
        self,
        client_identity: Identity,
        shared_secret: bytes,
        password: str,
        timestamp: int,
        sync_since: int = None,
        target_identity_name: str = None,
        target_identity_config: dict = None,
    ) -> tuple[bool, int]:

        target_identity_config = target_identity_config or {}

        # Check for identity-specific passwords (required for room servers)
        identity_settings = target_identity_config.get("settings", {})

        # Determine if this is a room server by checking the type field
        identity_type = target_identity_config.get("type", "")
        is_room_server = identity_type == "room_server"

        # Log sync_since if provided (room server format)
        if sync_since is not None:
            logger.debug(f"Client sync_since timestamp: {sync_since}")

        if is_room_server:
            # Room servers use passwords from their settings section only
            # Empty strings are treated as "not set"
            admin_pwd = identity_settings.get("admin_password") or None
            guest_pwd = identity_settings.get("guest_password") or None

            if not admin_pwd and not guest_pwd:
                logger.error(
                    f"Room server '{target_identity_name}' has no passwords configured! Set admin_password and/or guest_password in settings."
                )
                return False, 0
        else:
            # Repeater uses global passwords from its own security section
            admin_pwd = self.admin_password
            guest_pwd = self.guest_password
            logger.debug(
                f"Repeater passwords - admin: {'SET' if admin_pwd else 'NONE'}, "
                f"guest: {'SET' if guest_pwd else 'NONE'}"
            )

        admin_pwd = admin_pwd or ""
        guest_pwd = guest_pwd or ""

        if target_identity_name:
            logger.debug(
                f"Authenticating for identity '{target_identity_name}' (room_server={is_room_server})"
            )

        pub_key = client_identity.get_public_key()[:PUB_KEY_SIZE]

        # Whoever a newcomer displaces stays until the login is accepted.
        evicted = []
        if not password:
            is_new = pub_key not in self.clients
            client = self.clients.get(pub_key)
            if client is None:
                # Firmware simple_repeater compares the blank password with
                # guest_password, so an empty guest password admits anyone as a
                # guest. Room servers keep requiring allow_read_only.
                open_guest = not is_room_server and not guest_pwd
                if not (self.allow_read_only or open_guest):
                    logger.info("Blank password, sender not in ACL and read-only disabled")
                    return False, 0
                client = self._put_client(client_identity, evicted, keep_grants=True)
                if client is None:
                    return False, 0
                client.permissions = PERM_ACL_GUEST
                if self.allow_read_only:
                    logger.info("Blank password, allowing read-only guest access")
                else:
                    logger.info("Blank password, no guest password set: allowing guest access")
            else:
                # Firmware skips the replay check and the session touch on this
                # path. We keep both: a replayed blank-password login from a
                # persisted admin must not be accepted.
                logger.info(f"ACL-based login for {pub_key[:6].hex()}...")

            if self._is_replay(client, timestamp):
                self._undo_put(pub_key, is_new, evicted)
                return False, 0
            self._commit_evictions(evicted)
            self._touch_client_session(client, shared_secret, timestamp, sync_since=sync_since)
            self._record_login(bytes(pub_key), client)
            # No role normalisation needed: PERM_ACL_GUEST *is* role 0, so a
            # client stored with no role bits already reads back as a guest.
            return True, client.permissions

        permissions = 0
        logger.debug(f"Comparing password (len={len(password)}) against admin/guest")
        logger.debug(
            f"Admin pwd len={len(admin_pwd) if admin_pwd else 0}, Guest pwd len={len(guest_pwd) if guest_pwd else 0}"
        )
        if admin_pwd and password == admin_pwd:
            permissions = PERM_ACL_ADMIN
            logger.info(f"Admin password validated for '{target_identity_name or 'unknown'}'")
        elif guest_pwd and password == guest_pwd:
            # Firmware splits the guest password by server type. simple_repeater
            # grants GUEST (may fetch base telemetry, may not change settings);
            # simple_room_server grants READ_WRITE (may post and read messages).
            permissions = PERM_ACL_READ_WRITE if is_room_server else PERM_ACL_GUEST
            logger.info(
                f"Guest password validated for '{target_identity_name or 'unknown'}' "
                f"(role={role_name(permissions)})"
            )
        else:
            logger.info(f"Invalid password for '{target_identity_name or 'unknown'}'")
            return False, 0

        is_new = pub_key not in self.clients
        client = self._put_client(
            client_identity, evicted, admin_grant=permissions == PERM_ACL_ADMIN
        )
        if client is None:
            return False, 0
        if is_new:
            logger.info(f"Added new client {pub_key[:6].hex()}...")

        if self._is_replay(client, timestamp):
            self._undo_put(pub_key, is_new, evicted)
            return False, 0
        self._touch_client_session(client, shared_secret, timestamp, sync_since=sync_since)
        client.permissions &= ~PERM_ACL_ROLE_MASK
        client.permissions |= permissions
        # Firmware saves after any non-guest password login. _sync_entry writes
        # only when the stored permissions changed, and a guest has none to store.
        # A failed write does not fail the login: the session is valid, and the
        # grant is written by the next change that succeeds. Whoever it evicted
        # is then left stored, so a restart cannot lose both grants.
        try:
            self._sync_entry(pub_key)
        except ACLStoreError:
            self._defer_evictions(evicted, bytes(pub_key))
        else:
            self._commit_evictions(evicted, bytes(pub_key))
        self._record_login(bytes(pub_key), client)

        logger.info(f"Login success! Role: {client.role_name()}")
        return True, client.permissions

    def get_client(self, pub_key: bytes) -> Optional[ClientInfo]:
        return self.clients.get(pub_key[:PUB_KEY_SIZE])

    def get_num_clients(self) -> int:
        return len(self.clients)

    def get_all_clients(self):
        with self._lock:
            return list(self.clients.values())

    def remove_client(self, pub_key: bytes) -> bool:
        """Remove an entry, and its stored copy. Raises ACLStoreError, restoring it, on failure."""
        key = pub_key[:PUB_KEY_SIZE]
        with self._lock:
            self._retry_failed_load_locked()
            removed = self.clients.pop(key, None)
            if removed is None:
                return False
            try:
                self._sync_entry(key)
            except ACLStoreError:
                self.clients[key] = removed
                raise
            return True
