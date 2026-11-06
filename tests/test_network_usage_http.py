"""Real CherryPy authenticated query dispatch, backed by temporary SQLite."""

import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from wsgiref.simple_server import WSGIRequestHandler, make_server

import cherrypy
import pytest
import yaml

from repeater.data_acquisition.network_usage import NetworkUsageCollector
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.auth.cherrypy_tool import register_require_auth_tool


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def test_network_usage_openapi_hour_contract():
    spec = yaml.safe_load((Path(__file__).parents[1] / "repeater/web/openapi.yaml").read_text())
    endpoint = spec["paths"]["/network_usage"]["get"]
    params = {param["name"]: param for param in endpoint["parameters"]}
    assert params["hours"]["required"] is False
    assert params["hours"]["schema"] == {
        "type": "integer",
        "default": 24,
        "minimum": 1,
        "maximum": 168,
    }
    assert params["days"]["deprecated"] is True
    assert params["days"]["required"] is False
    schema = spec["components"]["schemas"]["NetworkUsage"]
    assert "hours" in schema["required"]
    assert "days" in schema["required"]
    assert schema["properties"]["hours"]["type"] == "integer"
    assert schema["properties"]["days"]["type"] == "number"
    assert schema["properties"]["days"]["minimum"] <= 1 / 24


@pytest.fixture
def usage_http(monkeypatch, tmp_path):
    monkeypatch.setattr(cherrypy, "request", cherrypy._ThreadLocalProxy("request"))
    monkeypatch.setattr(cherrypy, "response", cherrypy._ThreadLocalProxy("response"))
    register_require_auth_tool()
    monkeypatch.setitem(cherrypy.config, "jwt_handler", SimpleNamespace(verify_jwt=lambda _: None))
    monkeypatch.setitem(
        cherrypy.config,
        "token_manager",
        SimpleNamespace(
            verify_token=lambda token: (
                {"id": "test", "name": "test"} if token == "test-key" else None
            )
        ),
    )
    collector = NetworkUsageCollector(SQLiteHandler(tmp_path), clock=lambda: 86400, counters=dict)
    storage = SimpleNamespace(network_usage=collector)
    api = APIEndpoints.__new__(APIEndpoints)
    api.daemon_instance = SimpleNamespace(repeater_handler=SimpleNamespace(storage=storage))
    app = cherrypy.Application(
        SimpleNamespace(api=api), config={"/api": {"tools.require_auth.on": True}}
    )
    with make_server("127.0.0.1", 0, app, handler_class=QuietHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(query="", authenticated=True, path="/api/network_usage", method="GET"):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(
                    method, path + query, headers={"X-API-Key": "test-key"} if authenticated else {}
                )
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        try:
            yield get, collector
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_network_usage_authenticated_query_direct_contract(usage_http):
    get, collector = usage_http
    collector.database.store_usage_buckets(
        [
            {
                "kind": "interface",
                "source": "eth0",
                "bucket": 60,
                "upload_bytes": 11,
                "download_bytes": 22,
                "messages": 0,
            }
        ]
    )
    status, body = get("?days=1")
    assert status == 200, body
    data = json.loads(body)
    assert set(data) == {
        "hours",
        "days",
        "start",
        "end",
        "bucket_seconds",
        "available_since",
        "partial",
        "scope",
        "interfaces",
        "mqtt",
        "collection",
    }
    assert data["days"] == 1
    assert data["start"] == 0 and data["end"] == 86400
    assert data["interfaces"][0]["download_bytes"] == 22
    assert data["partial"] is True
    assert data["available_since"] is None
    assert json.loads(get()[1])["days"] == 1
    assert json.loads(get("?days=7")[1])["days"] == 7


@pytest.mark.parametrize("value", ["0", "8", "1.5", "no", "", "-1", "true"])
def test_network_usage_invalid_days_is_400(usage_http, value):
    get, _ = usage_http
    status, body = get("?days=" + value)
    assert status == 400, body
    assert json.loads(body)["success"] is False


@pytest.mark.parametrize("hours", [1, 6, 24, 48, 168, 2, 167])
def test_network_usage_authenticated_hour_windows(usage_http, hours):
    get, _ = usage_http
    status, body = get(f"?hours={hours}")
    assert status == 200, body
    report = json.loads(body)
    assert type(report["hours"]) is int
    assert report["hours"] == hours
    assert report["days"] == hours / 24
    assert report["end"] == 86400
    assert report["start"] == 86400 - hours * 3600


def test_network_usage_default_and_legacy_hours(usage_http):
    get, _ = usage_http
    assert json.loads(get()[1])["hours"] == 24
    for days in range(1, 8):
        status, body = get(f"?days={days}")
        assert status == 200, body
        assert json.loads(body)["hours"] == days * 24


@pytest.mark.parametrize("value", ["0", "169", "1.5", "no", "", "-1", "true"])
def test_network_usage_invalid_hours_is_400(usage_http, value):
    get, _ = usage_http
    status, body = get("?hours=" + value)
    assert status == 400, body
    assert json.loads(body)["success"] is False


@pytest.mark.parametrize("query", ["?hours=24&days=1", "?hours=&days=1", "?hours=1&days="])
def test_network_usage_conflicting_periods_is_400(usage_http, query):
    get, _ = usage_http
    status, body = get(query)
    assert status == 400, body
    assert json.loads(body)["success"] is False


def test_network_usage_auth_and_no_root_alias(usage_http, monkeypatch):
    get, collector = usage_http

    def forbidden(*args):
        pytest.fail("anonymous dispatch accessed usage")

    monkeypatch.setattr(collector, "snapshot", forbidden)
    assert get(authenticated=False)[0] == 401
    assert get("?hours=1", authenticated=False)[0] == 401
    assert get(path="/network_usage", authenticated=False)[0] == 404
    assert get(method="POST")[0] == 405
