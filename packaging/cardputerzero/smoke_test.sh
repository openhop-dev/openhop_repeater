#!/bin/sh
# Install the built openhop-cz .deb in a clean Debian trixie arm64 container
# (as root) and check what can be checked without the radio: dependencies
# resolve, the config is generated, the daemon and the on-device app load.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DEB=$(ls "$HERE"/build/openhop-cz_*_arm64.deb)
PY=/opt/openhop-cz/venv/bin/python

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends "$DEB" >/dev/null

id openhop >/dev/null
test -L /var/lib/openhop-cz/radio-settings.json
test -x /usr/share/APPLaunch/bin/openhop-cz

# The app lists regions with the system Python; the config is written by the
# bundled one.
REGION=$(python3 /opt/openhop-cz/bin/make_config.py --list | awk -F'\t' '/USA/ {print $1; exit}')
"$PY" /opt/openhop-cz/bin/make_config.py "$REGION"
"$PY" - <<'PY'
import json

import yaml

cfg = yaml.safe_load(open("/etc/openhop-cz/config.yaml"))
assert cfg["radio_type"] == "sx1262", cfg["radio_type"]
assert (cfg["sx1262"]["cs_id"], cfg["sx1262"]["irq_pin"]) == (1, 23), cfg["sx1262"]
assert cfg["sx1262"]["use_dio3_tcxo"] is True and cfg["sx1262"]["dio3_tcxo_voltage"] == 3.0
assert cfg["radio"]["frequency"] == 910525000, cfg["radio"]
assert cfg["storage"]["storage_dir"] == "/var/lib/openhop-cz"
assert json.load(open("/etc/openhop-cz/summary.json"))["region"]
PY

"$PY" -m repeater.main --help >/dev/null
"$PY" -c "import openhop_core.hardware.sx1262_wrapper"

# Render one frame of the on-device app to a file instead of the framebuffer.
OPENHOP_UI_PNG=/tmp/openhop-ui.png OPENHOP_UI_VIEW=main /usr/share/APPLaunch/bin/openhop-cz
test -s /tmp/openhop-ui.png

echo "smoke test passed: $(dpkg-query -W -f '${Package} ${Version}' openhop-cz)"
