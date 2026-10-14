#!/usr/bin/env bash
# Turn a freshly wiped Lenovo TB-7305X (Android 9) or TB-8505X (Android 10)
# into the vacuum lab's control tablet.
#
#   ./provision.sh --pin 482613                  # USB first, then Wi-Fi search
#   ./provision.sh --pin 482613 --urls "http://127.0.0.1:8600, http://172.16.2.69:8600, lan"
#   ./provision.sh --keep-google                 # leave Google Play services installed
#   ./provision.sh --undo                        # give the tablet back
#
# Before running it: factory reset, skip every step of the setup wizard (no
# Google account, no screen lock), enable Developer options -> USB debugging,
# plug the tablet in and accept the "Allow USB debugging?" prompt with
# "Always allow from this computer" ticked. README.md has the walkthrough.
#
# Everything here is reversible without root: apps are removed for user 0
# only (`--undo` brings them back with `cmd package install-existing`), and
# a factory reset returns the tablet to stock. Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TC="${TOOLCHAIN:-$HERE/.toolchain}"
ADB="${ADB:-$TC/platform-tools/adb}"
[[ -x "$ADB" ]] || ADB="$(command -v adb || true)"
APK="$HERE/dist/vacuum-lab-kiosk.apk"
CHROME_DIR="$(ls -d "$HERE"/vendor/chrome-* 2>/dev/null | sort -V | tail -1 || true)"
WEBVIEW_DIR="$(ls -d "$HERE"/vendor/webview-* 2>/dev/null | sort -V | tail -1 || true)"
PKG="lab.vacuum.kiosk"
ADMIN="$PKG/.AdminReceiver"

PIN=""
URLS=""
KEEP_GOOGLE=0
UNDO=0
BRIGHTNESS=170          # of 255: readable under lab lighting, not a torch
DENSITY=""              # per model once the tablet is known; see README

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pin) PIN="$2"; shift 2 ;;
        --urls) URLS="$2"; shift 2 ;;
        --keep-google) KEEP_GOOGLE=1; shift ;;
        --brightness) BRIGHTNESS="$2"; shift 2 ;;
        --density) DENSITY="$2"; shift 2 ;;
        --undo) UNDO=1; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# --- what goes ---------------------------------------------------------------
#
# Removed for user 0 only. Kept on purpose: Chrome (on Android 9 it IS the
# web engine), Android System WebView (the engine on Android 10), Gboard
# (typing setpoints and the token), Settings, SystemUI, the package
# installer, the stock launcher, telephony and every provider. Packages a
# tablet doesn't have are skipped, so one list serves both models.

BLOAT=(
    # Lenovo / Huaqin extras: weather, "what's new", telemetry, rescue agent,
    # retail demo, OTA nags, privacy pop-up
    com.tblenovo.lewea com.tblenovo.whatsnewclient com.tblenovo.whatsnewhost
    com.tblenovo.wallpaper com.lenovo.ota com.lenovo.lsf com.lenovo.lsf.device
    com.lenovo.ue.device com.lmsa.app.lmsapad com.motorola.demo
    com.huaqin.lenovoprivacy com.huaqin.apphide com.lenovo.capsensortest com.yha.factory
    com.yandex.browser.lite ru.yandex.searchplugin
    jp.co.fsi.fskaren.lenovo jp.co.fsi.fskaren.lenovo.theme.aqua
    jp.co.fsi.fskaren.lenovo.theme.wood jp.co.fsi.fskaren.lenovo.theme.stylish
    jp.co.fsi.fskaren.lenovo.theme.sf jp.co.fsi.fskaren.lenovo.theme.decoCute
    jp.co.fsi.fskaren.lenovo.theme.decoKira jp.co.fsi.fskaren.lenovo.theme.pretty
    jp.co.fsi.fskaren.lenovo.theme.toyBox jp.co.fsi.fskaren.lenovo.theme.japaneseStyle
    # Google "Go" apps
    com.google.android.apps.assistant com.google.android.apps.mapslite
    com.google.android.apps.navlite com.google.android.apps.photos
    com.google.android.apps.searchlite com.google.android.apps.tachyon
    com.google.android.apps.messaging com.google.android.apps.wellbeing
    com.google.android.apps.restore com.google.android.calendar
    com.google.android.gm.lite com.google.android.music com.google.android.youtube
    com.google.android.feedback com.google.android.marvin.talkback
    com.google.android.printservice.recommendation
    com.google.android.apps.speechservices com.google.android.tts
    # AOSP / MediaTek apps with no place on a control panel. mtklogger can
    # fill storage with logs on its own.
    com.android.calculator2 com.android.deskclock com.android.contacts com.android.egg
    com.android.protips com.android.dreams.basic com.android.bips com.android.printspooler
    com.android.musicfx com.android.wallpapercropper com.android.bookmarkprovider
    com.android.providers.partnerbookmarks com.android.fmradio com.android.stk
    com.android.cellbroadcastreceiver com.mediatek.camera com.mediatek.callrecorder
    com.mediatek.mtklogger com.dolby.daxappui
    # TB-8505X (full Google apps, not Go). DebugLoggerUI is mtklogger's
    # successor; DuraSpeed is MediaTek's background-app killer.
    com.tblenovo.center com.tblenovo.lenovowhatsnew com.tblenovo.soundrecorder
    com.google.android.apps.docs com.google.android.apps.maps
    com.google.android.apps.nbu.files com.google.android.apps.youtube.music
    com.google.android.calculator com.google.android.gm com.google.android.keep
    com.google.android.googlequicksearchbox com.google.android.videos
    com.google.android.projection.gearhead com.google.android.dialer
    com.google.android.gms.location.history
    com.debug.loggerui com.mediatek.duraspeed
)

