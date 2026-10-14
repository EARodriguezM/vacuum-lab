#!/usr/bin/env bash
# Install the tablet USB link on the lab PC, from this laptop.
#
#   labpc/install.sh esarodriguezm@172.16.2.69
#
# Copies platform-tools (the lab PC is offline and has no adb), the link
# script and this laptop's adb key -- the one the tablet authorised during
# provisioning -- into ~/vacuum-lab-tablet/, then installs the udev rule and
# the tablet-link service with sudo (passwordless on the lab PC).
# Safe to re-run; it replaces what it installed before.
set -euo pipefail

HOST="${1:?usage: labpc/install.sh user@lab-pc}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TC="${TOOLCHAIN:-$HERE/../.toolchain}"
KEY="${ADB_KEY:-$HOME/.android/adbkey}"

[[ -x "$TC/platform-tools/adb" ]] || { echo "run ./build.sh first: it fetches platform-tools" >&2; exit 1; }
[[ -f "$KEY" ]] || { echo "no adb key at $KEY: provision the tablet from this laptop first" >&2; exit 1; }

echo "== copying to $HOST:~/vacuum-lab-tablet/"
ssh "$HOST" 'mkdir -p ~/vacuum-lab-tablet && chmod 700 ~/vacuum-lab-tablet'
rsync -a "$TC/platform-tools" "$HERE/tablet-link.sh" "$HOST:vacuum-lab-tablet/"
rsync -a --chmod=600 "$KEY" "$HOST:vacuum-lab-tablet/adbkey"
rsync -a "$HERE/tablet-link.service" "$HERE/51-vacuum-lab-tablet.rules" "$HOST:vacuum-lab-tablet/"

echo "== installing the udev rule and the service"
ssh "$HOST" 'set -e
    cd ~/vacuum-lab-tablet
    sed -e "s|@USER@|$(id -un)|g" -e "s|@HOME@|$HOME|g" tablet-link.service > tablet-link.service.out
    sudo install -m 644 51-vacuum-lab-tablet.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb --action=add
    sudo install -m 644 tablet-link.service.out /etc/systemd/system/tablet-link.service
    rm tablet-link.service.out
    sudo systemctl daemon-reload
    sudo systemctl enable tablet-link >/dev/null 2>&1
    sudo systemctl restart tablet-link
    sleep 4
    systemctl --no-pager --lines=8 status tablet-link || true
    ~/vacuum-lab-tablet/tablet-link.sh status || true'
