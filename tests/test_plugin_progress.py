"""An install or update's progress, from the installer's output to the event stream.

Four layers, one story: the runtime hands each output line to a listener; the
manager keeps a bounded log per plugin and marks how the operation ended; the
IPC exposes it; the web layer streams it as server-sent events the way
/api/update/progress streams the repeater's own updater.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repeater.plugins.ipc import PluginIPCClient, PluginIPCServer
from repeater.plugins.manager import (
    PROGRESS_MAX_LINES,
    OperationProgress,
    PluginManager,
    PluginManagerError,
)
from repeater.plugins.runtime import PluginRuntime
from repeater.plugins.storage import PluginStorage
from repeater.web.plugin_endpoints import PluginAPIEndpoints

from tests.test_plugin_catalogue_install import manager as catalogue_manager  # noqa: F401  (fixture)


# ── the runtime hands over lines ─────────────────────────────────────────────


def test_runtime_streams_installer_output_line_by_line(tmp_path: Path):
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))
    heard: list[str] = []
    runtime.on_output = heard.append
    script = "import sys; print('Collecting demo'); print('Installing collected packages', end=''); sys.stdout.flush()"
    result = runtime._run_install_command([sys.executable, "-c", script], timeout=30)
    assert result.returncode == 0
    assert heard == ["Collecting demo", "Installing collected packages"]
    assert "Collecting demo" in result.stdout


def test_runtime_survives_a_failing_listener(tmp_path: Path):
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))

    def broken(_line: str) -> None:
        raise RuntimeError("listener bug")

    runtime.on_output = broken
    result = runtime._run_install_command([sys.executable, "-c", "print('ok')"], timeout=30)
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_runtime_with_no_listener_is_unchanged(tmp_path: Path):
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))
    result = runtime._run_install_command([sys.executable, "-c", "print('quiet')"], timeout=30)
    assert isinstance(result, subprocess.CompletedProcess)
    assert "quiet" in result.stdout


# ── the manager keeps the log ────────────────────────────────────────────────


def test_progress_log_is_bounded_and_pages_by_cursor():
    log = OperationProgress("openhop.demo", "update")
    for i in range(PROGRESS_MAX_LINES + 20):
        log.append(f"line {i}")
    snap = log.snapshot()
    assert snap["state"] == "running"
    assert len(snap["lines"]) == PROGRESS_MAX_LINES
    assert snap["lines"][0] == "line 20"
    # The cursor counts every line ever appended, so trimming never moves it.
    assert snap["next"] == PROGRESS_MAX_LINES + 20
    # A watcher resumes from the cursor it was given and sees only what is new.
    log.append("tail")
    later = log.snapshot(since=snap["next"])
    assert later["lines"] == ["tail"]
    log.finish()
    assert log.snapshot()["state"] == "complete"
    log.finish("boom")
    assert log.snapshot()["error"] == "boom"


def test_manager_reports_idle_before_any_operation(tmp_path: Path):
    mgr = PluginManager(PluginStorage(tmp_path / "plugins"))
    assert mgr.progress("openhop.demo") == {
        "id": "openhop.demo",
        "operation": None,
        "state": "idle",
        "error": None,
        "lines": [],
        "next": 0,
        "started": None,
        "finished": None,
    }


def test_catalogue_install_writes_its_story_and_completes(catalogue_manager: PluginManager):  # noqa: F811
    result = catalogue_manager.install_from_catalogue("openhop.demo")
    assert result["version"] == "0.1.0"
    snap = catalogue_manager.progress("openhop.demo")
    assert snap["operation"] == "install"
    assert snap["state"] == "complete"
    assert snap["error"] is None
    assert any(line.startswith("Downloading openhop.demo") for line in snap["lines"])
    assert snap["next"] == len(snap["lines"])


def test_a_failed_update_is_recorded_with_its_reason(tmp_path: Path):
    mgr = PluginManager(PluginStorage(tmp_path / "plugins"))
    with pytest.raises(PluginManagerError):
        mgr.update_plugin("missing.plugin")
    snap = mgr.progress("missing.plugin")
    assert snap["operation"] == "update"
    assert snap["state"] == "error"
    assert "plugin not found" in snap["error"]


def test_the_listener_is_removed_after_the_operation(tmp_path: Path):
    mgr = PluginManager(PluginStorage(tmp_path / "plugins"))
    with pytest.raises(PluginManagerError):
        mgr.update_plugin("missing.plugin")
    assert mgr.runtime.on_output is None


# ── the IPC exposes it ───────────────────────────────────────────────────────


def test_ipc_round_trips_progress(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    manager = PluginManager(storage, PluginRuntime(storage, popen_factory=lambda *a, **k: None))
    sock_path = Path(f"/tmp/oh-pm-progress-{tmp_path.name}.sock")
    server = PluginIPCServer(sock_path, manager)
    server.start()
    try:
        client = PluginIPCClient(sock_path)
        assert client.progress("openhop.demo")["state"] == "idle"
        with pytest.raises(PluginManagerError):
            manager.update_plugin("openhop.demo")
        snap = client.progress("openhop.demo", since=0)
        assert snap["state"] == "error"
        assert snap["operation"] == "update"
    finally:
        server.stop()


# ── the web layer streams it ─────────────────────────────────────────────────


def _events(api: PluginAPIEndpoints, plugin_id: str, snapshots: list[dict], *, ticks=None):
    """Drive the generator with a client that answers the snapshots in order."""
    answers = iter(snapshots)

    def progress(_id, since=0):
        try:
            return next(answers)
        except StopIteration:
            return snapshots[-1]

    client = SimpleNamespace(progress=progress)
    clock = iter(ticks or [0.0] * 1000)
    out: list[dict] = []
    with patch.object(api, "_client_or_raise", return_value=client):
        for chunk in api._progress_events(
            plugin_id, 0, sleep=lambda _s: None, clock=lambda: next(clock)
        ):
            assert chunk.startswith("data: ") and chunk.endswith("\n\n")
            out.append(json.loads(chunk[len("data: ") : -2]))
    return out


def test_stream_plays_lines_status_and_one_done(tmp_path: Path):
    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    running = {
        "state": "running",
        "operation": "update",
        "lines": ["Downloading demo 0.2.0"],
        "next": 1,
        "error": None,
    }
    more = {
        "state": "running",
        "operation": "update",
        "lines": ["Collecting demo", "Successfully installed demo-0.2.0"],
        "next": 3,
        "error": None,
    }
    done = {"state": "complete", "operation": "update", "lines": [], "next": 3, "error": None}
    events = _events(api, "openhop.demo", [running, more, done])
    assert [e["type"] for e in events] == [
        "connected",
        "line",
        "status",
        "line",
        "line",
        "status",
        "done",
    ]
    assert [e["line"] for e in events if e["type"] == "line"] == [
        "Downloading demo 0.2.0",
        "Collecting demo",
        "Successfully installed demo-0.2.0",
    ]
    assert events[2] == {"type": "status", "state": "running", "operation": "update"}
    assert events[-1] == {"type": "done", "state": "complete", "error": None}


def test_stream_reports_a_failed_operation_and_ends(tmp_path: Path):
    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    failed = {
        "state": "error",
        "operation": "update",
        "lines": ["pip failed"],
        "next": 1,
        "error": "update install failed: pip",
    }
    events = _events(api, "openhop.demo", [failed])
    assert events[-1] == {"type": "done", "state": "error", "error": "update install failed: pip"}
    assert events[-2]["type"] == "status"


def test_stream_keeps_alive_while_idle_then_times_out(tmp_path: Path):
    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    idle = {"state": "idle", "operation": None, "lines": [], "next": 0, "error": None}
    # The clock is read once for the deadline, then once per poll: twelve quiet polls (a keepalive every
    # fourth, after the first poll's status event) and then a reading past the budget.
    ticks = [0.0] + [1.0] * 12 + [10_000.0]
    events = _events(api, "openhop.demo", [idle], ticks=ticks)
    types = [e["type"] for e in events]
    assert types[0:2] == ["connected", "status"]
    assert types.count("keepalive") == 2
    assert events[-1] == {"type": "done", "state": "timeout", "error": "progress stream timed out"}


def test_stream_ends_when_the_manager_is_unavailable(tmp_path: Path):
    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    events = []
    for chunk in api._progress_events("openhop.demo", 0, sleep=lambda _s: None):
        events.append(json.loads(chunk[len("data: ") : -2]))
    assert events[0]["type"] == "connected"
    assert events[-1]["type"] == "done"
    assert events[-1]["state"] == "error"
    assert "unavailable" in events[-1]["error"].lower()
