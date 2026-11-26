#!/bin/sh
# Build the openhop-cz .deb for the M5Stack CardputerZero app store.
#
# Run on arm64 Debian trixie (the CardputerZero's OS): the package bundles a
# Python runtime with modules compiled for that platform. Any of these work:
#
#   on a CardputerZero:   packaging/cardputerzero/build_deb.sh
#   in Docker (arm64):    docker run --rm -v "$PWD:/src" -w /src debian:trixie \
#                           sh -c 'packaging/cardputerzero/ci-deps.sh && packaging/cardputerzero/build_deb.sh'
#
# Environment:
#   REVISION    Debian revision appended to the openHop version (default 1)
#   MAINTAINER  Maintainer field of the package
#   VERSION     full package version, overriding the one derived from git
set -eu

PKG=openhop-cz
REVISION=${REVISION:-1}
MAINTAINER=${MAINTAINER:-"Rightup <rightup@pymc.dev>"}

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT="$HERE/build"
STAGE="$OUT/stage"
APP="$STAGE/opt/$PKG"

[ "$(dpkg --print-architecture)" = arm64 ] || { echo "build on arm64 Debian (see the header of this script)" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$APP/bin" "$APP/share" "$STAGE/DEBIAN"

echo "==> bundling Python runtime from $REPO"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --prefer-binary --disable-pip-version-check "${REPO}[hardware]"
"$APP/venv/bin/python" -c "import repeater, openhop_core, spidev, periphery, yaml"

if [ -z "${VERSION:-}" ]; then
    # 1.2.0 -> 1.2.0-1; a development build 1.2.1.dev7 -> 1.2.1~dev7-1, which
    # sorts before the 1.2.1 release.
    UPSTREAM=$("$APP/venv/bin/python" -c "from importlib.metadata import version; print(version('openhop_repeater'))")
    VERSION="$(echo "$UPSTREAM" | sed 's/\.dev/~dev/')-$REVISION"
fi
PYMM=$("$APP/venv/bin/python" -c "import sys; print('%d.%d' % sys.version_info[:2])")
PYNEXT=$("$APP/venv/bin/python" -c "import sys; print('%d.%d' % (sys.version_info[0], sys.version_info[1] + 1))")

# The service calls venv/bin/python directly, so pip and the console scripts
# (whose shebangs point at the build directory) are dead weight.
"$APP/venv/bin/pip" uninstall --quiet --yes pip >/dev/null 2>&1 || true
find "$APP/venv/bin" -type f -delete
find "$APP/venv" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$APP/venv" -type d \( -name tests -o -name test \) -path "*/site-packages/*" -prune -exec rm -rf {} +

echo "==> staging app files"
install -m 0755 "$HERE/app/cap_lora_power.py" "$HERE/app/make_config.py" "$HERE/app/openhop_ui.py" "$APP/bin/"
install -m 0755 "$HERE/files/autostart" "$APP/bin/autostart"
install -m 0644 "$REPO/config.yaml.example" "$REPO/radio-presets.json" "$APP/share/"
# The setup wizard lists every entry of radio-settings.json; on this device
# only the Cap LoRa-1262 one applies.
python3 - "$REPO/radio-settings.json" "$APP/share/radio-settings.json" <<'PY'
import json
import sys

key = "cardputerzero-cap-lora-1262"
with open(sys.argv[1]) as f:
    entry = json.load(f)["hardware"][key]
with open(sys.argv[2], "w") as f:
    json.dump({"hardware": {key: entry}}, f, indent=2)
    f.write("\n")
PY

install -D -m 0755 "$HERE/files/openhop-cz.launcher" "$STAGE/usr/share/APPLaunch/bin/$PKG"
install -D -m 0644 "$HERE/files/openhop-cz.desktop" "$STAGE/usr/share/APPLaunch/applications/$PKG.desktop"
install -D -m 0644 "$HERE/assets/icon-launcher.png" "$STAGE/usr/share/APPLaunch/share/images/$PKG.png"

for unit in openhop-cz.service openhop-cz-boot.service openhop-cz-setup@.service openhop-cz-autostart@.service; do
    install -D -m 0644 "$HERE/files/$unit" "$STAGE/usr/lib/systemd/system/$unit"
done
install -D -m 0644 "$HERE/files/49-openhop-cz.rules" "$STAGE/usr/share/polkit-1/rules.d/49-openhop-cz.rules"
install -D -m 0644 "$HERE/files/70-openhop-cz.rules" "$STAGE/usr/lib/udev/rules.d/70-openhop-cz.rules"

DOC="$STAGE/usr/share/doc/$PKG"
mkdir -p "$DOC"
{
    echo "openhop-cz packages openHop Repeater for the M5Stack CardputerZero."
    echo "Source: https://github.com/openhop-dev/openhop_repeater"
    echo
    echo "openhop_repeater and openhop_core (MIT):"
    echo
    cat "$REPO/LICENSE"
    echo
    echo "Other bundled Python packages keep their own licenses; see the"
    echo "*.dist-info directories under /opt/$PKG/venv/lib/."
} > "$DOC/copyright"

sed -i "/^command = /d" "$APP/venv/pyvenv.cfg"
chmod -R go-w "$STAGE"
install -m 0755 "$HERE/maintscripts/postinst" "$HERE/maintscripts/prerm" "$HERE/maintscripts/postrm" "$STAGE/DEBIAN/"
SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VERSION
Architecture: arm64
Maintainer: $MAINTAINER
Installed-Size: $SIZE
Depends: python3 (>= $PYMM), python3 (<< $PYNEXT), python3-pil, python3-numpy, fonts-dejavu-core, adduser, polkitd, systemd
Section: net
Priority: optional
Homepage: https://github.com/openhop-dev/openhop_repeater
Description: openHop MeshCore repeater for CardputerZero + Cap LoRa-1262
 Runs the openHop Repeater daemon on an M5Stack CardputerZero using the
 SX1262 radio in the Cap LoRa-1262, with an on-device control panel to
 start and stop it, pick a radio region and find the web dashboard.
 .
 The repeater runs as the unprivileged "openhop" user and is off until
 you start it from the openHop app.
CTRL

DEB="$OUT/${PKG}_${VERSION}_arm64.deb"
rm -f "$OUT"/*.deb
dpkg-deb --root-owner-group -Zxz -b "$STAGE" "$DEB" >/dev/null
echo "==> built $DEB ($(du -h "$DEB" | cut -f1))"
