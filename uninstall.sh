#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must be run as root (use sudo)" >&2
    exit 1
fi

echo "→ stopping and disabling service"
systemctl disable --now scyroxd 2>/dev/null || true

echo "→ removing files"
rm -f /usr/local/bin/scyroxd
rm -f /etc/systemd/system/scyroxd.service
rm -f /etc/modules-load.d/scyroxd.conf
# Also clean up any leftovers from older user-service installs.
rm -f /etc/systemd/user/scyroxd.service
rm -f /etc/udev/rules.d/70-scyroxd.rules

systemctl daemon-reload
udevadm control --reload-rules 2>/dev/null || true

echo
echo "Uninstalled."
