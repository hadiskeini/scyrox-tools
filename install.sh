#!/bin/bash
# Install scyroxd system-wide. Works on Arch, Fedora, Bazzite, Ubuntu —
# anywhere that has systemd + udev + a writable /etc and /usr/local.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must be run as root (use sudo)" >&2
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"

# Migrate from a previous user-service install, if present.
if [[ -e /etc/systemd/user/scyroxd.service ]]; then
    echo "→ removing previous user-service install at /etc/systemd/user/"
    rm -f /etc/systemd/user/scyroxd.service
fi
if [[ -e /etc/udev/rules.d/70-scyroxd.rules ]]; then
    echo "→ removing previous udev rule (no longer needed)"
    rm -f /etc/udev/rules.d/70-scyroxd.rules
    udevadm control --reload-rules
fi

# The daemon now imports the `scyrox` package, so install the package into a
# self-contained venv and symlink its console scripts into /usr/local/bin.
# (NixOS users: ignore this script — use modules/scyrox in your config instead.)
VENV=/usr/local/lib/scyrox-venv
echo "→ installing scyrox package into $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$DIR[all]"   # + textual (TUI) + hid (cross-platform)
for bin in scyroxd scyrox scyrox-tui; do
    ln -sf "$VENV/bin/$bin" "/usr/local/bin/$bin"
    echo "→ /usr/local/bin/$bin"
done

echo "→ /etc/systemd/system/scyroxd.service"
install -m 644 "$DIR/scyroxd.service" /etc/systemd/system/

echo "→ /etc/modules-load.d/scyroxd.conf"
echo uhid > /etc/modules-load.d/scyroxd.conf

echo "→ loading uhid module now"
modprobe uhid 2>/dev/null || true

systemctl daemon-reload

echo
echo "Installed. To enable and start the service:"
echo "    sudo systemctl enable --now scyroxd"
echo
echo "Verify:"
echo "    systemctl status scyroxd"
echo "    journalctl -u scyroxd -f"
echo
echo "Then check Settings → Power → Devices for 'Scyrox V6'."
