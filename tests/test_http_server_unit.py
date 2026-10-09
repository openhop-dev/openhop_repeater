import io
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.web import http_server as hs


def test_log_buffer_emit_collects_messages():
    buf = hs.LogBuffer(max_lines=2)
    rec1 = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    rec2 = logging.LogRecord("x", logging.ERROR, __file__, 2, "boom", (), None)
    rec3 = logging.LogRecord("x", logging.WARNING, __file__, 3, "warn", (), None)

    buf.emit(rec1)
    buf.emit(rec2)
    buf.emit(rec3)

    assert len(buf.logs) == 2
    assert buf.logs[-1]["level"] == "WARNING"
    assert "warn" in buf.logs[-1]["message"]


def test_log_buffer_emit_redacts_sensitive_values():
    buf = hs.LogBuffer(max_lines=5)
    rec = logging.LogRecord(
        "auth",
        logging.DEBUG,
        __file__,
        10,
        "auth password=secret123 token=abc123 Authorization: Bearer deadbeef",
        (),
        None,
    )

    buf.emit(rec)

    assert len(buf.logs) == 1
    entry = buf.logs[0]
    assert "secret123" not in entry["message"]
    assert "abc123" not in entry["message"]
    assert "deadbeef" not in entry["message"]
    assert "[REDACTED]" in entry["message"]
    assert "raw_message" not in entry


def test_log_buffer_emit_includes_exception_text_without_crashing():
    buf = hs.LogBuffer(max_lines=5)
    try:
        raise RuntimeError("boom password=secret123")
    except RuntimeError:
        rec = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            20,
            "failure while sending advert",
            (),
            sys.exc_info(),
        )

    buf.emit(rec)

    assert len(buf.logs) == 1
    assert "exception" in buf.logs[0]
    assert "RuntimeError" in buf.logs[0]["exception"]
    assert "secret123" not in buf.logs[0]["exception"]


def test_doc_endpoint_routes_and_openapi_json_paths(monkeypatch):
    api = SimpleNamespace(docs=lambda: "docs-html")
    doc = hs.DocEndpoint(api)

    assert doc.index() == "docs-html"
    assert doc.docs() == "docs-html"

    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )

    # success path
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO("openapi: 3.0.0\n"))
    out = doc.openapi_json()
    assert cherrypy.response.headers["Content-Type"] == "application/json"
    assert b"openapi" in out

    # not found
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", _missing)
    out = doc.openapi_json()
    assert cherrypy.response.status == 404
    assert b"not found" in out

    # generic error
    def _err(*_args, **_kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr("builtins.open", _err)
    out = doc.openapi_json()
    assert cherrypy.response.status == 500
    assert b"Error loading OpenAPI spec" in out


def test_stats_app_index_and_default_routing(monkeypatch, tmp_path):
    index_html = tmp_path / "index.html"
    index_html.write_text("<html>ok</html>", encoding="utf-8")

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    assert app.index() == "<html>ok</html>"

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="OPTIONS"), raising=False)
    assert app.default("anything") == ""

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    with pytest.raises(cherrypy.NotFound):
        app.default("api")

    assert app.default("ws", "packets") == ""
    assert app.default("plugins") == "<html>ok</html>"
    assert app.default("route") == "<html>ok</html>"


def test_stats_app_exposes_compiled_ui_favicon(monkeypatch, tmp_path):
    favicon = b"compiled-ui-favicon"
    (tmp_path / "favicon.ico").write_bytes(favicon)

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}), raising=False)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    assert app.favicon_ico() == favicon
    assert cherrypy.response.headers["Content-Type"] == "image/x-icon"


def test_stats_app_index_error_paths(monkeypatch, tmp_path):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    with pytest.raises(cherrypy.HTTPError):
        app.index()

    # Force generic open() exception branch
    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("builtins.open", _explode)
    (tmp_path / "index.html").write_text("ignored", encoding="utf-8")
    with pytest.raises(cherrypy.HTTPError):
        app.index()


def test_stats_app_resolves_legacy_plugin_web_path(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    plugin_id = "waev.outpost"
    release = plugins_root / plugin_id / "releases" / "0.9.400"
    ui = release / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>plugin</html>", encoding="utf-8")
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": "Outpost",
        "version": "0.9.400",
        "description": "Outpost UI",
        "ui": {"type": "application", "entry": "ui/index.html"},
    }
    (release / "openhop-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugins_root / plugin_id / "state.json").write_text(
        json.dumps({"id": plugin_id, "version": "0.9.400", "enabled": True, "source": "catalogue"}),
        encoding="utf-8",
    )
    (plugins_root / plugin_id / "current").symlink_to(Path("releases/0.9.400"))

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    legacy_cfg_path = plugins_root / plugin_id / "releases" / "0.9.386" / "ui"
    app = hs.StatsApp(
        config={"plugins": {"root": str(plugins_root)}, "web": {"web_path": str(legacy_cfg_path)}}
    )

    resolved = app._resolve_html_dir()
    assert resolved.endswith(f"{plugin_id}/current/ui")


