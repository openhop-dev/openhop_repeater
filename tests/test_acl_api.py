"""ACL management over the web API: listing, setting, removing, and identity changes."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cherrypy
import pytest
from openhop_core import LocalIdentity
from openhop_core.protocol import Identity

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.handler_helpers.login import LoginHelper
from repeater.handler_helpers.mesh_cli import MeshCLI
from repeater.web.api_endpoints import APIEndpoints

ROOM_SETTINGS = {"admin_password": "roomadmin", "guest_password": "roomguest"}


@pytest.fixture
def request_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request


class _Daemon:
    """The parts of the daemon the ACL endpoints read, backed by a real store."""

    def __init__(self, db, rooms):
        self.local_identity = LocalIdentity()
        self.login_helper = LoginHelper(
            identity_manager=MagicMock(), packet_injector=AsyncMock(), sqlite_handler=db
        )
        self.login_helper.register_identity(
            "repeater",
            self.local_identity,
            identity_type="repeater",
            config={"repeater": {"security": {"admin_password": "adminpw"}}},
        )
        self.rooms = []
        for name, identity in rooms:
            self.add_room(name, identity)
        self.identity_manager = SimpleNamespace(get_identities_by_type=self._by_type)
        self.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=db))

    def add_room(self, name, identity):
        cfg = {"name": name, "type": "room_server", "settings": dict(ROOM_SETTINGS)}
        self.login_helper.register_identity(name, identity, identity_type="room_server", config=cfg)
        self.rooms.append((name, identity, cfg))

    def _by_type(self, kind):
        return list(self.rooms) if kind == "room_server" else []


def _api(daemon, config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api.daemon_instance = daemon
    api.config_manager = MagicMock()
    api.config_manager.save_to_file.return_value = True
    return api


def _post(request, api_method, body):
    request.method = "POST"
    request.json = body
    return api_method()


@pytest.fixture
def db(tmp_path):
    return SQLiteHandler(tmp_path)


def test_set_list_and_remove_an_entry(db, request_ctx):
    daemon = _Daemon(db, [])
    api = _api(daemon)
    admin = LocalIdentity().get_public_key().hex()

    result = _post(
        request_ctx,
        api.acl_set_permissions,
        {"identity_name": "repeater", "client_pubkey": admin, "permissions": 3},
    )
    assert result["success"] is True
    assert result["data"]["persisted"] is True
    assert result["data"]["permissions"] == "admin"

    request_ctx.method = "GET"
    listed = api.acl_clients(identity_name="repeater")["data"]["clients"]
    assert [(c["public_key_full"], c["permissions_value"], c["persisted"]) for c in listed] == [
        (admin, 3, True)
    ]
    assert listed[0]["identity_pubkey"] == daemon.local_identity.get_public_key().hex()
    info = api.acl_info()["data"]["acls"][0]
    # Provisioned, not logged in: an entry, stored, but not a session.
    assert (info["acl_entries"], info["stored_entries"], info["authenticated_clients"]) == (1, 1, 0)

    removed = _post(
        request_ctx,
        api.acl_remove_client,
        {"client_pubkey": admin, "identity_name": "repeater"},
    )
    assert removed["success"] is True
    assert removed["data"]["removed_from"] == ["repeater"]
    assert db.load_acl_entries(daemon.local_identity.get_public_key().hex()) == []


def test_remove_accepts_the_legacy_public_key_field(db, request_ctx):
    daemon = _Daemon(db, [])
    api = _api(daemon)
    admin = LocalIdentity().get_public_key()
    daemon.login_helper.get_acl_by_name("repeater").apply_permissions(admin, 3)

    removed = _post(request_ctx, api.acl_remove_client, {"public_key": admin.hex()})
    assert removed["success"] is True


def test_remove_accepts_a_numeric_identity_hash(db, request_ctx):
    daemon = _Daemon(db, [])
    api = _api(daemon)
    acl = daemon.login_helper.get_acl_by_name("repeater")
    admin = LocalIdentity().get_public_key()
    acl.apply_permissions(admin, 3)
    hash_byte = acl.identity_pubkey_hex[:2]

    body = {"identity_hash": int(hash_byte, 16), "client_pubkey": admin.hex()}
    assert _post(request_ctx, api.acl_remove_client, body)["success"] is True


@pytest.mark.parametrize("path_hash_mode", [1, 2])
def test_remove_matches_the_multi_byte_hash_acl_clients_lists(db, request_ctx, path_hash_mode):
    """acl_clients lists a 2- or 3-byte identity_hash; remove must accept it back."""
    daemon = _Daemon(db, [])
    api = _api(daemon, {"mesh": {"path_hash_mode": path_hash_mode}})
    acl = daemon.login_helper.get_acl_by_name("repeater")
    admin = LocalIdentity().get_public_key()
    acl.apply_permissions(admin, 3)

    request_ctx.method = "GET"
    listed = api.acl_clients(identity_name="repeater")["data"]["clients"]
    identity_hash = listed[0]["identity_hash"]
    assert len(identity_hash) == 2 + 2 * (path_hash_mode + 1)

    removed = _post(
        request_ctx,
        api.acl_remove_client,
        {"identity_hash": identity_hash, "client_pubkey": admin.hex()},
    )
    assert removed["success"] is True
    assert removed["data"]["removed_from"] == ["repeater"]
    assert db.load_acl_entries(daemon.local_identity.get_public_key().hex()) == []


def test_a_multi_byte_hash_does_not_select_an_identity_sharing_only_its_first_byte(db):
    api = _api(_Daemon(db, []), {"mesh": {"path_hash_mode": 1}})
    (owner,) = api._acl_owners()
    key = owner[2].get_public_key()
    other = bytes([key[0], key[1] ^ 0xFF])
    assert api._acl_targets(None, f"0x{key[:2].hex().upper()}") == [owner]
    assert api._acl_targets(None, f"0x{other.hex()}") == []
    assert api._acl_targets(None, f"0X{key[:2].hex()}") == [owner]
    # A number has no width of its own: 0-255 still names the first byte alone.
    assert api._acl_targets(None, key[0]) == [owner]
    assert api._acl_targets(None, str(key[0])) == [owner]
    if key[0]:  # a zero first byte drops out of a number
        assert api._acl_targets(None, int.from_bytes(key[:2], "big")) == [owner]


def test_a_leading_zero_hash_byte_keeps_its_width(db):
    api = _api(_Daemon(db, []), {"mesh": {"path_hash_mode": 1}})
    (owner,) = api._acl_owners()
    key = owner[2].get_public_key()
    assert api._parse_hash_prefix("0x0042") == b"\x00\x42"
    assert api._parse_hash_prefix("0x042") == b"\x00\x42"
    assert api._parse_hash_prefix("0x42") == b"\x42"
    assert api._acl_targets(None, f"0x00{key[1]:02x}") == ([owner] if key[0] == 0 else [])


def test_acl_clients_filters_on_the_multi_byte_hash(db, request_ctx):
    daemon = _Daemon(db, [])
    api = _api(daemon, {"mesh": {"path_hash_mode": 1}})
    daemon.login_helper.get_acl_by_name("repeater").apply_permissions(
        LocalIdentity().get_public_key(), 3
    )
    key = daemon.local_identity.get_public_key()
    other = bytes([key[0], key[1] ^ 0xFF])

    result = api.acl_clients(identity_hash=f"0x{key[:2].hex()}")
    assert len(result["data"]["clients"]) == 1
    assert api.acl_clients(identity_hash=f"0x{other.hex()}")["data"]["clients"] == []


@pytest.mark.parametrize("identity_hash", ["0x", "0xzz", "-1", "nope", 1.5, [1], True])
def test_remove_rejects_a_malformed_identity_hash(db, request_ctx, identity_hash):
    api = _api(_Daemon(db, []))
    body = {"identity_hash": identity_hash, "client_pubkey": "aa" * 32}
    result = _post(request_ctx, api.acl_remove_client, body)
    assert result["success"] is False
    assert "identity_hash" in result["error"]


@pytest.mark.parametrize(
    "body, message",
    [
        ({"client_pubkey": "aa" * 32, "permissions": 3}, "identity_name"),
        ({"identity_name": "repeater", "client_pubkey": "aa" * 16, "permissions": 3}, "64"),
        ({"identity_name": "repeater", "client_pubkey": "zz" * 32, "permissions": 3}, "hex"),
        ({"identity_name": "repeater", "client_pubkey": "aa" * 32, "permissions": "3"}, "integer"),
        ({"identity_name": "repeater", "client_pubkey": "aa" * 32, "permissions": True}, "integer"),
        ({"identity_name": "repeater", "client_pubkey": "aa" * 32, "permissions": 0}, "remove"),
        ({"identity_name": "repeater", "client_pubkey": "aa" * 32, "permissions": 128}, "remove"),
        ({"identity_name": "nope", "client_pubkey": "aa" * 32, "permissions": 3}, "not found"),
        ({"identity_name": "repeater", "client_pubkey": "ab" * 32, "permissions": 3}, "valid"),
    ],
)
def test_set_permissions_validates_its_input(db, request_ctx, body, message):
    api = _api(_Daemon(db, []))
    result = _post(request_ctx, api.acl_set_permissions, body)
    assert result["success"] is False
    assert message in result["error"]


def test_a_room_read_write_entry_is_reported_as_not_stored(db, request_ctx):
    daemon = _Daemon(db, [("room-a", LocalIdentity())])
    api = _api(daemon)
    result = _post(
        request_ctx,
        api.acl_set_permissions,
        {
            "identity_name": "room-a",
            "client_pubkey": LocalIdentity().get_public_key().hex(),
            "permissions": 2,
        },
    )
    assert result["success"] is True
    assert result["data"]["persisted"] is False
    assert "until restart" in result["message"]


def test_identities_that_share_a_hash_byte_are_listed_separately(db, request_ctx):
    daemon = _Daemon(db, [])
    twin = LocalIdentity()
    while twin.get_public_key()[0] != daemon.local_identity.get_public_key()[0]:
        twin = LocalIdentity()
    daemon.add_room("room-a", twin)
    api = _api(daemon)
    for name in ("repeater", "room-a"):
        _post(
            request_ctx,
            api.acl_set_permissions,
            {
                "identity_name": name,
                "client_pubkey": LocalIdentity().get_public_key().hex(),
                "permissions": 3,
            },
        )

    request_ctx.method = "GET"
    listed = api.acl_clients()["data"]["clients"]
    assert sorted(c["identity_name"] for c in listed) == ["repeater", "room-a"]


def test_update_identity_moves_a_room_acl_when_renamed_and_rekeyed(db, request_ctx):
    old_seed = "11" * 32
    old_identity = LocalIdentity(seed=bytes.fromhex(old_seed))
    daemon = _Daemon(db, [("room-a", old_identity)])
    config = {
        "identities": {
            "room_servers": [
                {"name": "room-a", "identity_key": old_seed, "settings": dict(ROOM_SETTINGS)}
            ]
        }
    }
    api = _api(daemon, config)
    admin = LocalIdentity()
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(admin.get_public_key(), 3)

    new_seed = "22" * 32
    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b", "identity_key": new_seed}
    assert api.update_identity()["success"] is True

    new_identity = LocalIdentity(seed=bytes.fromhex(new_seed))
    rows = db.load_acl_entries(new_identity.get_public_key().hex())
    assert [r["client_pubkey"] for r in rows] == [admin.get_public_key().hex()]
    assert db.load_acl_entries(old_identity.get_public_key().hex()) == []


def test_delete_identity_drops_the_room_acl(db, request_ctx):
    seed = "33" * 32
    identity = LocalIdentity(seed=bytes.fromhex(seed))
    daemon = _Daemon(db, [("room-a", identity)])
    daemon.identity_manager.named_identities = {}
    config = {"identities": {"room_servers": [{"name": "room-a", "identity_key": seed}]}}
    api = _api(daemon, config)
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(
        LocalIdentity().get_public_key(), 3
    )

    request_ctx.method = "DELETE"
    assert api.delete_identity(name="room-a", type="room_server")["success"] is True
    assert db.load_acl_entries(identity.get_public_key().hex()) == []


def test_web_cli_get_acl_is_local(db, request_ctx):
    daemon = _Daemon(db, [])
    acl = daemon.login_helper.get_acl_by_name("repeater")
    admin = LocalIdentity().get_public_key()
    acl.apply_permissions(admin, 3)
    cli = MeshCLI(
        "/tmp/cfg.yaml",
        {"repeater": {}},
        SimpleNamespace(save_to_file=MagicMock(return_value=True), live_update_daemon=MagicMock()),
        acl=acl,
    )
    daemon.text_helper = SimpleNamespace(cli=cli)
    api = _api(daemon)

    # Past the auth decorator: authentication is not what this covers.
    reply = _post(request_ctx, lambda: APIEndpoints.cli.__wrapped__(api), {"command": "get acl"})
    assert reply["data"]["reply"] == "ACL:\n03 " + admin.hex().upper()
    # The mesh path passes no local flag.
    assert cli.handle_command(b"x", "get acl", is_admin=True).startswith("Error:")


def test_a_loaded_admin_has_a_secret_for_the_current_key(db):
    # The UI's "stored" entries must be usable straight after a restart.
    daemon = _Daemon(db, [])
    admin = LocalIdentity()
    daemon.login_helper.get_acl_by_name("repeater").apply_permissions(admin.get_public_key(), 3)

    restarted = LoginHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sqlite_handler=db
    )
    restarted.register_identity(
        "repeater", daemon.local_identity, identity_type="repeater", config={"repeater": {}}
    )
    client = restarted.get_acl_by_name("repeater").get_client(admin.get_public_key())
    assert client.shared_secret == Identity(admin.get_public_key()).calc_shared_secret(
        daemon.local_identity.get_private_key()
    )


def _room_update_setup(db):
    old_seed, other_seed = "11" * 32, "44" * 32
    old_identity = LocalIdentity(seed=bytes.fromhex(old_seed))
    daemon = _Daemon(db, [("room-a", old_identity)])
    config = {
        "identities": {
            "room_servers": [
                {"name": "room-a", "identity_key": old_seed, "settings": dict(ROOM_SETTINGS)},
                {"name": "room-b", "identity_key": other_seed, "settings": dict(ROOM_SETTINGS)},
            ]
        }
    }
    admin = LocalIdentity()
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(admin.get_public_key(), 3)
    return daemon, config, old_identity, admin


def test_update_identity_refuses_a_key_another_identity_uses(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "renamed", "identity_key": "44" * 32}
    result = api.update_identity()

    assert result["success"] is False
    assert "already used" in result["error"]
    room = config["identities"]["room_servers"][0]
    assert (room["name"], room["identity_key"]) == ("room-a", "11" * 32)
    api.config_manager.save_to_file.assert_not_called()
    assert len(db.load_acl_entries(old_identity.get_public_key().hex())) == 1


@pytest.mark.parametrize("taken", ["repeater", "comp-a"])
def test_a_room_cannot_take_a_name_another_identity_uses(db, request_ctx, taken):
    """Names are unique across every identity at boot, so a duplicate saved
    here would leave a config the next restart refuses."""
    daemon, config, old_identity, admin = _room_update_setup(db)
    config["identities"]["companions"] = [{"name": "comp-a", "identity_key": "55" * 32}]
    api = _api(daemon, config)

    live_entry = config["identities"]["room_servers"][0]

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": taken}
    result = api.update_identity()

    assert result["success"] is False
    # Restored in place: the identity manager holds this same dict.
    assert config["identities"]["room_servers"][0] is live_entry
    assert live_entry["name"] == "room-a"
    api.config_manager.save_to_file.assert_not_called()


def test_a_new_room_cannot_reuse_another_identitys_key(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)

    request_ctx.method = "POST"
    request_ctx.json = {
        "name": "room-c",
        "type": "room_server",
        "identity_key": "44" * 32,  # room-b's
        "settings": dict(ROOM_SETTINGS),
    }
    result = api.create_identity()

    assert result["success"] is False
    assert "already used" in result["error"]
    assert [r["name"] for r in config["identities"]["room_servers"]] == ["room-a", "room-b"]
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize("taken", ["repeater", "room-a"])
def test_a_new_companion_cannot_take_a_name_another_identity_uses(db, request_ctx, taken):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)

    request_ctx.method = "POST"
    request_ctx.json = {"name": taken, "type": "companion", "identity_key": "66" * 32}
    result = api.create_identity()

    assert result["success"] is False
    assert "already exists" in result["error"]
    api.config_manager.save_to_file.assert_not_called()


def test_renaming_a_room_to_its_own_name_is_allowed(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-a"}
    assert api.update_identity()["success"] is True


@pytest.mark.parametrize("settings", ["x", {"flood_advert_interval_hours": "abc"}])
def test_bad_room_settings_undo_the_rename_in_the_same_request(db, request_ctx, settings):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)
    before = copy.deepcopy(config["identities"]["room_servers"][0])

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "renamed", "settings": settings}
    assert api.update_identity()["success"] is False
    assert config["identities"]["room_servers"][0] == before


def test_identity_hash_zero_names_hash_byte_zero_not_every_acl(db):
    """0 is falsy; it must not fall through to "no filter, every ACL"."""
    api = _api(_Daemon(db, []))
    owners = api._acl_owners()
    assert api._acl_targets(None, 0) == [o for o in owners if o[2].get_public_key()[0] == 0]
    assert api._acl_targets(None, 0) != owners or not owners


def test_the_identity_endpoints_stay_exposed():
    from repeater.web.api_endpoints import APIEndpoints

    for name in ("create_identity", "update_identity", "delete_identity"):
        assert getattr(getattr(APIEndpoints, name), "exposed", False), name


def test_update_identity_does_not_save_a_key_whose_acl_did_not_move(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)
    daemon.login_helper.move_room_acl = MagicMock(side_effect=RuntimeError("disk full"))

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "identity_key": "22" * 32}
    result = api.update_identity()

    assert result["success"] is False
    api.config_manager.save_to_file.assert_not_called()
    assert config["identities"]["room_servers"][0]["identity_key"] == "11" * 32


def test_a_failed_config_save_moves_the_acl_back(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)
    api.config_manager.save_to_file.return_value = False

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "identity_key": "22" * 32}
    assert api.update_identity()["success"] is False

    assert len(db.load_acl_entries(old_identity.get_public_key().hex())) == 1
    new_identity = LocalIdentity(seed=bytes.fromhex("22" * 32))
    assert db.load_acl_entries(new_identity.get_public_key().hex()) == []


def test_acl_info_uses_the_right_acl_when_hashes_collide(db, request_ctx):
    daemon = _Daemon(db, [])
    twin = LocalIdentity()
    while twin.get_public_key()[0] != daemon.local_identity.get_public_key()[0]:
        twin = LocalIdentity()
    daemon.add_room("room-a", twin)
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(
        LocalIdentity().get_public_key(), 3
    )
    api = _api(daemon)

    acls = {a["name"]: a for a in api.acl_info()["data"]["acls"]}
    assert acls["repeater"]["acl_entries"] == 0
    assert acls["room-a"]["acl_entries"] == 1
    assert acls["room-a"]["has_admin_password"] is True
    assert acls["repeater"]["store_error"] is None


def test_a_refused_update_leaves_the_live_config_unchanged(db, request_ctx):
    daemon, config, old_identity, admin = _room_update_setup(db)
    api = _api(daemon, config)
    before = dict(config["identities"]["room_servers"][0], settings=dict(ROOM_SETTINGS))

    request_ctx.method = "PUT"
    request_ctx.json = {
        "name": "room-a",
        "new_name": "renamed",
        "settings": {"admin_password": "same", "guest_password": "same"},
    }
    assert api.update_identity()["success"] is False
    assert config["identities"]["room_servers"][0] == before


def test_a_key_change_with_no_store_is_refused(request_ctx):
    config = {
        "identities": {
            "room_servers": [{"name": "room-a", "identity_key": "11" * 32, "settings": {}}]
        }
    }
    api = _api(SimpleNamespace(), config)
    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "identity_key": "22" * 32}
    result = api.update_identity()
    assert result["success"] is False
    assert "no ACL store" in result["error"]
    api.config_manager.save_to_file.assert_not_called()


def test_a_room_without_an_acl_does_not_borrow_one_by_hash(db, request_ctx):
    daemon = _Daemon(db, [])
    twin = LocalIdentity()
    while twin.get_public_key()[0] != daemon.local_identity.get_public_key()[0]:
        twin = LocalIdentity()
    # Registered with the identity manager but, having no passwords, not for logins.
    daemon.rooms.append(("room-b", twin, {"name": "room-b", "settings": {}}))
    api = _api(daemon)

    names = [a["name"] for a in api.acl_info()["data"]["acls"]]
    assert names == ["repeater"]
    result = _post(
        request_ctx,
        api.acl_set_permissions,
        {
            "identity_name": "room-b",
            "client_pubkey": LocalIdentity().get_public_key().hex(),
            "permissions": 3,
        },
    )
    assert result["success"] is False


def test_acl_clients_reports_an_unreadable_store(db, request_ctx):
    daemon = _Daemon(db, [])
    daemon.login_helper.get_acl_by_name("repeater").load_error = "database is locked"
    api = _api(daemon)
    data = api.acl_clients()["data"]
    assert data["store_errors"] == {"repeater": "database is locked"}


def test_remove_from_every_acl_reports_which_failed(db, request_ctx):
    from repeater.handler_helpers.acl import ACLStoreError

    daemon = _Daemon(db, [("room-a", LocalIdentity())])
    key = LocalIdentity().get_public_key()
    for name in ("repeater", "room-a"):
        daemon.login_helper.get_acl_by_name(name).apply_permissions(key, 3)
    daemon.login_helper.get_acl_by_name("room-a").remove_client = MagicMock(
        side_effect=ACLStoreError("disk full")
    )
    api = _api(daemon)

    result = _post(request_ctx, api.acl_remove_client, {"client_pubkey": key.hex()})
    assert result["success"] is False
    assert "room-a (disk full)" in result["error"]
    assert "removed from repeater" in result["error"]


class _LiveDaemon:
    """A daemon whose hot reload goes through the real IdentityManager, as main.py's does."""

    def __init__(self, db, room_name, room_seed):
        from repeater.identity_manager import IdentityManager

        self.identity_manager = IdentityManager({})
        self.local_identity = LocalIdentity()
        self.login_helper = LoginHelper(
            identity_manager=self.identity_manager, packet_injector=AsyncMock(), sqlite_handler=db
        )
        self.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=db))
        self.login_helper.register_identity(
            "repeater", self.local_identity, identity_type="repeater", config={"repeater": {}}
        )
        cfg = {"name": room_name, "identity_key": room_seed, "settings": dict(ROOM_SETTINGS)}
        self._register_identity_everywhere(
            room_name, LocalIdentity(seed=bytes.fromhex(room_seed)), cfg, "room_server"
        )
        self.config = {"identities": {"room_servers": [cfg]}}

    def _register_identity_everywhere(
        self, name, identity, config, identity_type, previous_name=None
    ):
        if not self.identity_manager.register_identity(
            name=name, identity=identity, config=config, identity_type=identity_type
        ):
            return False
        self.login_helper.register_identity(
            name=name, identity=identity, identity_type=identity_type, config=config
        )
        return True