# Google Play services and friends: about 300 MB of the tablet's 1 GB while
# running, and useless offline with no account. Chrome and its WebView do
# not need them.
GOOGLE=(
    com.google.android.gms com.google.android.gsf com.android.vending
    com.google.android.gms.policy_sidecar_aps com.google.android.partnersetup
    com.google.android.syncadapters.contacts com.google.android.backuptransport
    com.google.android.configupdater com.google.android.onetimeinitializer
)

# --- helpers -------------------------------------------------------------------

say() { printf '\n== %s\n' "$*"; }
# adb shell joins its arguments into one remote command line, so quote each
# one for the tablet's shell: the server list has spaces and commas in it.
quote() { local out="" arg; for arg in "$@"; do out+="'${arg//\'/\'\\\'\'}' "; done; printf '%s' "$out"; }
run() { "$ADB" shell "$(quote "$@")" | tr -d '\r'; }

device() {
    [[ -n "$ADB" ]] || { echo "adb not found: run ./build.sh once (it fetches platform-tools)" >&2; exit 1; }
    "$ADB" start-server >/dev/null
    local devices
    devices="$("$ADB" devices | awk 'NR>1 && $2!="" {print $1" "$2}')"
    if [[ -z "$devices" ]]; then
        echo "No tablet found. Enable USB debugging and plug it in." >&2; exit 1
    fi
    if grep -q unauthorized <<<"$devices"; then
        echo "The tablet has not authorised this computer yet: tap Allow on its screen" >&2
        echo "(tick 'Always allow from this computer'), then run this again." >&2
        exit 1
    fi
    if [[ "$(wc -l <<<"$devices")" -gt 1 ]]; then
        echo "More than one Android device connected; set ANDROID_SERIAL." >&2; exit 1
    fi
    local model
    model="$(run getprop ro.product.model)"
    echo "tablet: $model, Android $(run getprop ro.build.version.release), build $(run getprop ro.build.display.id)"
    SDK="$(run getprop ro.build.version.sdk)"
    # Density gives the panel a 1024 CSS px wide viewport on both models:
    # 1024x600 at DPR 1 on the 7" TB-7305X, 1024x640 at DPR 1.25 on the 8"
    # TB-8505X (its stock 240 would leave 853x533).
    case "$model" in
        "Lenovo TB-7305X") : "${DENSITY:=160}" ;;
        "Lenovo TB-8505X") : "${DENSITY:=200}" ;;
        *) echo "warning: built for the Lenovo TB-7305X and TB-8505X, this is $model"
           : "${DENSITY:=160}" ;;
    esac
}

installed() { run pm list packages --user 0 "$1" | grep -qx "package:$1"; }

configure() {  # extra args for the kiosk's ConfigReceiver
    run am broadcast -n "$PKG/.ConfigReceiver" -a "$PKG.CONFIGURE" "$@" | sed -n 's/.*data="\(.*\)"/  \1/p'
}