def test_http_server_utility_methods(monkeypatch, tmp_path):
    def _fake_init_auth(self):
        self.jwt_handler = object()
        self.token_manager = object()

    monkeypatch.setattr(hs.HTTPStatsServer, "_init_auth_handlers", _fake_init_auth)
    monkeypatch.setattr(
        hs,
        "StatsApp",
        lambda *args, **kwargs: SimpleNamespace(api=SimpleNamespace(config_manager=object())),
    )
    monkeypatch.setattr(hs, "AuthEndpoints", lambda *args, **kwargs: object())
    monkeypatch.setattr(hs, "DocEndpoint", lambda *_args, **_kwargs: object())

    server = hs.HTTPStatsServer(
        config={"web": {"cors_enabled": False}}, config_path=str(Path(tmp_path) / "cfg.yml")
    )

    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}), raising=False)
    out = server._json_error_handler(401, "no", "", "")
    assert '"success": false' in out

    install_called = {"v": False}
    monkeypatch.setattr(hs.cherrypy_cors, "install", lambda: install_called.__setitem__("v", True))
    server._setup_server_cors()
    assert install_called["v"] is True

    exited = {"v": False}
    monkeypatch.setattr(
        cherrypy,
        "engine",
        SimpleNamespace(exit=lambda: exited.__setitem__("v", True)),
        raising=False,
    )
    server.stop()
    assert exited["v"] is True


def test_cors_response_headers_allow_bearer_preflight_without_credentials():
    headers = dict(hs._cors_response_headers())

    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Credentials" not in headers


def _csp_app(monkeypatch, config):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}), raising=False)
    return hs.StatsApp(config=config)


def test_bundled_ui_document_carries_default_csp(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    app = _csp_app(monkeypatch, config={})
    # Point the *default* frontend at a temp bundle so the test needs no build.
    app.default_html_dir = str(tmp_path)

    assert app.index() == "<html>ok</html>"
    csp = cherrypy.response.headers["Content-Security-Policy"]
    assert csp == hs.StatsApp._DEFAULT_CONTENT_SECURITY_POLICY
    directives = dict(part.strip().split(" ", 1) for part in csp.split(";"))
    # The point of the policy: mesh-supplied text rendered as HTML must not run.
    assert directives["script-src"] == "'self'"
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'self'"
    # What the bundled UI legitimately needs still works.
    assert "https://fonts.googleapis.com" in directives["style-src"]
    assert "blob:" in directives["worker-src"]
    assert "https:" in directives["img-src"]
    assert "https:" in directives["connect-src"]

    # Client-side routes serve the same document with the same header.
    cherrypy.response.headers.clear()
    assert app.default("neighbors") == "<html>ok</html>"
    assert cherrypy.response.headers["Content-Security-Policy"] == csp


def test_custom_web_path_frontend_gets_no_csp_unless_configured(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html>custom</html>", encoding="utf-8")

    app = _csp_app(monkeypatch, config={"web": {"web_path": str(tmp_path)}})
    assert app.index() == "<html>custom</html>"
    assert "Content-Security-Policy" not in cherrypy.response.headers

    policy = "default-src 'self'; script-src 'self' 'unsafe-inline'"
    app = _csp_app(
        monkeypatch,
        config={"web": {"web_path": str(tmp_path), "content_security_policy": policy}},
    )
    assert app.index() == "<html>custom</html>"
    assert cherrypy.response.headers["Content-Security-Policy"] == policy


def test_csp_config_overrides_and_disables(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    app = _csp_app(
        monkeypatch, config={"web": {"content_security_policy": "  script-src 'self'  "}}
    )
    app.default_html_dir = str(tmp_path)
    app.index()
    assert cherrypy.response.headers["Content-Security-Policy"] == "script-src 'self'"

    for disabled in (False, "", "   "):
        app = _csp_app(monkeypatch, config={"web": {"content_security_policy": disabled}})
        app.default_html_dir = str(tmp_path)
        app.index()
        assert "Content-Security-Policy" not in cherrypy.response.headers, repr(disabled)
