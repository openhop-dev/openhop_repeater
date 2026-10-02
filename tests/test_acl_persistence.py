"""Persistent ACL entries and ``setperm`` / ``get acl``, against firmware ClientACL.

Firmware keeps its ACL in ``/s_contacts``: an admin provisioned over serial with
``setperm <pubkey> 3`` logs in with a blank password forever after, across
reboots. These tests hold the repeater to that.
"""

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openhop_core import LocalIdentity
from openhop_core.protocol import Identity

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.handler_helpers.acl import (
    ACL,
    ACLStoreError,
    PERM_ACL_ADMIN,
    PERM_ACL_GUEST,
    PERM_ACL_READ_ONLY,
    PERM_ACL_READ_WRITE,
    acl_identity_label,
)
from repeater.handler_helpers.login import LoginHelper
from repeater.handler_helpers.mesh_cli import MeshCLI

ROOM_CFG = {
    "type": "room_server",
    "settings": {"admin_password": "roomadmin", "guest_password": "roomguest"},
}


def _cli(acl):
    cfg = {"repeater": {"security": {}}, "mesh": {}}
    mgr = SimpleNamespace(save_to_file=MagicMock(return_value=True), live_update_daemon=MagicMock())
    return MeshCLI("/tmp/cfg.yaml", cfg, mgr, acl=acl)


def _repeater_acl(db, local, **kwargs):
    kwargs.setdefault("max_clients", 10)
    return ACL(
        admin_password="adminpw",
        guest_password="guestpw",
        allow_read_only=True,
        store=db,
        local_identity=local,
        identity_label="repeater",
        adopt_by_label=True,
        **kwargs,
    )


def _room_acl(db, local, name="room-a"):
    return ACL(
        max_clients=10,
        store=db,
        local_identity=local,
        identity_label=acl_identity_label(name, "room_server"),
        persist_filter=lambda c: c.is_admin(),
    )


def _login(acl, client, password, timestamp, config=None):
    return acl.authenticate_client(
        client_identity=Identity(client.get_public_key()),
        shared_secret=b"s" * 32,
        password=password,
        timestamp=timestamp,
        target_identity_config=config,
    )


def _acl_rows(db):
    with db._connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM acl_entries").fetchall()]


@pytest.fixture
def db(tmp_path):
    return SQLiteHandler(tmp_path)


# ---------------------------------------------------------------------------
# setperm parsing and replies (firmware simple_repeater handleCommand)
# ---------------------------------------------------------------------------

FULL = LocalIdentity().get_public_key().hex()


@pytest.mark.parametrize(
    "command, reply",
    [
        ("setperm " + FULL, "Err - bad params"),  # no separator
        ("setperm zz" + FULL[2:] + " 3", "Err - bad pubkey"),  # not hex
        ("setperm abc 3", "Err - bad pubkey"),  # odd length
        ("setperm " + FULL + "ab 3", "Err - bad pubkey"),  # longer than a key
        ("setperm " + FULL[:4] + " 3", "Err - invalid params"),  # a role needs the full key
        ("setperm " + FULL[:4] + " 0", "Err - invalid params"),  # delete, but no match
        ("setperm " + "ab" * 32 + " 3", "Err - invalid params"),  # not an ed25519 key
        ("setperm " + FULL + " 3", "OK"),
    ],
)
def test_setperm_replies_match_firmware(command, reply):
    assert _cli(ACL())._cmd_setperm(command) == reply


def test_setperm_full_key_creates_an_entry_with_the_whole_byte():
    acl = ACL()
    cli = _cli(acl)

    assert cli._cmd_setperm("setperm " + FULL + " 3") == "OK"
    assert acl.get_client(bytes.fromhex(FULL)).permissions == PERM_ACL_ADMIN

    # Firmware stores the whole byte, not just the role bits.
    assert cli._cmd_setperm("setperm " + FULL + " 129") == "OK"
    client = acl.get_client(bytes.fromhex(FULL))
    assert client.permissions == 0x81
    assert client.permissions & 3 == PERM_ACL_READ_ONLY


def test_setperm_guest_role_deletes_by_prefix():
    acl = ACL()
    cli = _cli(acl)
    cli._cmd_setperm("setperm " + FULL + " 3")

    assert cli._cmd_setperm("setperm " + FULL[:4] + " 0") == "OK"
    assert acl.get_num_clients() == 0
    assert cli._cmd_setperm("setperm " + FULL[:4] + " 0") == "Err - invalid params"


def test_setperm_parses_perms_like_atoi():
    acl = ACL()
    cli = _cli(acl)
    cli._cmd_setperm("setperm " + FULL + " 2")

    # atoi("abc") is 0, the guest role: a delete, not an error.
    assert cli._cmd_setperm("setperm " + FULL + " abc") == "OK"
    assert acl.get_num_clients() == 0

    # atoi stops at the first non-digit; -1 wraps to 0xFF, an admin role.
    assert cli._cmd_setperm("setperm " + FULL + " 2x") == "OK"
    assert acl.get_client(bytes.fromhex(FULL)).permissions == PERM_ACL_READ_WRITE
    assert cli._cmd_setperm("setperm " + FULL + " -1") == "OK"
    assert acl.get_client(bytes.fromhex(FULL)).permissions == 0xFF


def test_setperm_with_an_empty_key_does_not_delete_anyone():
    # Firmware matches an empty prefix against its first entry; we refuse.
    acl = ACL()
    cli = _cli(acl)
    cli._cmd_setperm("setperm " + FULL + " 3")

    assert cli._cmd_setperm("setperm  0") == "Err - invalid params"
    assert acl.get_num_clients() == 1


def test_setperm_through_handle_command_echoes_the_prefix():
    cli = _cli(ACL())
    assert cli.handle_command(b"x", "07|setperm " + FULL + " 3", is_admin=True) == "07|OK"
    assert cli.handle_command(b"x", "setperm " + FULL + " 3", is_admin=False).startswith("Error:")


def test_setperm_without_an_acl_is_an_error_not_a_crash():
    assert _cli(None)._cmd_setperm("setperm " + FULL + " 3") == "Err - invalid params"


# ---------------------------------------------------------------------------
# get acl: the local console only
# ---------------------------------------------------------------------------


def test_get_acl_lists_entries_with_permissions_on_the_local_console_only():
    acl = ACL(allow_read_only=True)
    cli = _cli(acl)
    cli._cmd_setperm("setperm " + FULL + " 3")
    # A blank-password guest has permissions 0 and is left out, as in firmware.
    guest = LocalIdentity()
    _login(acl, guest, "", 1)

    assert cli.handle_command(b"x", "get acl", is_admin=True, local=True) == (
        "ACL:\n03 " + FULL.upper()
    )
    assert cli.handle_command(b"x", "get acl", is_admin=True) == (
        "Error: Use 'get acl' via serial console only"
    )


def test_get_acl_is_not_swallowed_by_get():
    cli = _cli(ACL())
    assert cli.handle_command(b"x", "get acl", is_admin=True, local=True) == "ACL:"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_setperm_admin_survives_a_restart_and_logs_in_with_a_blank_password(db):
    local = LocalIdentity()
    admin = LocalIdentity()

    before = _repeater_acl(db, local)
    assert _cli(before)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3") == "OK"

    after = _repeater_acl(db, local)
    assert after.load() == 1
    loaded = after.get_client(admin.get_public_key())
    # Known but not active until it logs in, as firmware after a reboot.
    assert loaded.last_activity == 0
    assert loaded.last_timestamp == 0
    # The shared secret is derived, never stored.
    assert loaded.shared_secret == Identity(admin.get_public_key()).calc_shared_secret(
        local.get_private_key()
    )

    ok, perms = _login(after, admin, "", 100)
    assert (ok, perms) == (True, PERM_ACL_ADMIN)
    # Our blank-password path keeps the replay check firmware lacks.
    assert _login(after, admin, "", 100) == (False, 0)


