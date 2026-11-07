"""Regression gates for the frontend actually distributed with the backend."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "repeater/web/html"


def reachable_javascript(bundle):
    """Follow Vite entry, preload and lazy chunk names, not stale unreferenced files."""
    pending = [bundle / "index.html"]
    seen = set()
    scripts = []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        assert path.is_file(), f"Bundled frontend reference is missing: {path.name}"
        seen.add(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            scripts.append(text)
        for name in re.findall(r"[A-Za-z0-9_.-]+\.js", text):
            asset = bundle / "assets" / name
            if asset.is_file() and asset not in seen:
                pending.append(asset)
    return "\n".join(scripts)


def test_distributed_system_stats_includes_merged_network_usage():
    javascript = reachable_javascript(BUNDLE)
    assert "Network and MQTT usage" in javascript, "Backend still ships the old System Stats UI"
    assert "How these totals work" in javascript
    assert "network_usage" in javascript
    for label in ("1 Hour", "6 Hours", "24 Hours", "2 Days", "1 Week"):
        assert label in javascript


def test_bundle_has_verifiable_source_and_file_manifest():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_frontend_bundle.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra", "symlink"])
def test_bundle_checker_rejects_incomplete_or_stale_installation(tmp_path, mutation):
    bundle = tmp_path / "html"
    shutil.copytree(BUNDLE, bundle)
    asset = next((bundle / "assets").glob("SystemStats-*.js"))
    if mutation == "changed":
        asset.write_text("// synthetic stale frontend\n")
    elif mutation == "missing":
        asset.unlink()
    elif mutation == "extra":
        (bundle / "assets/obsolete.js").write_text("// synthetic obsolete chunk\n")
    else:
        asset.unlink()
        asset.symlink_to(BUNDLE / "index.html")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_frontend_bundle.py"), "--root", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Frontend bundle verification failed" in result.stderr