# --- undo ------------------------------------------------------------------------

if [[ "$UNDO" == 1 ]]; then
    device
    say "Releasing device owner and removing the kiosk"
    if installed "$PKG"; then
        configure --ez release true || true
        run pm uninstall "$PKG" || true
    fi
    say "Restoring removed apps"
    for pkg in "${BLOAT[@]}" "${GOOGLE[@]}"; do
        run cmd package install-existing "$pkg" 2>/dev/null | grep -q "installed for user" \
            && echo "  restored $pkg" || true
    done
    say "Restoring settings"
    for key in window_animation_scale transition_animation_scale animator_duration_scale; do
        run settings put global "$key" 1
    done
    run settings put global auto_time 1
    run settings put global heads_up_notifications_enabled 1
    run wm density reset
    echo "Done. A factory reset gives back the rest."
    exit 0
fi

# --- provision -----------------------------------------------------------------

device

accounts="$(run dumpsys account | grep -c 'Account {' || true)"
if [[ "$accounts" -gt 0 ]] && ! run dumpsys device_policy | grep -q "$PKG"; then
    echo >&2
    echo "The tablet has $accounts account(s). Android only accepts a device owner on a" >&2
    echo "tablet with none: factory reset it and skip the Google sign-in." >&2
    exit 1
fi

# Find My Device is a device admin, and Android will not remove an active
# admin's package -- nor let adb deactivate it ("non-test admin"). Only the
# person at the tablet can, and it must happen now: once the kiosk is locked
# in, Settings is out of reach.
if [[ "$KEEP_GOOGLE" == 0 ]] && run dumpsys device_policy | grep -q "com.google.android.gms/.mdm"; then
    say "Find My Device is an active device admin"
    run input keyevent KEYCODE_WAKEUP
    run am start -n 'com.android.settings/.Settings$DeviceAdminSettingsActivity' >/dev/null
    echo "  On the tablet: switch off Find My Device, then 'Deactivate this device admin app'."
    for _ in $(seq 1 150); do
        run dumpsys device_policy | grep -q "com.google.android.gms/.mdm" || break
        sleep 2
    done
    if run dumpsys device_policy | grep -q "com.google.android.gms/.mdm"; then
        echo "Still active after 5 minutes. Deactivate it and run again, or use --keep-google." >&2
        exit 1
    fi
    echo "  deactivated"
fi

if [[ -z "$PIN" ]]; then
    PIN="$(shuf -i 100000-999999 -n 1)"
    echo "no --pin given: generated admin PIN $PIN"
fi
[[ "$PIN" =~ ^[0-9]{4,12}$ ]] || { echo "--pin must be 4 to 12 digits" >&2; exit 2; }

