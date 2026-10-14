#!/usr/bin/env bash
# Keep the kiosk tablet's 127.0.0.1:8600 forwarded to this PC's panel over USB.
#
#   tablet-link.sh            run forever (the tablet-link systemd service)
#   tablet-link.sh reload     make the kiosk reload the panel now, e.g. after
#                             a deploy restarted vacuum-lab-web in under 15 s
#   tablet-link.sh status     what is connected and forwarded
#
# `adb reverse` lasts only as long as the USB connection, so this re-adds it
# every time the tablet (re)appears: a replug, a tablet reboot, an adb server
# restart. Polling every 3 s costs nothing and has no udev timing to get wrong.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB="${ADB:-$HERE/platform-tools/adb}"
PORT="${PORT:-8600}"
KIOSK="lab.vacuum.kiosk"

# The key the tablet authorised during provisioning. In lock task mode the
# tablet may not be able to show the "Allow USB debugging?" prompt for a new
# computer, so this PC presents that same key instead of its own.
[[ -f "$HERE/adbkey" ]] && export ADB_VENDOR_KEYS="$HERE/adbkey"

devices() { "$ADB" devices | awk -v state="$1" 'NR>1 && $2==state {print $1}'; }

case "${1:-run}" in
    reload)
        for serial in $(devices device); do
            "$ADB" -s "$serial" shell am broadcast -n "$KIOSK/.ConfigReceiver" \
                -a "$KIOSK.CONFIGURE" >/dev/null && echo "reloaded $serial"
        done
        exit 0 ;;
    status)
        "$ADB" devices -l
        for serial in $(devices device); do
            echo "$serial: $("$ADB" -s "$serial" reverse --list | tr -d '\r')"
        done
        exit 0 ;;
    run) ;;
    *) echo "usage: $0 [run|reload|status]" >&2; exit 2 ;;
esac

"$ADB" start-server
declare -A said=()
while true; do
    for serial in $(devices device); do
        if ! "$ADB" -s "$serial" reverse --list 2>/dev/null | grep -q "tcp:$PORT tcp:$PORT"; then
            if "$ADB" -s "$serial" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
                echo "forwarding the panel (port $PORT) to $serial"
                said[$serial]=forwarded
            elif [[ "${said[$serial]:-}" != failed ]]; then
                echo "could not forward to $serial; will keep trying"
                said[$serial]=failed
            fi
        fi
    done
    for serial in $(devices unauthorized); do
        if [[ "${said[$serial]:-}" != unauthorized ]]; then
            echo "$serial is connected but has not authorised this PC's adb key" \
                 "(see README: copy the provisioning key to $HERE/adbkey)"
            said[$serial]=unauthorized
        fi
    done
    sleep 3
done
