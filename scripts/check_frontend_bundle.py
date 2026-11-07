#!/usr/bin/env python3
"""Record/verify the standalone UI build shipped in backend packages (no Node/network)."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess  # nosec B404 - maintainer-only CLI uses read-only Git commands, no shell
from pathlib import Path

REPOSITORY = "https://github.com/openhop-dev/openHop_RepeaterUI"
MANIFEST = "frontend-build.json"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "repeater/web/html"


def file_digests(root):
    """Inventory the entire generated output, excluding its self-describing manifest."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Frontend symlink is not a generated asset: {path}")
        if path.is_file() and path != root / MANIFEST:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def record_source(root, source):
    """Record a clean source checkout after the maintainer has built/staged its output."""

    git_path = shutil.which("git")
    if git_path is None:
        raise ValueError("Git is required to record UI source provenance")

    def git(*args):
        # Fixed read-only subcommands; the checkout is an argv operand, never shell text.
        return subprocess.check_output(  # nosec B603
            [git_path, "-C", str(source), *args], text=True
        ).strip()

    if git("status", "--porcelain"):
        raise ValueError("Build provenance requires a clean RepeaterUI source checkout")
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full UI source commit")
    manifest = {
        "schema_version": 1,
        "source": {
            "repository": REPOSITORY,
            "commit": commit,
            "package_lock_sha256": hashlib.sha256(
                (source / "package-lock.json").read_bytes()
            ).hexdigest(),
        },
        "files": file_digests(root),
    }
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def check_bundle(root):
    """Fail closed on missing, stale or additional generated assets."""
    manifest = json.loads((root / MANIFEST).read_text())
    source = manifest["source"]
    if manifest["schema_version"] != 1 or source["repository"] != REPOSITORY:
        raise ValueError("Unsupported frontend provenance")
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("Missing full RepeaterUI source commit")
    if not re.fullmatch(r"[0-9a-f]{64}", source["package_lock_sha256"]):
        raise ValueError("Missing UI lockfile digest")
    actual = file_digests(root)
    if not actual or actual != manifest["files"]:
        raise ValueError(
            "Frontend assets differ from the recorded build; rebuild the complete bundle"
        )
    index = (root / "index.html").read_text()
    entries = re.findall(r"""(?:src|href)=["'](/assets/[^"']+)["']""", index)
    if not entries or not any(entry.endswith(".js") for entry in entries):
        raise ValueError("Frontend index has no JavaScript entry point")
    for entry in entries:
        if entry.lstrip("/") not in actual:
            raise ValueError(f"Frontend index references a missing asset: {entry}")
    return source["commit"], len(actual)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="Staged/installed UI directory"
    )
    parser.add_argument(
        "--record-source", type=Path, help="Record the clean UI checkout just built"
    )
    args = parser.parse_args()
    try:
        if args.record_source:
            record_source(args.root, args.record_source)
        commit, count = check_bundle(args.root)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Frontend bundle verification failed: {exc}\n")
    print(f"Verified {count} frontend files from RepeaterUI {commit}")


if __name__ == "__main__":
    main()