say "Web engine"
engine() { run dumpsys webviewupdate | sed -n 's/.*Current WebView package (name, version): (\(.*\))/\1/p'; }
echo "  current: $(engine)"
if [[ "$SDK" -lt 29 ]]; then
    chrome_version="$(run dumpsys package com.android.chrome | sed -n 's/.*versionName=\([0-9]*\).*/\1/p' | sort -n | tail -1)"
    if [[ "${chrome_version:-0}" -lt 138 ]]; then
        # A factory reset brings back the Chrome 77 in the system image, which
        # cannot load the panel (import maps need 89). 138 is the last Chrome
        # for Android 9: the copy in vendor/ was pulled off the TB-7305X.
        [[ -n "$CHROME_DIR" ]] || { echo "vendor/chrome-*/ is missing: the panel needs Chrome >= 89" >&2; exit 1; }
        echo "  installing $(basename "$CHROME_DIR")"
        "$ADB" install-multiple -r "$CHROME_DIR"/*.apk
    fi
    run cmd webviewupdate set-webview-implementation com.android.chrome >/dev/null || true
else
    # Android 10 doesn't accept Chrome as the WebView, only the standalone
    # Android System WebView, and the TB-8505X image ships version 81. The
    # copy in vendor/ is Google's (same signing certificate as the system
    # one; Android refuses the update otherwise), Android 10+, arm64 + arm.
    webview_version="$(engine | sed -n 's/.*, \([0-9]*\)\..*/\1/p')"
    bundled="$(basename "${WEBVIEW_DIR:-webview-0}" | sed 's/webview-\([0-9]*\).*/\1/')"
    if [[ "${webview_version:-0}" -lt 89 && -z "$WEBVIEW_DIR" ]]; then
        echo "vendor/webview-*/ is missing: the panel needs WebView >= 89" >&2; exit 1
    fi
    if [[ "${webview_version:-0}" -lt "$bundled" ]]; then
        echo "  installing $(basename "$WEBVIEW_DIR")"
        "$ADB" install -r "$WEBVIEW_DIR"/*.apk
    fi
fi
echo "  now: $(engine)"

say "Kiosk app"
[[ -f "$APK" ]] || "$HERE/build.sh"
"$ADB" install -r "$APK"
if run dumpsys device_policy | grep -q "admin=ComponentInfo{$PKG"; then
    echo "  already device owner"
else
    run dpm set-device-owner "$ADMIN"
fi
# Home screen now; the kiosk also pins it as device owner on its first start.
run cmd package set-home-activity "$PKG/.MainActivity" >/dev/null || true

say "Removing apps (user 0 only)"
remove=("${BLOAT[@]}")
[[ "$KEEP_GOOGLE" == 1 ]] || remove+=("${GOOGLE[@]}")
for pkg in "${remove[@]}"; do
    installed "$pkg" || continue
    result="$(run pm uninstall -k --user 0 "$pkg" 2>&1 || true)"
    if [[ "$result" == Success ]]; then
        echo "  removed $pkg"
    else
        # Fallback: disable. Some system packages refuse removal.
        run pm disable-user --user 0 "$pkg" >/dev/null 2>&1 \
            && echo "  disabled $pkg ($result)" || echo "  KEPT $pkg ($result)"
    fi
done

say "Settings"
# No animations: on four 1.3 GHz cores they are pure latency. Chrome also
# reads this as prefers-reduced-motion.
for key in window_animation_scale transition_animation_scale animator_duration_scale; do
    run settings put global "$key" 0
done
run settings put system font_scale 1.0
run settings put system screen_off_timeout 1800000      # only matters outside the kiosk
run settings put global stay_on_while_plugged_in 7
run settings put system screen_brightness_mode 0
run settings put system screen_brightness "$BRIGHTNESS"
run settings put system accelerometer_rotation 0
run settings put system sound_effects_enabled 0
run settings put system haptic_feedback_enabled 0
run settings put global heads_up_notifications_enabled 0
run settings put secure screensaver_enabled 0
run settings put secure location_mode 0
run settings put global package_verifier_enable 0
run settings put global wifi_sleep_policy 2               # Wi-Fi never sleeps
run settings put global ota_disable_automatic_update 1
# Lenovo's battery protection (SystemUI handles it): stops charging around
# 60 % so a tablet on USB for years does not sit at 100 % and swell. Not yet
# confirmed on this unit -- check the charge level after a day plugged in.
run settings put system battery_protected 1
run svc data disable || true
run wm density "$DENSITY"
run locksettings set-disabled true >/dev/null 2>&1 || true
# Alarms play on the media stream: all the way up.
max="$(run media volume --stream 3 --get 2>/dev/null | sed -n 's/.*range \[0\.\.\([0-9]*\)\].*/\1/p')"
[[ -n "$max" ]] && run media volume --stream 3 --set "$max" >/dev/null && echo "  media volume $max/$max"
run dumpsys deviceidle whitelist +"$PKG" >/dev/null
echo "  animations off, screen always on while powered, brightness $BRIGHTNESS, density $DENSITY"

say "Kiosk configuration"
args=(--es pin "$PIN")
[[ -n "$URLS" ]] && args+=(--es urls "$URLS")
zone="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
[[ -n "$zone" ]] && args+=(--es timezone "$zone")
configure "${args[@]}"

say "Starting"
run am start -a android.intent.action.MAIN -c android.intent.category.HOME >/dev/null
sleep 3
echo "  focus: $(run dumpsys window | sed -n 's/.*mCurrentFocus=Window{[^ ]* [^ ]* \(.*\)}/\1/p')"
echo "  lock task: $(run dumpsys activity activities | sed -n 's/.*mLockTaskModeState=\(.*\)/\1/p' | head -1)"

cat <<EOF

Provisioned. Admin PIN: $PIN  (tap the top-left corner 5 times)

Next, on the lab PC: labpc/install.sh <user@lab-pc>  -- forwards the panel over USB.
Reboot once to check it comes straight back:  $ADB reboot
EOF
