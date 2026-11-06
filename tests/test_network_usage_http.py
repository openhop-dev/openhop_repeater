"""Real CherryPy authenticated query dispatch, backed by temporary SQLite."""

import json
import threading
from http.client import HTTPConnection
from types import SimpleNamespace
from wsgiref.simple_server import WSGIRequestHandler, make_server

import cherrypy
import pytest

from repeater.data_acquisition.network_usage import NetworkUsageCollector
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.auth.cherrypy_tool import register_require_auth_tool


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


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


def test_network_usage_auth_and_no_root_alias(usage_http, monkeypatch):
    get, collector = usage_http

    def forbidden(*args):
        pytest.fail("anonymous dispatch accessed usage")

    monkeypatch.setattr(collector, "snapshot", forbidden)
    assert get(authenticated=False)[0] == 401
    assert get(path="/network_usage", authenticated=False)[0] == 404
    assert get(method="POST")[0] == 405