def test_a_rename_applies_live_and_the_room_stays_manageable(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    acl = daemon.login_helper.get_acl_by_name("room-a")
    acl.apply_permissions(LocalIdentity().get_public_key(), 3)

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    result = api.update_identity()
    assert "applied immediately" in result["message"]

    assert daemon.login_helper.get_acl_by_name("room-b") is acl
    assert daemon.login_helper.get_acl_by_name("room-a") is None
    added = _post(
        request_ctx,
        api.acl_set_permissions,
        {
            "identity_name": "room-b",
            "client_pubkey": LocalIdentity().get_public_key().hex(),
            "permissions": 3,
        },
    )
    assert added["success"] is True


def test_a_rename_then_a_rekey_leaves_one_live_acl(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(
        LocalIdentity().get_public_key(), 3
    )

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    api.update_identity()
    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-b", "identity_key": "22" * 32}
    assert "applied immediately" in api.update_identity()["message"]

    attached = [a for a in set(daemon.login_helper.acls_by_name.values()) if not a.detached]
    assert len(attached) == 2  # the repeater and the room
    assert [n for n, *_ in daemon.identity_manager.get_identities_by_type("room_server")] == [
        "room-b"
    ]
    new_pubkey = LocalIdentity(seed=bytes.fromhex("22" * 32)).get_public_key().hex()
    assert daemon.login_helper.get_acl_by_name("room-b").store_key == new_pubkey
    assert len(db.load_acl_entries(new_pubkey)) == 1


def test_a_refused_hot_reload_keeps_the_room_registered(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    daemon._register_identity_everywhere = MagicMock(return_value=False)

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    assert "Restart required" in api.update_identity()["message"]
    assert [n for n, *_ in daemon.identity_manager.get_identities_by_type("room_server")] == [
        "room-a"
    ]


def test_a_hot_reload_that_raises_keeps_the_room_registered(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    daemon._register_identity_everywhere = MagicMock(side_effect=RuntimeError("boom"))

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    assert "Restart required" in api.update_identity()["message"]
    assert [n for n, *_ in daemon.identity_manager.get_identities_by_type("room_server")] == [
        "room-a"
    ]


def test_a_rename_that_clears_the_passwords_waits_for_a_restart(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)

    request_ctx.method = "PUT"
    request_ctx.json = {
        "name": "room-a",
        "new_name": "room-b",
        "settings": {"admin_password": "", "guest_password": ""},
    }
    assert "Restart required" in api.update_identity()["message"]
    # Nothing half-applied: the running room keeps its registration and ACL.
    assert [n for n, *_ in daemon.identity_manager.get_identities_by_type("room_server")] == [
        "room-a"
    ]
    assert daemon.login_helper.get_acl_by_name("room-a") is not None


def test_delete_identity_frees_the_name_and_hash(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)

    request_ctx.method = "DELETE"
    assert api.delete_identity(name="room-a", type="room_server")["success"] is True
    identity = LocalIdentity(seed=bytes.fromhex("11" * 32))
    # Re-creating the room registers at once instead of conflicting until a restart.
    assert daemon.identity_manager.registration_error("room-a", identity, "room_server") is None


def test_giving_a_passwordless_room_a_password_registers_it_live(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "settings": {"admin_password": "", "guest_password": ""}}
    api.update_identity()

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "settings": {"admin_password": "new-admin"}}
    result = api.update_identity()
    assert "no reload needed" not in result["message"]
    assert daemon.login_helper.get_acl_by_name("room-a").admin_password == "new-admin"


def test_a_hot_reload_names_the_room_it_replaces(db, request_ctx):
    # The text helper stops the replaced RoomServer's sync loop by this name.
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    real = daemon._register_identity_everywhere
    daemon._register_identity_everywhere = MagicMock(side_effect=real)

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    api.update_identity()
    assert daemon._register_identity_everywhere.call_args.kwargs["previous_name"] == "room-a"


def test_a_rekey_stops_the_old_key_answering_logins(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    old_hash = LocalIdentity(seed=bytes.fromhex("11" * 32)).get_public_key()[0]
    new_seed = "22" * 32
    while LocalIdentity(seed=bytes.fromhex(new_seed)).get_public_key()[0] == old_hash:
        new_seed = new_seed[2:] + "33"
    daemon._unregister_identity_everywhere = lambda identity: (
        daemon.login_helper.unregister_identity(identity)
    )
    api = _api(daemon, daemon.config)
    assert old_hash in daemon.login_helper.handlers

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "identity_key": new_seed}
    assert "applied immediately" in api.update_identity()["message"]

    assert old_hash not in daemon.login_helper.handlers
    new_hash = LocalIdentity(seed=bytes.fromhex(new_seed)).get_public_key()[0]
    assert new_hash in daemon.login_helper.handlers


def test_deleting_a_room_stops_it_answering_logins(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    room_hash = LocalIdentity(seed=bytes.fromhex("11" * 32)).get_public_key()[0]
    daemon._unregister_identity_everywhere = lambda identity: (
        daemon.login_helper.unregister_identity(identity)
    )
    api = _api(daemon, daemon.config)

    request_ctx.method = "DELETE"
    result = api.delete_identity(name="room-a", type="room_server")
    assert "deactivated immediately" in result["message"]
    assert room_hash not in daemon.login_helper.handlers
    assert daemon.login_helper.get_acl_by_name("room-a") is None


def test_a_new_room_does_not_inherit_a_deleted_rooms_leftover_rows(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    identity = LocalIdentity(seed=bytes.fromhex("11" * 32))
    daemon.login_helper.get_acl_by_name("room-a").apply_permissions(
        LocalIdentity().get_public_key(), 3
    )
    # The delete's ACL cleanup fails, leaving the old admin stored.
    real_delete = db.delete_acl_label
    db.delete_acl_label = MagicMock(side_effect=RuntimeError("disk full"))
    request_ctx.method = "DELETE"
    result = api.delete_identity(name="room-a", type="room_server")
    assert "could not be removed" in result["message"]
    db.delete_acl_label = real_delete

    request_ctx.method = "POST"
    request_ctx.json = {
        "name": "room-a",
        "type": "room_server",
        "identity_key": "11" * 32,
        "settings": dict(ROOM_SETTINGS),
    }
    api.create_identity()
    assert db.load_acl_entries(identity.get_public_key().hex()) == []


def test_a_rename_is_refused_when_the_new_names_leftovers_cannot_be_cleared(db, request_ctx):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    db.delete_acl_label = MagicMock(side_effect=RuntimeError("disk full"))

    request_ctx.method = "PUT"
    request_ctx.json = {"name": "room-a", "new_name": "room-b"}
    result = api.update_identity()
    assert result["success"] is False
    assert daemon.config["identities"]["room_servers"][0]["name"] == "room-a"
    api.config_manager.save_to_file.assert_not_called()


def test_creating_a_room_is_refused_when_leftovers_under_its_name_cannot_be_cleared(
    db, request_ctx
):
    daemon = _LiveDaemon(db, "room-a", "11" * 32)
    api = _api(daemon, daemon.config)
    db.delete_acl_label = MagicMock(side_effect=RuntimeError("disk full"))

    request_ctx.method = "POST"
    request_ctx.json = {
        "name": "room-new",
        "type": "room_server",
        "identity_key": "44" * 32,
        "settings": dict(ROOM_SETTINGS),
    }
    result = api.create_identity()
    assert result["success"] is False
    assert [r["name"] for r in daemon.config["identities"]["room_servers"]] == ["room-a"]


def test_acl_clients_names_a_client_from_its_advert_or_a_companion_contact(db, request_ctx):
    daemon = _Daemon(db, [])
    heard, via_companion, unknown = (LocalIdentity().get_public_key() for _ in range(3))
    acl = daemon.login_helper.get_acl_by_name("repeater")
    for key in (heard, via_companion, unknown):
        acl.apply_permissions(key, 3)
    now = 1_790_000_000
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO adverts (timestamp, pubkey, node_name, is_repeater, contact_type, "
            "first_seen, last_seen, is_new_neighbor) VALUES (?, ?, ?, 0, 'Chat Node', ?, ?, 0)",
            (now, heard.hex(), "Heard Node", now, now),
        )
        conn.execute(
            "INSERT INTO companion_contacts (companion_hash, pubkey, name, updated_at) "
            "VALUES ('0xab', ?, 'Contact Name', ?)",
            (via_companion, now),
        )
    api = _api(daemon)

    clients = {c["public_key_full"]: c for c in api.acl_clients()["data"]["clients"]}
    assert (clients[heard.hex()]["client_name"], clients[heard.hex()]["client_type"]) == (
        "Heard Node",
        "Chat Node",
    )
    assert clients[via_companion.hex()]["client_name"] == "Contact Name"
    assert clients[unknown.hex()]["client_name"] is None
