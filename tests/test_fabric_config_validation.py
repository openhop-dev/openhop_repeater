"""Fabric fan-out is checked where a caller can still act on the answer.

``build_radio_stack`` validates ``fabric:`` when the daemon starts. Everything
that writes config -- the UI, a restore, a hand edit -- ran long before that, so
an impossible combination used to persist and then take the node down at the
next restart, with no one left to tell. These cover the checks on the write and
pre-restart paths.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.config import validate_fabric_config
from repeater.web.api_endpoints import APIEndpoints


def _two_radios():
    return [{"id": "local"}, {"id": "link"}]


def _make_api(config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api.daemon_instance = None
    api.send_advert_func = None
    api.event_loop = None
    api.stats_getter = None
    api._config_path = "/tmp/test-fabric-config.yaml"
    api.config_manager = MagicMock()
    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True
    return api


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request, response


# ---------------------------------------------------------------------------
# validate_fabric_config
# ---------------------------------------------------------------------------


def test_bridge_two_radio_repeat_on_ingress_is_accepted():
    assert validate_fabric_config(
        {"fabric": {"tx_mode": "bridge", "repeat_on_ingress": True}, "radios": _two_radios()}
    ) == (True, "default")


def test_repeat_on_ingress_requires_bridge():
    with pytest.raises(ValueError, match="requires fabric.tx_mode=bridge"):
        validate_fabric_config(
            {"fabric": {"tx_mode": "sticky", "repeat_on_ingress": True}, "radios": _two_radios()}
        )


@pytest.mark.parametrize(
    "radios", [None, [{"id": "only"}], [{"id": "a"}, {"id": "b"}, {"id": "c"}]]
)
def test_repeat_on_ingress_requires_exactly_two_radios(radios):
    with pytest.raises(ValueError, match="requires exactly two radios"):
        validate_fabric_config(
            {"fabric": {"tx_mode": "bridge", "repeat_on_ingress": True}, "radios": radios}
        )


def test_origin_tx_all_requires_exactly_two_radios():
    with pytest.raises(ValueError, match="requires exactly two radios"):
        validate_fabric_config({"fabric": {"origin_tx": "all"}, "radios": [{"id": "only"}]})


def test_origin_tx_all_on_two_radios_needs_no_particular_tx_mode():
    assert validate_fabric_config({"fabric": {"origin_tx": "all"}, "radios": _two_radios()}) == (
        False,
        "all",
    )


def test_unknown_tx_mode_is_rejected():
    with pytest.raises(ValueError, match="Unknown fabric.tx_mode"):
        validate_fabric_config({"fabric": {"tx_mode": "briidge"}, "radios": _two_radios()})


def test_blank_tx_mode_reads_as_default():
    # _apply_fabric_tx_mode treats "" as default, so validation must agree
    # rather than refusing a config the daemon would happily start.
    assert validate_fabric_config({"fabric": {"tx_mode": ""}, "radios": _two_radios()}) == (
        False,
        "default",
    )


def test_conflicting_origin_tx_spellings_are_rejected():
    with pytest.raises(ValueError, match="its former name"):
        validate_fabric_config(
            {
                "fabric": {"origin_tx": "default", "local_tx_mode": "all"},
                "radios": _two_radios(),
            }
        )


def test_matching_origin_tx_spellings_are_accepted():
    assert validate_fabric_config(
        {"fabric": {"origin_tx": "all", "local_tx_mode": "all"}, "radios": _two_radios()}
    ) == (False, "all")


def test_missing_fabric_section_is_valid():
    assert validate_fabric_config({}) == (False, "default")


# ---------------------------------------------------------------------------
# config_import
# ---------------------------------------------------------------------------


def test_config_import_rejects_impossible_fanout(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api({"repeater": {"security": {"admin_password": "set"}}})

    request.json = {
        "config": {
            "radios": [{"id": "only"}],
            "fabric": {"tx_mode": "bridge", "repeat_on_ingress": True},
        }
    }

    result = api.config_import()

    assert result["success"] is False
    assert "requires exactly two radios" in result["error"]


def test_rejected_fanout_leaves_config_untouched(cherrypy_ctx):
    """The import loop mutates self.config section by section.

    Validating after it started would leave ``radios`` already replaced while
    the request reports failure -- a node whose in-memory radio list no longer
    matches what is on disk.
    """
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "bridge"},
        }
    )

    request.json = {
        "config": {
            "radios": [{"id": "only"}],
            "fabric": {"repeat_on_ingress": True},
        }
    }

    result = api.config_import()

    assert result["success"] is False
    assert api.config["radios"] == _two_radios()
    assert api.config["fabric"] == {"tx_mode": "bridge"}
    api.config_manager.save_to_file.assert_not_called()


def test_config_import_validates_against_the_merged_fabric(cherrypy_ctx):
    """A partial fabric edit is checked against the tx_mode already on the node.

    The import dict-merges ``fabric``, so an incoming ``repeat_on_ingress``
    alone still has to answer for the stored ``tx_mode``.
    """
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "sticky"},
        }
    )

    request.json = {"config": {"fabric": {"repeat_on_ingress": True}}}

    result = api.config_import()

    assert result["success"] is False
    assert "requires fabric.tx_mode=bridge" in result["error"]


def test_config_import_accepts_a_valid_fanout_edit(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "bridge"},
        }
    )

    request.json = {"config": {"fabric": {"repeat_on_ingress": True, "origin_tx": "all"}}}

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is True
    assert api.config["fabric"]["repeat_on_ingress"] is True
    assert api.config["fabric"]["origin_tx"] == "all"


# ---------------------------------------------------------------------------
# validate_config (pre-restart check)
# ---------------------------------------------------------------------------


_BASE_YAML = """
repeater:
  node_name: bridge-node
  security:
    admin_password: supersecret
