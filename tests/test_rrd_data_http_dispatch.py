"""Exercise RRD query parameters through authenticated CherryPy dispatch."""

import json
import threading
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import Mock
from wsgiref.simple_server import WSGIRequestHandler, make_server

import cherrypy
import pytest

from repeater.web.api_endpoints import APIEndpoints
from repeater.web.auth.cherrypy_tool import register_require_auth_tool


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


@pytest.fixture
def rrd_http(monkeypatch):
    # Direct-handler tests may leave synthetic request/response objects behind.
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
    storage = SimpleNamespace(
        get_metrics_data=Mock(return_value={"timestamps": [1, 2], "metrics": {}})
    )
    api = APIEndpoints.__new__(APIEndpoints)
    api.daemon_instance = SimpleNamespace(repeater_handler=SimpleNamespace(storage=storage))
    app = cherrypy.Application(
        SimpleNamespace(api=api),
        config={"/api": {"tools.require_auth.on": True}},
    )
    with make_server("127.0.0.1", 0, app, handler_class=QuietHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(query="", *, authenticated=True):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                headers = {"X-API-Key": "test-key"} if authenticated else {}
                connection.request("GET", "/api/rrd_data" + query, headers=headers)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        try:
            yield get, storage.get_metrics_data
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_rrd_query_parameters_reach_storage_over_http(rrd_http):
    get, read_metrics = rrd_http
    status, body = get("?start_time=1&end_time=2&resolution=average")
    assert status == 200, body
    assert json.loads(body) == {
        "success": True,
        "data": {"timestamps": [1, 2], "metrics": {}},
    }
    read_metrics.assert_called_once_with(start_time="1", end_time="2", resolution="average")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {"start_time": None, "end_time": None, "resolution": "average"}),
        (
            "?start_time=10&resolution=max",
            {"start_time": "10", "end_time": None, "resolution": "max"},
        ),
    ],
)
def test_rrd_http_preserves_defaults_and_partial_queries(rrd_http, query, expected):
    get, read_metrics = rrd_http
    status, body = get(query)
    assert status == 200, body
    assert json.loads(body)["success"] is True
    read_metrics.assert_called_once_with(**expected)


def test_rrd_http_query_requires_authentication(rrd_http):
    get, read_metrics = rrd_http
    status, _ = get("?start_time=1&end_time=2&resolution=average", authenticated=False)
    assert status == 401
    read_metrics.assert_not_called()


def test_rrd_http_preserves_storage_parameter_errors(rrd_http):
    get, read_metrics = rrd_http
    read_metrics.side_effect = ValueError("invalid time range")
    status, body = get("?start_time=invalid&end_time=2&resolution=average")
    assert status == 200, body
    assert json.loads(body) == {
        "success": False,
        "error": "Invalid parameter format: invalid time range",
    }
    read_metrics.assert_called_once_with(start_time="invalid", end_time="2", resolution="average")