def test_password_admin_login_persists_but_a_guest_login_does_not(db):
    local = LocalIdentity()
    admin, guest, reader = LocalIdentity(), LocalIdentity(), LocalIdentity()

    acl = _repeater_acl(db, local)
    assert _login(acl, admin, "adminpw", 1) == (True, PERM_ACL_ADMIN)
    assert _login(acl, guest, "guestpw", 1) == (True, PERM_ACL_GUEST)
    assert _login(acl, reader, "", 1) == (True, PERM_ACL_GUEST)

    reloaded = _repeater_acl(db, local)
    assert reloaded.load() == 1
    assert reloaded.get_client(admin.get_public_key()) is not None


def test_a_returning_admin_login_does_not_rewrite_its_entry(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    _login(acl, admin, "adminpw", 1)

    db.upsert_acl_entry = MagicMock(wraps=db.upsert_acl_entry)
    _login(acl, admin, "adminpw", 2)
    _login(acl, admin, "", 3)
    db.upsert_acl_entry.assert_not_called()


def test_a_stored_entrys_last_login_survives_a_restart(db):
    local = LocalIdentity()
    admin, guest = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    reloaded = _repeater_acl(db, local)
    reloaded.load()
    # Provisioned but never logged in.
    assert reloaded.get_client(admin.get_public_key()).last_login_success == 0

    assert _login(reloaded, admin, "", 5) == (True, PERM_ACL_ADMIN)
    logged_in_at = reloaded.get_client(admin.get_public_key()).last_login_success
    assert logged_in_at > 0
    # A guest is not stored, so its login writes nothing.
    _login(reloaded, guest, "guestpw", 5)
    assert len(_acl_rows(db)) == 1

    restarted = _repeater_acl(db, local)
    restarted.load()
    loaded = restarted.get_client(admin.get_public_key())
    assert loaded.last_login_success == logged_in_at
    # Still not active until it logs in again.
    assert loaded.last_activity == 0


def test_a_password_login_that_creates_the_entry_stores_its_login_time(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    _login(_repeater_acl(db, local), admin, "adminpw", 1)

    (row,) = _acl_rows(db)
    assert row["last_login"] and row["last_login"] > 0


def test_a_rekeyed_identity_keeps_its_entries_last_login(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room", old_key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("room")
    _login(live, admin, "roomadmin", 1, config=ROOM_CFG)
    logged_in_at = live.get_client(admin.get_public_key()).last_login_success

    helper.move_room_acl(
        "room", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "room"
    )

    restarted = _room_acl(db, new_key, "room")
    restarted.load()
    assert restarted.get_client(admin.get_public_key()).last_login_success == logged_in_at


def test_a_failed_login_time_write_does_not_fail_the_login(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    _login(acl, admin, "adminpw", 1)

    db.touch_acl_login = MagicMock(side_effect=RuntimeError("disk gone"))
    assert _login(acl, admin, "", 2) == (True, PERM_ACL_ADMIN)
    assert _login(acl, admin, "adminpw", 3) == (True, PERM_ACL_ADMIN)


def test_a_guest_promoted_by_setperm_keeps_its_login_time(db):
    local = LocalIdentity()
    guest = LocalIdentity()
    acl = _repeater_acl(db, local)
    _login(acl, guest, "guestpw", 1)
    logged_in_at = acl.get_client(guest.get_public_key()).last_login_success

    _cli(acl)._cmd_setperm(f"setperm {guest.get_public_key().hex()} 3")

    restarted = _repeater_acl(db, local)
    restarted.load()
    assert restarted.get_client(guest.get_public_key()).last_login_success == logged_in_at


def test_a_permission_change_keeps_the_stored_login_time(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    db.touch_acl_login(local.get_public_key().hex(), admin.get_public_key().hex(), 42.0)

    restarted = _repeater_acl(db, local)
    restarted.load()
    _cli(restarted)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 7")

    assert _acl_rows(db)[0]["last_login"] == 42.0


def test_an_entry_taken_over_from_another_label_does_not_inherit_its_login_time(db):
    # A deleted room whose cleanup failed leaves a row; a new room on the same
    # key granting the same client must not show the old room's login.
    local = LocalIdentity()
    admin = LocalIdentity()
    db.upsert_acl_entry(
        local.get_public_key().hex(),
        "room_server:old",
        admin.get_public_key().hex(),
        PERM_ACL_ADMIN,
        last_login=42.0,
    )

    room = _room_acl(db, local, "new")
    room.load()
    _cli(room)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    (row,) = _acl_rows(db)
    assert row["identity_label"] == "room_server:new"
    assert row["last_login"] is None
    restarted = _room_acl(db, local, "new")
    restarted.load()
    assert restarted.get_client(admin.get_public_key()).last_login_success == 0


def test_a_login_in_the_same_second_as_the_stored_one_does_not_write(db, monkeypatch):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    _login(acl, admin, "adminpw", 1)

    db.touch_acl_login = MagicMock(wraps=db.touch_acl_login)
    db.upsert_acl_entry = MagicMock(wraps=db.upsert_acl_entry)
    _login(acl, admin, "", 2)
    _login(acl, admin, "adminpw", 3)
    db.touch_acl_login.assert_not_called()
    db.upsert_acl_entry.assert_not_called()

    monkeypatch.setattr(time, "time", lambda: 1001.0)
    _login(acl, admin, "", 4)
    db.touch_acl_login.assert_called_once()


def test_a_failed_login_time_write_is_retried_by_the_next_login(db, monkeypatch):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    _login(acl, admin, "adminpw", 1)

    monkeypatch.setattr(time, "time", lambda: 1001.0)
    real_touch = db.touch_acl_login
    db.touch_acl_login = MagicMock(side_effect=RuntimeError("database is locked"))
    _login(acl, admin, "", 2)
    db.touch_acl_login = real_touch
    _login(acl, admin, "", 3)

    assert _acl_rows(db)[0]["last_login"] == 1001.0


def test_a_login_during_a_failed_load_is_stored_once_the_load_recovers(db, monkeypatch):
    local = LocalIdentity()
    admin = LocalIdentity()
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    _login(_repeater_acl(db, local), admin, "adminpw", 1)

    acl = _repeater_acl(db, local)
    real_load = db.load_acl_entries
    db.load_acl_entries = MagicMock(side_effect=RuntimeError("database is locked"))
    acl.load()
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    # The guest password: the stored admin grant is unknown until the load works.
    assert _login(acl, admin, "guestpw", 2) == (True, PERM_ACL_GUEST)

    db.load_acl_entries = real_load
    acl.load()

    restarted = _repeater_acl(db, local)
    restarted.load()
    assert restarted.get_client(admin.get_public_key()).last_login_success == 2000


def test_a_recovered_load_does_not_erase_the_login_time_of_an_entry_granted_meanwhile(db):
    local = LocalIdentity()
    admin, other = LocalIdentity(), LocalIdentity()
    _cli(_repeater_acl(db, local))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    db.touch_acl_login(local.get_public_key().hex(), admin.get_public_key().hex(), 5000.0)

    acl = _repeater_acl(db, local)
    real_load = db.load_acl_entries
    db.load_acl_entries = MagicMock(side_effect=RuntimeError("database is locked"))
    acl.load()
    # A setperm while the stored ACL is unreadable: a live entry that never logged in.
    assert acl.apply_permissions(admin.get_public_key(), PERM_ACL_ADMIN)
    assert acl.get_client(admin.get_public_key()).last_login_success == 0

    db.load_acl_entries = real_load
    # Any later change retries the load.
    assert acl.apply_permissions(other.get_public_key(), PERM_ACL_ADMIN)

    assert acl.get_client(admin.get_public_key()).last_login_success == 5000
    restarted = _repeater_acl(db, local)
    restarted.load()
    assert restarted.get_client(admin.get_public_key()).last_login_success == 5000


def test_a_renamed_room_keeps_its_entries_last_login(db):
    key = LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("old", key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("old")
    _login(live, admin, "roomadmin", 1, config=ROOM_CFG)
    logged_in_at = live.get_client(admin.get_public_key()).last_login_success

    helper.move_room_acl("old", key.get_public_key().hex(), key.get_public_key().hex(), "new")

    restarted = _room_acl(db, key, "new")
    restarted.load()
    assert restarted.get_client(admin.get_public_key()).last_login_success == logged_in_at


def test_a_rekeyed_repeater_adopts_its_entries_last_login(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    before = _repeater_acl(db, old_key)
    _login(before, admin, "adminpw", 1)
    logged_in_at = before.get_client(admin.get_public_key()).last_login_success

    rotated = _repeater_acl(db, new_key)
    rotated.load()
    assert rotated.get_client(admin.get_public_key()).last_login_success == logged_in_at


def test_loading_an_acl_writes_nothing(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    _login(_repeater_acl(db, local), admin, "adminpw", 1)

    db.touch_acl_login = MagicMock(wraps=db.touch_acl_login)
    db.upsert_acl_entry = MagicMock(wraps=db.upsert_acl_entry)
    acl = _repeater_acl(db, local)
    acl.load()
    acl.load()
    db.touch_acl_login.assert_not_called()
    db.upsert_acl_entry.assert_not_called()


def test_an_acl_table_from_before_last_login_is_migrated(tmp_path):
    db = SQLiteHandler(tmp_path)
    admin = LocalIdentity().get_public_key().hex()
    with db._connect() as conn:
        conn.execute("DROP TABLE acl_entries")
        conn.execute(
            "CREATE TABLE acl_entries (identity_pubkey TEXT NOT NULL, identity_label TEXT NOT NULL, "
            "client_pubkey TEXT NOT NULL, permissions INTEGER NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (identity_pubkey, client_pubkey))"
        )
        conn.execute(
            "INSERT INTO acl_entries VALUES ('aa', 'repeater', ?, 3, 0)",
            (admin,),
        )

    migrated = SQLiteHandler(tmp_path)
    (row,) = migrated.load_acl_entries("aa", identity_label="repeater")
    assert row["last_login"] is None
    migrated.touch_acl_login("aa", admin, 42.0)
    assert migrated.load_acl_entries("aa", identity_label="repeater")[0]["last_login"] == 42.0


def test_room_server_persists_admins_only(db):
    local = LocalIdentity()
    admin, writer = LocalIdentity(), LocalIdentity()

    acl = _room_acl(db, local)
    assert _login(acl, admin, "roomadmin", 1, ROOM_CFG) == (True, PERM_ACL_ADMIN)
    assert _login(acl, writer, "roomguest", 1, ROOM_CFG) == (True, PERM_ACL_READ_WRITE)

    reloaded = _room_acl(db, local)
    assert reloaded.load() == 1
    assert reloaded.get_client(admin.get_public_key()) is not None
    assert reloaded.get_client(writer.get_public_key()) is None


def test_demoting_a_room_admin_drops_its_stored_entry(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _room_acl(db, local)
    _login(acl, admin, "roomadmin", 1, ROOM_CFG)
    # Same key logs in with the room password: now read-write, not an admin.
    _login(acl, admin, "roomguest", 2, ROOM_CFG)

    assert _room_acl(db, local).load() == 0


def test_remove_client_deletes_the_stored_entry(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    _login(acl, admin, "adminpw", 1)

    assert acl.remove_client(admin.get_public_key()) is True
    assert _repeater_acl(db, local).load() == 0


def test_setperm_delete_removes_the_stored_entry(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    cli = _cli(acl)
    cli._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    cli._cmd_setperm(f"setperm {admin.get_public_key()[:4].hex()} 0")

    assert _repeater_acl(db, local).load() == 0


def test_identities_do_not_share_entries_even_when_their_hash_bytes_collide(db):
    first = LocalIdentity()
    second = LocalIdentity()
    while second.get_public_key()[0] != first.get_public_key()[0]:
        second = LocalIdentity()
    admin = LocalIdentity()

    _cli(_repeater_acl(db, first))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert _room_acl(db, second).load() == 0
    assert _repeater_acl(db, first).load() == 1


def test_a_new_identity_key_keeps_the_acl(db):
    # Firmware's ACL survives a new private key; it recomputes secrets on load.
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    _cli(_repeater_acl(db, old_key))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    rotated = _repeater_acl(db, new_key)
    assert rotated.load() == 1
    assert rotated.get_client(admin.get_public_key()).shared_secret == Identity(
        admin.get_public_key()
    ).calc_shared_secret(new_key.get_private_key())
    # The rows moved; the old key has none left.
    assert [r["identity_pubkey"] for r in _acl_rows(db)] == [new_key.get_public_key().hex()]


def test_a_room_is_never_adopted_by_label(db):
    # Room servers move their rows explicitly; a new key alone, or a new room
    # reusing a deleted room's name, starts empty.
    admin = LocalIdentity()
    _cli(_room_acl(db, LocalIdentity()))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert _room_acl(db, LocalIdentity()).load() == 0


def test_a_room_renamed_and_rekeyed_in_one_update_keeps_its_acl(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("old-name", old_key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("old-name")
    _cli(live)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    helper.move_room_acl(
        "old-name", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "new-name"
    )

    rows = _acl_rows(db)
    assert [(r["identity_pubkey"], r["identity_label"]) for r in rows] == [
        (new_key.get_public_key().hex(), "room_server:new-name")
    ]
    # The live ACL, still on the old key until a restart, now writes to the
    # moved rows rather than recreating them under the old key.
    reader = LocalIdentity()
    _cli(live)._cmd_setperm(f"setperm {reader.get_public_key().hex()} 3")
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {new_key.get_public_key().hex()}

    restarted = _room_acl(db, new_key, "new-name")
    assert restarted.load() == 2
    assert _login(restarted, admin, "", 1) == (True, PERM_ACL_ADMIN)


def test_moving_onto_a_key_that_has_entries_is_refused(db):
    # Those entries belong to another identity; merging would hand them over.
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin, other = LocalIdentity(), LocalIdentity()
    _cli(_room_acl(db, old_key))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    _cli(_room_acl(db, new_key, "room-b"))._cmd_setperm(f"setperm {other.get_public_key().hex()} 3")

    with pytest.raises(ValueError):
        db.copy_acl_identity(
            old_key.get_public_key().hex(),
            new_key.get_public_key().hex(),
            "room_server:room-a",
            "room_server:room-a",
        )
    assert sorted(r["identity_pubkey"] for r in _acl_rows(db)) == sorted(
        [old_key.get_public_key().hex(), new_key.get_public_key().hex()]
    )


def test_a_move_racing_a_grant_does_not_strand_it_under_the_old_key(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", old_key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("room-a")
    keys = [LocalIdentity().get_public_key() for _ in range(20)]

    def grant():
        for key in keys:
            live.apply_permissions(key, PERM_ACL_ADMIN)

    thread = threading.Thread(target=grant)
    thread.start()
    helper.move_room_acl(
        "room-a", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "room-a"
    )
    thread.join()

    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {new_key.get_public_key().hex()}
    assert len(_acl_rows(db)) == 20


def test_a_deleted_identity_does_not_write_its_acl_back(db):
    # Its handlers stay registered until a restart; a straggler login there
    # must not recreate the rows the delete removed.
    local = LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_by_name("room-a")
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert helper.forget_identity_acl("room-a", local.get_public_key().hex()) == 1
    _login(acl, LocalIdentity(), "roomadmin", 1, ROOM_CFG)
    _cli(acl)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")
    acl.remove_client(admin.get_public_key())

    assert _acl_rows(db) == []


def test_deleting_an_identity_drops_its_acl(db):
    key = LocalIdentity()
    admin = LocalIdentity()
    _cli(_room_acl(db, key))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert db.delete_acl_identity(key.get_public_key().hex()) == 1
    assert _room_acl(db, key).load() == 0


def test_a_room_setperm_read_write_entry_is_not_stored(db):
    # Firmware's room server saves admins only.
    local = LocalIdentity()
    writer = LocalIdentity()
    acl = _room_acl(db, local)
    assert _cli(acl)._cmd_setperm(f"setperm {writer.get_public_key().hex()} 2") == "OK"
    assert acl.get_client(writer.get_public_key()) is not None
    assert acl.is_persisted(writer.get_public_key()) is False
    assert _room_acl(db, local).load() == 0


# ---------------------------------------------------------------------------
# Store failures
# ---------------------------------------------------------------------------


class _FailingStore:
    """A store whose writes fail, with a working read."""

    def __init__(self, rows=()):
        self.rows = list(rows)

    def load_acl_entries(self, identity_pubkey, identity_label=None, adopt_label=None):
        return list(self.rows)

    def upsert_acl_entry(self, *args, **kwargs):
        raise RuntimeError("disk full")

    def delete_acl_entry(self, *args):
        raise RuntimeError("disk full")


def test_setperm_reports_a_failed_write_and_leaves_the_table_unchanged():
    acl = ACL(store=_FailingStore(), local_identity=LocalIdentity(), identity_label="repeater")
    admin = LocalIdentity()

    reply = _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    assert reply == "Err - failed to save"
    assert acl.get_num_clients() == 0


def test_a_failed_delete_keeps_the_entry(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    acl._store = _FailingStore()

    assert _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 0") == (
        "Err - failed to save"
    )
    with pytest.raises(ACLStoreError):
        acl.remove_client(admin.get_public_key())
    assert acl.get_client(admin.get_public_key()) is not None


def test_a_failed_write_does_not_fail_a_password_login():
    acl = ACL(
        admin_password="adminpw",
        store=_FailingStore(),
        local_identity=LocalIdentity(),
        identity_label="repeater",
    )
    assert _login(acl, LocalIdentity(), "adminpw", 1) == (True, PERM_ACL_ADMIN)


def test_concurrent_grant_and_removal_leave_memory_and_store_in_agreement(db):
    # A removal racing a grant must not leave the grant stored but not listed.
    local = LocalIdentity()
    keys = [LocalIdentity().get_public_key() for _ in range(8)]
    acl = _repeater_acl(db, local, max_clients=64)

    def churn(key):
        for _ in range(20):
            acl.apply_permissions(key, PERM_ACL_ADMIN)
            acl.remove_client(key)
            acl.apply_permissions(key, PERM_ACL_ADMIN)

    threads = [threading.Thread(target=churn, args=(k,)) for k in keys for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stored = {bytes.fromhex(r["client_pubkey"]) for r in _acl_rows(db)}
    assert stored == set(acl.clients)


def test_stored_entries_past_max_clients_all_load():
    # Lowering max_clients must not revoke grants that are already stored.
    rows = [
        {"client_pubkey": LocalIdentity().get_public_key().hex(), "permissions": 3}
        for _ in range(4)
    ]
    acl = ACL(max_clients=2, store=_FailingStore(rows), local_identity=LocalIdentity())
    assert acl.load() == 4
    # Full of admins: a blank-password newcomer is refused, not given a place.
    assert _login(acl, LocalIdentity(), "", 1) == (False, 0)
    assert acl.get_num_clients() == 4


def test_a_failed_store_read_is_reported_not_shown_as_empty():
    store = SimpleNamespace(load_acl_entries=MagicMock(side_effect=RuntimeError("db gone")))
    acl = ACL(store=store, local_identity=LocalIdentity(), identity_label="repeater")
    assert acl.load() == 0
    assert acl.load_error == "db gone"


# ---------------------------------------------------------------------------
# A full table (firmware putClient)
# ---------------------------------------------------------------------------


def test_a_full_table_evicts_the_least_active_non_admin():
    acl = ACL(max_clients=3, admin_password="adminpw", allow_read_only=True)
    admin, old_guest, new_guest, newcomer = (LocalIdentity() for _ in range(4))
    _login(acl, admin, "adminpw", 1)
    _login(acl, old_guest, "", 1)
    _login(acl, new_guest, "", 1)
    acl.get_client(admin.get_public_key()).last_activity = 0  # admins are never picked
    acl.get_client(old_guest.get_public_key()).last_activity = 10
    acl.get_client(new_guest.get_public_key()).last_activity = 20

    assert _login(acl, newcomer, "", 1) == (True, PERM_ACL_GUEST)
    assert acl.get_client(old_guest.get_public_key()) is None
    assert acl.get_client(admin.get_public_key()) is not None
    assert acl.get_num_clients() == 3


def test_a_table_full_of_admins_evicts_the_least_recently_seen():
    """Firmware evicts its last slot when every entry is an admin; here the
    admin seen least recently goes, and a newcomer is never locked out."""
    acl = ACL(max_clients=2, admin_password="adminpw", allow_read_only=True)
    cli = _cli(acl)
    stale, recent, newcomer = (LocalIdentity() for _ in range(3))
    cli._cmd_setperm(f"setperm {stale.get_public_key().hex()} 3")
    cli._cmd_setperm(f"setperm {recent.get_public_key().hex()} 3")
    acl.get_client(stale.get_public_key()).last_activity = 10
    acl.get_client(recent.get_public_key()).last_activity = 20

    assert _login(acl, newcomer, "adminpw", 1) == (True, PERM_ACL_ADMIN)
    assert acl.get_client(stale.get_public_key()) is None
    assert acl.get_client(recent.get_public_key()) is not None
    assert acl.get_num_clients() == 2


def test_after_a_restart_the_admin_with_the_oldest_login_is_evicted(db):
    # Loaded entries have no activity yet, so the stored last login decides,
    # whatever order the store returns them in.
    local = LocalIdentity()
    admins = [LocalIdentity() for _ in range(3)]
    acl = _repeater_acl(db, local, max_clients=3)
    for admin in admins:
        _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    for admin, when in zip(admins, (300, 100, 200)):
        db.touch_acl_login(local.get_public_key().hex(), admin.get_public_key().hex(), when)

    restarted = _repeater_acl(db, local, max_clients=3)
    assert restarted.load() == 3
    _cli(restarted)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")

    assert restarted.get_client(admins[1].get_public_key()) is None
    assert restarted.get_client(admins[0].get_public_key()) is not None
    assert restarted.get_client(admins[2].get_public_key()) is not None


def test_evicting_a_stored_admin_deletes_it(db):
    local = LocalIdentity()
    admin, newcomer = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    _cli(acl)._cmd_setperm(f"setperm {newcomer.get_public_key().hex()} 3")

    stored = _repeater_acl(db, local)
    assert stored.load() == 1
    assert stored.get_client(newcomer.get_public_key()) is not None


def test_a_failed_grant_puts_an_evicted_admin_back(db):
    local = LocalIdentity()
    admin, newcomer = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))

    assert _cli(acl)._cmd_setperm(f"setperm {newcomer.get_public_key().hex()} 3") == (
        "Err - failed to save"
    )
    assert acl.get_client(admin.get_public_key()) is not None
    assert _repeater_acl(db, local).load() == 1


def test_evicting_a_stored_entry_deletes_it(db):
    local = LocalIdentity()
    reader, newcomer = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {reader.get_public_key().hex()} 1")
    _login(acl, newcomer, "guestpw", 1)

    assert _repeater_acl(db, local).load() == 0


def test_a_blank_password_newcomer_cannot_evict_a_stored_grant(db):
    local = LocalIdentity()
    reader, newcomer = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {reader.get_public_key().hex()} 1")

    assert _login(acl, newcomer, "", 1) == (False, 0)
    assert acl.get_client(reader.get_public_key()) is not None
    assert _repeater_acl(db, local).load() == 1


def test_a_blank_password_newcomer_evicts_a_guest_session_not_a_stored_grant(db):
    local = LocalIdentity()
    reader, guest, newcomer = LocalIdentity(), LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=2)
    _cli(acl)._cmd_setperm(f"setperm {reader.get_public_key().hex()} 1")
    assert _login(acl, guest, "", 1)[0] is True

    assert _login(acl, newcomer, "", 2)[0] is True
    assert acl.get_client(guest.get_public_key()) is None
    assert acl.get_client(reader.get_public_key()) is not None
    assert _repeater_acl(db, local).load() == 1


def test_a_failed_grant_on_a_full_table_does_not_evict_anyone(db):
    local = LocalIdentity()
    reader, newcomer = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {reader.get_public_key().hex()} 1")
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))

    assert _cli(acl)._cmd_setperm(f"setperm {newcomer.get_public_key().hex()} 3") == (
        "Err - failed to save"
    )
    assert acl.get_client(reader.get_public_key()) is not None
    assert acl.get_client(newcomer.get_public_key()) is None
    assert _repeater_acl(db, local).load() == 1


# ---------------------------------------------------------------------------
# LoginHelper wiring
# ---------------------------------------------------------------------------


def _login_helper(db):
    return LoginHelper(identity_manager=MagicMock(), packet_injector=AsyncMock(), sqlite_handler=db)


def test_login_helper_loads_the_stored_acl_at_registration(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    config = {"repeater": {"security": {"admin_password": "adminpw"}}}

    first = _login_helper(db)
    first.register_identity("repeater", local, identity_type="repeater", config=config)
    acl = first.get_acl_for_identity(local.get_public_key()[0])
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    second = _login_helper(db)
    second.register_identity("repeater", local, identity_type="repeater", config=config)
    reloaded = second.get_acl_for_identity(local.get_public_key()[0])
    assert _login(reloaded, admin, "", 5) == (True, PERM_ACL_ADMIN)


def test_a_room_config_without_a_type_still_authenticates_as_a_room(db):
    """The registered type decides; a room must not fall into the repeater rules."""
    local, client = LocalIdentity(), LocalIdentity()
    settings = {"admin_password": "roomadmin", "allow_read_only": False}
    config = {"name": "room-a", "settings": settings}
    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=config)
    authenticate = helper.handlers[local.get_public_key()[0]].login_handler.authenticate

    assert authenticate(Identity(client.get_public_key()), b"s" * 32, "", 1) == (False, 0)
    assert "type" not in config


def test_re_registering_an_identity_keeps_its_live_sessions(db):
    # A hot reload (a rename, say) must not zero live sessions: the room sync
    # loop skips clients with no activity, and the replay watermark would reset.
    local = LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_by_name("room-a")
    _login(acl, admin, "roomadmin", 10, ROOM_CFG)

    helper.register_identity("room-b", local, identity_type="room_server", config=ROOM_CFG)

    assert helper.get_acl_by_name("room-b") is acl
    assert helper.get_acl_by_name("room-a") is None
    client = acl.get_client(admin.get_public_key())
    assert client.last_activity != 0
    assert _login(acl, admin, "", 10) == (False, 0)  # still a replay


def test_a_rekeyed_identity_gets_a_fresh_acl_on_its_new_key(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    while new_key.get_public_key()[0] != old_key.get_public_key()[0]:
        new_key = LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", old_key, identity_type="room_server", config=ROOM_CFG)
    _cli(helper.get_acl_by_name("room-a"))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    helper.move_room_acl(
        "room-a", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "room-a"
    )

    # Same hash byte, different key: not reused, so secrets use the new key.
    helper.register_identity("room-a", new_key, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_by_name("room-a")
    assert acl.identity_pubkey_hex == new_key.get_public_key().hex()
    assert acl.get_client(admin.get_public_key()).shared_secret == Identity(
        admin.get_public_key()
    ).calc_shared_secret(new_key.get_private_key())


def test_login_helper_room_acl_keeps_admins_only(db):
    local = LocalIdentity()
    admin, writer = LocalIdentity(), LocalIdentity()

    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_for_identity(local.get_public_key()[0])
    _login(acl, admin, "roomadmin", 1, ROOM_CFG)
    _login(acl, writer, "roomguest", 1, ROOM_CFG)

    again = _login_helper(db)
    again.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    reloaded = again.get_acl_for_identity(local.get_public_key()[0])
    assert [c.id.get_public_key() for c in reloaded.get_all_clients()] == [admin.get_public_key()]


# ---------------------------------------------------------------------------
# Room server: loaded admins are neither pushed to nor evicted
# ---------------------------------------------------------------------------


def _room_server(acl, db):
    from repeater.handler_helpers.room_server import RoomServer

    return RoomServer(
        room_hash=0x34,
        room_name="room-a",
        local_identity=LocalIdentity(),
        sqlite_handler=db,
        packet_injector=AsyncMock(return_value=True),
        acl=acl,
    )


@pytest.mark.asyncio
async def test_room_eviction_keeps_a_stored_admin():
    acl = ACL(max_clients=5, admin_password="roomadmin")
    admin, writer = LocalIdentity(), LocalIdentity()
    _login(acl, admin, "roomadmin", 1, ROOM_CFG)
    _login(acl, writer, "roomguest", 1, ROOM_CFG)

    stale = time.time() - 10_000
    db = SimpleNamespace(
        get_all_room_clients=MagicMock(
            return_value=[
                {
                    "client_pubkey": c.get_public_key().hex(),
                    "push_failures": 0,
                    "last_activity": stale,
                }
                for c in (admin, writer)
            ]
        ),
        upsert_client_sync=MagicMock(),
    )
    await _room_server(acl, db)._evict_failed_clients()

    assert acl.get_client(admin.get_public_key()) is not None
    assert acl.get_client(writer.get_public_key()) is None


@pytest.mark.asyncio
async def test_room_sync_loop_does_not_push_to_an_entry_that_has_not_logged_in(monkeypatch):
    monkeypatch.setattr("repeater.handler_helpers.room_server.secrets.randbelow", lambda n: 0)
    monkeypatch.setattr("repeater.handler_helpers.room_server.SYNC_PUSH_INTERVAL_MS", 10)
    acl = ACL(max_clients=5)
    acl.apply_permissions(LocalIdentity().get_public_key(), PERM_ACL_ADMIN)

    db = SimpleNamespace(
        get_client_sync=MagicMock(return_value=None),
        upsert_client_sync=MagicMock(),
        get_unsynced_messages=MagicMock(return_value=[]),
        get_all_room_clients=MagicMock(return_value=[]),
        cleanup_old_messages=MagicMock(return_value=0),
    )
    rs = _room_server(acl, db)
    rs.next_push_time = 0
    rs._running = True

    task = asyncio.create_task(rs._sync_loop())
    await asyncio.sleep(0.3)
    rs._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    db.get_client_sync.assert_not_called()
    db.get_unsynced_messages.assert_not_called()

    # Once it logs in it is pushed to as before.
    acl.get_all_clients()[0].last_activity = int(time.time())
    rs.next_push_time = 0  # skip the back-off after a round with no one to push to
    rs._running = True
    task = asyncio.create_task(rs._sync_loop())
    await asyncio.sleep(0.3)
    rs._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    db.get_unsynced_messages.assert_called()


# ---------------------------------------------------------------------------
# Round-3 review: moves around a commit, one ACL per identity, load retry
# ---------------------------------------------------------------------------


def test_a_failed_commit_leaves_the_entries_under_the_old_key(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", old_key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("room-a")
    _cli(live)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    def commit():
        raise OSError("config not written")

    with pytest.raises(OSError):
        helper.move_room_acl(
            "room-a",
            old_key.get_public_key().hex(),
            new_key.get_public_key().hex(),
            "room-a",
            commit,
        )
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {old_key.get_public_key().hex()}
    # The live ACL still writes where the config still points.
    _cli(live)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {old_key.get_public_key().hex()}


def test_a_failed_commit_and_cleanup_still_leave_the_old_key_whole(db):
    # The worst case: the config save fails and so does dropping the copies.
    # The config still names the old key, which still has every entry.
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    _cli(_room_acl(db, old_key))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    real_delete = db.delete_acl_identity
    db.delete_acl_identity = MagicMock(side_effect=RuntimeError("disk gone"))

    def commit():
        raise OSError("config not written")

    from repeater.handler_helpers.acl import move_identity_acl

    with pytest.raises(OSError):
        move_identity_acl(
            db,
            old_key.get_public_key().hex(),
            new_key.get_public_key().hex(),
            "room_server:room-a",
            "room_server:room-a",
            commit,
        )
    db.delete_acl_identity = real_delete
    assert _room_acl(db, old_key).load() == 1


def test_a_grant_after_a_move_lands_under_the_new_key(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", old_key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("room-a")
    helper.move_room_acl(
        "room-a", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "room-a"
    )

    _cli(live)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {new_key.get_public_key().hex()}


def test_a_rekeyed_identity_leaves_one_acl_and_moves_again_by_name(db):
    # Two rekeys without a restart: the second must move the live ACL's rows,
    # not those of the ACL the first re-registration replaced.
    keys = [LocalIdentity() for _ in range(3)]
    while keys[1].get_public_key()[0] == keys[0].get_public_key()[0]:
        keys[1] = LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", keys[0], identity_type="room_server", config=ROOM_CFG)
    first = helper.get_acl_by_name("room-a")
    _cli(first)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    helper.move_room_acl(
        "room-a", keys[0].get_public_key().hex(), keys[1].get_public_key().hex(), "room-a"
    )
    helper.register_identity("room-a", keys[1], identity_type="room_server", config=ROOM_CFG)
    second = helper.get_acl_by_name("room-a")
    assert second is not first
    assert first.detached
    assert first not in helper.acls.values()

    helper.move_room_acl(
        "room-a", keys[1].get_public_key().hex(), keys[2].get_public_key().hex(), "room-a"
    )
    _cli(second)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")
    # The replaced ACL no longer writes anywhere.
    _cli(first)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {keys[2].get_public_key().hex()}
    assert len(_acl_rows(db)) == 2


def test_a_room_deleted_and_re_added_with_its_key_persists_again(db):
    local = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    helper.forget_identity_acl("room-a", local.get_public_key().hex())

    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_by_name("room-a")
    assert not acl.detached
    admin = LocalIdentity()
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    assert acl.is_persisted(admin.get_public_key())
    assert len(_acl_rows(db)) == 1


def test_a_failed_load_is_retried_by_the_next_login(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    _cli(_repeater_acl(db, local))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    real_load = db.load_acl_entries
    db.load_acl_entries = MagicMock(side_effect=RuntimeError("database is locked"))
    acl = _repeater_acl(db, local)
    assert acl.load() == 0
    assert acl.load_error == "database is locked"

    db.load_acl_entries = real_load
    assert _login(acl, admin, "", 1) == (True, PERM_ACL_ADMIN)
    assert acl.load_error is None


def test_a_hot_reload_applies_new_settings_to_the_reused_acl(db):
    local = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", local, identity_type="room_server", config=ROOM_CFG)
    acl = helper.get_acl_by_name("room-a")
    changed = {
        "type": "room_server",
        "settings": {"admin_password": "new-admin", "guest_password": "rg", "max_clients": 7},
    }
    helper.register_identity("room-a", local, identity_type="room_server", config=changed)
    assert helper.get_acl_by_name("room-a") is acl
    assert (acl.admin_password, acl.max_clients) == ("new-admin", 7)


@pytest.mark.asyncio
async def test_room_eviction_carries_on_past_a_failed_store_write():
    acl = ACL(max_clients=5, admin_password="roomadmin")
    first, second = LocalIdentity(), LocalIdentity()
    for client in (first, second):
        _login(acl, client, "roomguest", 1, ROOM_CFG)
    calls = []

    def remove(pub_key):
        calls.append(pub_key)
        if len(calls) == 1:
            raise ACLStoreError("disk full")
        return True

    acl.remove_client = remove
    stale = time.time() - 10_000
    db = SimpleNamespace(
        get_all_room_clients=MagicMock(
            return_value=[
                {
                    "client_pubkey": c.get_public_key().hex(),
                    "push_failures": 0,
                    "last_activity": stale,
                }
                for c in (first, second)
            ]
        ),
        upsert_client_sync=MagicMock(),
    )
    await _room_server(acl, db)._evict_failed_clients()
    assert len(calls) == 2


def test_a_change_retried_after_a_failed_cleanup_goes_through(db):
    # A failed save whose cleanup also failed leaves a copy at the new key under
    # this room's label; retrying must replace it, not be refused for ever.
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    _cli(_room_acl(db, old_key))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    from repeater.handler_helpers.acl import move_identity_acl

    label = acl_identity_label("room-a", "room_server")
    real_delete = db.delete_acl_identity
    db.delete_acl_identity = MagicMock(side_effect=RuntimeError("disk full"))

    def failing_commit():
        raise OSError("config not written")

    with pytest.raises(OSError):
        move_identity_acl(
            db,
            old_key.get_public_key().hex(),
            new_key.get_public_key().hex(),
            label,
            label,
            failing_commit,
        )
    db.delete_acl_identity = real_delete

    move_identity_acl(
        db, old_key.get_public_key().hex(), new_key.get_public_key().hex(), label, label
    )
    assert {r["identity_pubkey"] for r in _acl_rows(db)} == {new_key.get_public_key().hex()}
    assert len(_acl_rows(db)) == 1


# ---------------------------------------------------------------------------
# Round-5 review (codex): rows are owned by key *and* label
# ---------------------------------------------------------------------------


def test_a_room_does_not_load_another_rooms_orphaned_rows(db):
    # room-a's admin left at key K by a failed cleanup; room-b later created on K.
    key = LocalIdentity()
    admin = LocalIdentity()
    _cli(_room_acl(db, key, "room-a"))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    room_b = _room_acl(db, key, "room-b")
    assert room_b.load() == 0
    # A blank-password login gets no stored grant (at most a read-only guest).
    assert _login(room_b, admin, "", 1)[1] == PERM_ACL_GUEST


def test_a_rekey_carries_only_the_rooms_own_rows(db):
    old_key, new_key = LocalIdentity(), LocalIdentity()
    own, orphan = LocalIdentity(), LocalIdentity()
    _cli(_room_acl(db, old_key, "room-a"))._cmd_setperm(f"setperm {own.get_public_key().hex()} 3")
    _cli(_room_acl(db, old_key, "gone"))._cmd_setperm(f"setperm {orphan.get_public_key().hex()} 3")
    from repeater.handler_helpers.acl import move_identity_acl

    label = acl_identity_label("room-a", "room_server")
    move_identity_acl(
        db, old_key.get_public_key().hex(), new_key.get_public_key().hex(), label, label
    )

    moved = _room_acl(db, new_key, "room-a")
    assert moved.load() == 1
    assert moved.get_client(own.get_public_key()) is not None
    assert moved.get_client(orphan.get_public_key()) is None


def test_a_rename_relabels_before_the_save_and_back_if_it_fails(db):
    key = LocalIdentity()
    admin = LocalIdentity()
    _cli(_room_acl(db, key, "room-a"))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    from repeater.handler_helpers.acl import move_identity_acl

    old = acl_identity_label("room-a", "room_server")
    new = acl_identity_label("room-b", "room_server")

    def failing_commit():
        raise OSError("config not written")

    with pytest.raises(OSError):
        move_identity_acl(
            db, key.get_public_key().hex(), key.get_public_key().hex(), old, new, failing_commit
        )
    assert _room_acl(db, key, "room-a").load() == 1

    move_identity_acl(db, key.get_public_key().hex(), key.get_public_key().hex(), old, new)
    assert _room_acl(db, key, "room-b").load() == 1
    assert _room_acl(db, key, "room-a").load() == 0


def test_rows_a_failed_rekey_left_behind_never_reach_a_later_room(db):
    # Codex's chain: rekey K1 -> K2 whose cleanup fails, rename A -> B, delete
    # B, create a new A on K1. The new A must not inherit the old admin.
    old_key, new_key = LocalIdentity(), LocalIdentity()
    admin = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", old_key, identity_type="room_server", config=ROOM_CFG)
    _cli(helper.get_acl_by_name("room-a"))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    real_delete, real_sweep = db.delete_acl_identity, db.delete_acl_label
    db.delete_acl_identity = MagicMock(side_effect=RuntimeError("disk full"))
    db.delete_acl_label = MagicMock(side_effect=RuntimeError("disk full"))
    helper.move_room_acl(
        "room-a", old_key.get_public_key().hex(), new_key.get_public_key().hex(), "room-a"
    )
    db.delete_acl_identity, db.delete_acl_label = real_delete, real_sweep
    assert len({r["identity_pubkey"] for r in _acl_rows(db)}) == 2  # a leftover at K1

    helper.move_room_acl(
        "room-a", new_key.get_public_key().hex(), new_key.get_public_key().hex(), "room-b"
    )
    assert {(r["identity_pubkey"], r["identity_label"]) for r in _acl_rows(db)} == {
        (new_key.get_public_key().hex(), "room_server:room-b")
    }

    helper.forget_identity_acl("room-b", new_key.get_public_key().hex())
    assert _acl_rows(db) == []
    assert _room_acl(db, old_key, "room-a").load() == 0


def test_a_room_renamed_to_a_deleted_rooms_name_does_not_inherit_its_rows(db):
    # Codex: delete B whose ACL cleanup fails, create A on B's old key,
    # rename A to B. B's leftover admin must not become the new B's.
    key = LocalIdentity()
    admin, own = LocalIdentity(), LocalIdentity()
    _cli(_room_acl(db, key, "room-b"))._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    _cli(_room_acl(db, key, "room-a"))._cmd_setperm(f"setperm {own.get_public_key().hex()} 3")
    from repeater.handler_helpers.acl import move_identity_acl

    move_identity_acl(
        db,
        key.get_public_key().hex(),
        key.get_public_key().hex(),
        acl_identity_label("room-a", "room_server"),
        acl_identity_label("room-b", "room_server"),
    )
    renamed = _room_acl(db, key, "room-b")
    assert renamed.load() == 1
    assert renamed.get_client(own.get_public_key()) is not None
    assert renamed.get_client(admin.get_public_key()) is None


def test_deleting_a_room_whose_rename_was_not_reloaded_detaches_its_live_acl(db):
    # Codex: rename A to B with the hot reload refused, so the live ACL is
    # still listed under A, then delete B. A grant through that ACL must not
    # write B's rows back.
    key = LocalIdentity()
    admin, later = LocalIdentity(), LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", key, identity_type="room_server", config=ROOM_CFG)
    live = helper.get_acl_by_name("room-a")
    _cli(live)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    key_hex = key.get_public_key().hex()
    helper.move_room_acl("room-a", key_hex, key_hex, "room-b")
    assert helper.get_acl_by_name("room-a") is live

    helper.forget_identity_acl("room-b", key_hex)
    _cli(live)._cmd_setperm(f"setperm {later.get_public_key().hex()} 3")

    assert _acl_rows(db) == []
    assert helper.get_acl_by_name("room-a") is None


def test_unregistering_an_identity_whose_hash_byte_was_taken_detaches_its_acl(db):
    first = LocalIdentity()
    second = LocalIdentity()
    while second.get_public_key()[0] != first.get_public_key()[0]:
        second = LocalIdentity()
    helper = _login_helper(db)
    helper.register_identity("room-a", first, identity_type="room_server", config=ROOM_CFG)
    displaced = helper.get_acl_by_name("room-a")
    helper.register_identity("room-b", second, identity_type="room_server", config=ROOM_CFG)

    helper.unregister_identity(first)
    _cli(displaced)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 3")

    assert helper.get_acl_by_name("room-a") is None
    assert db.load_acl_entries(first.get_public_key().hex()) == []


@pytest.mark.parametrize("password", ["", "adminpw"])
def test_a_refused_login_does_not_evict_anyone(db, password):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert _login(acl, LocalIdentity(), password, 0) == (False, 0)  # a replay
    assert acl.get_client(admin.get_public_key()) is not None
    assert acl.get_num_clients() == 1
    assert _repeater_acl(db, local).load() == 1


@pytest.mark.parametrize("password", ["", "guestpw"])
def test_only_an_admin_grant_may_evict_an_admin(db, password):
    """A blank-password or guest login must not strip provisioned admins."""
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    assert _login(acl, LocalIdentity(), password, 1) == (False, 0)
    assert acl.get_client(admin.get_public_key()) is not None
    assert _repeater_acl(db, local).load() == 1


def test_setperm_to_a_non_admin_role_does_not_evict_an_admin(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")

    reply = _cli(acl)._cmd_setperm(f"setperm {LocalIdentity().get_public_key().hex()} 1")
    assert reply != "OK"
    assert acl.get_client(admin.get_public_key()) is not None


def test_an_admin_login_that_cannot_be_stored_keeps_the_evicted_admin_stored(db):
    local = LocalIdentity()
    admin = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {admin.get_public_key().hex()} 3")
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))

    assert _login(acl, LocalIdentity(), "adminpw", 1) == (True, PERM_ACL_ADMIN)
    restarted = _repeater_acl(db, local)
    assert restarted.load() == 1
    assert restarted.get_client(admin.get_public_key()) is not None


def test_an_evicted_admins_old_login_cannot_be_replayed():
    acl = ACL(max_clients=1, admin_password="adminpw", local_identity=LocalIdentity())
    first, second = LocalIdentity(), LocalIdentity()
    assert _login(acl, first, "adminpw", 200) == (True, PERM_ACL_ADMIN)
    assert _login(acl, second, "adminpw", 300) == (True, PERM_ACL_ADMIN)  # evicts first
    assert acl.get_client(first.get_public_key()) is None

    assert _login(acl, first, "adminpw", 100) == (False, 0)  # a captured, older login
    assert _login(acl, first, "adminpw", 201) == (True, PERM_ACL_ADMIN)


def test_a_replay_is_caught_even_when_its_watermark_is_the_oldest_kept():
    acl = ACL(max_clients=1, admin_password="adminpw", local_identity=LocalIdentity())
    first = LocalIdentity()
    assert _login(acl, first, "adminpw", 500) == (True, PERM_ACL_ADMIN)
    for _ in range(8):  # fill the watermark cache behind the first admin's
        assert _login(acl, LocalIdentity(), "adminpw", 1000) == (True, PERM_ACL_ADMIN)

    assert _login(acl, first, "adminpw", 500) == (False, 0)


def test_an_eviction_left_stored_by_a_failed_write_is_deleted_once_storage_recovers(db):
    local = LocalIdentity()
    first, second = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {first.get_public_key().hex()} 3")
    real_upsert = db.upsert_acl_entry
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    assert _login(acl, second, "adminpw", 1) == (True, PERM_ACL_ADMIN)
    db.upsert_acl_entry = real_upsert

    assert _login(acl, second, "adminpw", 2) == (True, PERM_ACL_ADMIN)

    restarted = _repeater_acl(db, local, max_clients=1)
    assert restarted.load() == 1
    assert restarted.get_client(second.get_public_key()) is not None


def test_a_refused_replay_does_not_lose_the_watermark_on_retry():
    acl = ACL(max_clients=1, admin_password="adminpw", local_identity=LocalIdentity())
    first = LocalIdentity()
    assert _login(acl, first, "adminpw", 500) == (True, PERM_ACL_ADMIN)
    for _ in range(8):
        assert _login(acl, LocalIdentity(), "adminpw", 1000) == (True, PERM_ACL_ADMIN)

    assert _login(acl, first, "adminpw", 500) == (False, 0)
    assert _login(acl, first, "adminpw", 500) == (False, 0)  # and again


def test_an_unrelated_write_does_not_drop_a_deferred_eviction(db):
    local = LocalIdentity()
    first, second, other = LocalIdentity(), LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=2)
    _cli(acl)._cmd_setperm(f"setperm {first.get_public_key().hex()} 3")
    _cli(acl)._cmd_setperm(f"setperm {other.get_public_key().hex()} 3")
    acl.get_client(other.get_public_key()).last_activity = 10**12  # first goes
    real_upsert = db.upsert_acl_entry
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    assert _login(acl, second, "adminpw", 1) == (True, PERM_ACL_ADMIN)
    db.upsert_acl_entry = real_upsert

    _cli(acl)._cmd_setperm(f"setperm {other.get_public_key().hex()} 3")  # unrelated

    restarted = _repeater_acl(db, local, max_clients=2)
    restarted.load()
    assert restarted.get_client(first.get_public_key()) is not None  # second not stored


def test_a_deferred_eviction_passes_on_when_its_displacer_is_displaced(db):
    local = LocalIdentity()
    a, b, c = LocalIdentity(), LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {a.get_public_key().hex()} 3")
    real_upsert = db.upsert_acl_entry
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    assert _login(acl, b, "adminpw", 1) == (True, PERM_ACL_ADMIN)  # a stays stored
    db.upsert_acl_entry = real_upsert

    assert _login(acl, c, "adminpw", 1) == (True, PERM_ACL_ADMIN)  # displaces b

    restarted = _repeater_acl(db, local, max_clients=1)
    assert restarted.load() == 1
    assert restarted.get_client(c.get_public_key()) is not None


def test_replay_watermarks_stay_bounded_while_writes_fail(db):
    local = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    for _ in range(30):
        assert _login(acl, LocalIdentity(), "adminpw", 1) == (True, PERM_ACL_ADMIN)
    assert len(acl._evicted_watermarks) <= 8


def test_an_eviction_whose_delete_fails_is_retried_by_the_next_write_of_its_grant(db):
    local = LocalIdentity()
    first, second = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {first.get_public_key().hex()} 3")
    real_delete = db.delete_acl_entry
    db.delete_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    assert _login(acl, second, "adminpw", 1) == (True, PERM_ACL_ADMIN)
    db.delete_acl_entry = real_delete

    _cli(acl)._cmd_setperm(f"setperm {second.get_public_key().hex()} 3")

    restarted = _repeater_acl(db, local, max_clients=1)
    assert restarted.load() == 1
    assert restarted.get_client(second.get_public_key()) is not None


def test_a_returning_guest_does_not_keep_its_old_stored_admin_grant(db):
    local = LocalIdentity()
    a, b = LocalIdentity(), LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    _cli(acl)._cmd_setperm(f"setperm {a.get_public_key().hex()} 3")
    real_delete = db.delete_acl_entry
    db.delete_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    assert _login(acl, b, "adminpw", 1) == (True, PERM_ACL_ADMIN)  # a's row stays
    assert _login(acl, b, "guestpw", 2) == (True, PERM_ACL_GUEST)
    assert _login(acl, a, "", 3) == (True, PERM_ACL_GUEST)  # a is back, as a guest
    db.delete_acl_entry = real_delete
    assert _login(acl, a, "", 4) == (True, PERM_ACL_GUEST)

    restarted = _repeater_acl(db, local, max_clients=1)
    restarted.load()
    stored = restarted.get_client(a.get_public_key())
    assert stored is None or not stored.is_admin()


def test_never_stored_evictions_are_not_queued_for_deletion(db):
    local = LocalIdentity()
    acl = _repeater_acl(db, local, max_clients=1)
    db.upsert_acl_entry = MagicMock(side_effect=RuntimeError("disk full"))
    for _ in range(30):
        assert _login(acl, LocalIdentity(), "adminpw", 1) == (True, PERM_ACL_ADMIN)
    assert acl._pending_evictions == {}