radio_type: none
"""


def _write_config(tmp_path, extra: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text((_BASE_YAML + extra).strip(), encoding="utf-8")
    return str(path)


def test_validate_config_flags_impossible_fanout(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = _write_config(
        tmp_path,
        """
radios:
  - id: only
fabric:
  tx_mode: bridge
  repeat_on_ingress: true
""",
    )

    result = api.validate_config()

    assert result["data"]["valid"] is False
    assert result["data"]["blocked_restart"] is True
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "fabric" in paths


def test_validate_config_passes_a_workable_bridge(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = _write_config(
        tmp_path,
        """
radios:
  - id: local
  - id: link
fabric:
  tx_mode: bridge
  repeat_on_ingress: true
  origin_tx: all
""",
    )

    result = api.validate_config()

    assert result["data"]["valid"] is True
    assert result["data"]["summary"]["error_count"] == 0


def test_validate_config_reports_invalid_duty_cycle_scope(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = _write_config(
        tmp_path,
        """
duty_cycle:
  budget_scope: country-default
""",
    )

    result = api.validate_config()

    assert result["data"]["valid"] is False
    assert any(error["path"] == "duty_cycle" for error in result["data"]["errors"])


def test_clearing_radios_alone_is_refused_while_fanout_is_still_set(cherrypy_ctx):
    """Dropping to one radio invalidates the fan-out that described two.

    The UI clears the fan-out keys in the same request for this reason; this
    pins the behaviour that makes that necessary.
    """
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "bridge", "repeat_on_ingress": True},
        }
    )

    request.json = {"config": {"radios": None}}

    result = api.config_import()

    assert result["success"] is False
    assert "requires exactly two radios" in result["error"]


def test_clearing_radios_with_the_fanout_is_accepted(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "bridge", "repeat_on_ingress": True, "origin_tx": "all"},
        }
    )

    request.json = {
        "config": {
            "radios": None,
            "fabric": {"repeat_on_ingress": False, "origin_tx": "default"},
        }
    }

    result = api.config_import()

    assert result["success"] is True
    assert "radios" not in api.config


# ---------------------------------------------------------------------------
# Repair and structural paths (regressions found in review)
# ---------------------------------------------------------------------------


def test_tx_mode_is_not_checked_where_the_daemon_never_applies_it():
    """A legacy single-radio node boots with any tx_mode at all.

    build_radio_stack only calls _apply_fabric_tx_mode -- the thing that raises
    on an unknown mode -- for a non-empty radios list or use_fabric. Rejecting a
    stale tx_mode on a node that starts fine would block unrelated imports over
    a value nothing reads.
    """
    assert validate_fabric_config({"fabric": {"tx_mode": "briidge"}}) == (False, "default")


@pytest.mark.parametrize(
    "config",
    [
        {"fabric": {"tx_mode": "briidge"}, "radios": _two_radios()},
        {"fabric": {"tx_mode": "briidge", "use_fabric": True}},
    ],
)
def test_tx_mode_is_checked_where_the_daemon_does_apply_it(config):
    with pytest.raises(ValueError, match="Unknown fabric.tx_mode"):
        validate_fabric_config(config)


def test_empty_radios_list_counts_as_one_radio():
    with pytest.raises(ValueError, match="requires exactly two radios"):
        validate_fabric_config(
            {"fabric": {"tx_mode": "bridge", "repeat_on_ingress": True}, "radios": []}
        )


def test_clearing_the_fabric_section_repairs_an_invalid_node(cherrypy_ctx):
    """``fabric: null`` replaces the section wholesale, so it must validate as empty.

    Fan-out could be persisted invalid by a build predating write-time
    validation. Wiping the section is how an operator fixes that, and checking
    the *old* section would reject the one import that repairs the node.
    """
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "sticky", "repeat_on_ingress": True},
        }
    )

    request.json = {"config": {"fabric": None}}

    result = api.config_import()

    assert result["success"] is True
    assert api.config["fabric"] is None


def test_malformed_radios_is_refused_before_anything_is_merged(cherrypy_ctx):
    """The loop's own radios check runs after earlier sections are already in.

    A request that fails must leave self.config exactly as it was, or the node
    is running settings that were never saved.
    """
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api({"repeater": {"security": {"admin_password": "set"}, "node_name": "before"}})

    request.json = {"config": {"repeater": {"node_name": "changed"}, "radios": "not-a-list"}}

    result = api.config_import()

    assert result["success"] is False
    assert "radios must be a list or null" in result["error"]
    assert api.config["repeater"]["node_name"] == "before"
    api.config_manager.save_to_file.assert_not_called()


def test_malformed_radios_reports_its_own_problem_not_a_radio_count(cherrypy_ctx):
    """Structure is the real error; a non-list must not surface as "one radio"."""
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.user = {"username": "admin", "auth_type": "jwt"}
    api = _make_api(
        {
            "repeater": {"security": {"admin_password": "set"}},
            "radios": _two_radios(),
            "fabric": {"tx_mode": "bridge", "repeat_on_ingress": True},
        }
    )

    request.json = {"config": {"radios": "not-a-list"}}

    result = api.config_import()

    assert result["success"] is False
    assert "radios must be a list or null" in result["error"]
    assert "two radios" not in result["error"]
