#!/bin/sh
# Install what build_deb.sh needs in a fresh Debian trixie container (as root).
# A CardputerZero already has all of this.
set -eu

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates git python3 python3-venv python3-dev build-essential xz-utils >/dev/null

# The checkout is mounted from the host and owned by another user; the version
# of the bundled openHop comes from its git tags.
git config --global --add safe.directory '*'
