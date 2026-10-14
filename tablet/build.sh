#!/usr/bin/env bash
# Build the kiosk APK with the bare SDK tools: no Gradle, no Android Studio.
#
#   ./build.sh            -> dist/vacuum-lab-kiosk.apk
#
# The toolchain (JDK 17, build-tools 34, android-28 platform, platform-tools)
# is downloaded once into .toolchain/ (about 330 MB, ignored by git). Set
# TOOLCHAIN=/some/dir to keep it elsewhere.
#
# The APK is signed with kiosk/signing/kiosk.keystore, created on the first
# build. KEEP THAT FILE. Once the app is device owner, an update signed with
# any other key is refused, and the only way back is a factory reset.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TC="${TOOLCHAIN:-$HERE/.toolchain}"
SRC="$HERE/kiosk"
OUT="$HERE/build"
DIST="$HERE/dist"
APK="$DIST/vacuum-lab-kiosk.apk"
KEYSTORE="$SRC/signing/kiosk.keystore"
# Not a secret worth protecting: whoever has the keystore file has the repo.
KEYPASS="vacuum-lab-kiosk"

fetch() {  # url dest-dir name
    local url="$1" dest="$2" name="$3"
    [[ -e "$dest" ]] && return 0
    echo "toolchain: fetching $name"
    mkdir -p "$TC/.dl"
    local file="$TC/.dl/$(basename "$url")"
    curl -fSL --retry 3 -o "$file" "$url"
    case "$file" in
        *.zip) (cd "$TC/.dl" && unzip -q -o "$file") ;;
        *)     (cd "$TC/.dl" && tar xzf "$file") ;;
    esac
}

toolchain() {
    mkdir -p "$TC"
    if [[ ! -x "$TC/jdk/bin/javac" ]]; then
        fetch "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse" \
              "$TC/jdk" "JDK 17"
        mv "$TC"/.dl/jdk-17* "$TC/jdk"
    fi
    if [[ ! -x "$TC/build-tools/aapt2" ]]; then
        fetch https://dl.google.com/android/repository/build-tools_r34-linux.zip "$TC/build-tools" "build-tools 34"
        mv "$TC/.dl/android-14" "$TC/build-tools"
    fi
    if [[ ! -f "$TC/platform-28/android.jar" ]]; then
        fetch https://dl.google.com/android/repository/platform-28_r06.zip "$TC/platform-28" "platform android-28"
        mv "$TC/.dl/android-9" "$TC/platform-28"
    fi
    if [[ ! -x "$TC/platform-tools/adb" ]]; then
        fetch https://dl.google.com/android/repository/platform-tools-latest-linux.zip "$TC/platform-tools" "platform-tools"
        mv "$TC/.dl/platform-tools" "$TC/platform-tools"
    fi
    rm -rf "$TC/.dl"
}

toolchain

export JAVA_HOME="$TC/jdk"
export PATH="$JAVA_HOME/bin:$PATH"
BT="$TC/build-tools"
ANDROID_JAR="$TC/platform-28/android.jar"

# Monotonic without bookkeeping: an update is only accepted with a version
# code no lower than the installed one.
VERSION_CODE="$(date +%y%m%d%H)"
VERSION_NAME="1.0 ($(date +%Y-%m-%d\ %H:%M))"

rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex" "$DIST"

echo "build: resources"
"$BT/aapt2" compile --dir "$SRC/res" -o "$OUT/res.zip"
"$BT/aapt2" link -o "$OUT/unsigned.apk" -I "$ANDROID_JAR" \
    --manifest "$SRC/AndroidManifest.xml" --java "$OUT/gen" \
    --min-sdk-version 28 --target-sdk-version 28 \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
    "$OUT/res.zip"

echo "build: java"
# -source/-target 8 against the platform jar: javac --release cannot target
# Android's class library, and d8 desugars the lambdas.
javac -source 8 -target 8 -bootclasspath "$ANDROID_JAR" \
    -classpath "$ANDROID_JAR:$BT/core-lambda-stubs.jar" \
    -Xlint:-options -Xlint:deprecation -encoding UTF-8 -d "$OUT/classes" \
    $(find "$SRC/src" "$OUT/gen" -name '*.java')

echo "build: dex"
"$BT/d8" --release --min-api 28 --lib "$ANDROID_JAR" --output "$OUT/dex" \
    $(find "$OUT/classes" -name '*.class')
(cd "$OUT/dex" && "$BT/aapt" add "$OUT/unsigned.apk" classes.dex >/dev/null)

echo "build: sign"
if [[ ! -f "$KEYSTORE" ]]; then
    mkdir -p "$(dirname "$KEYSTORE")"
    keytool -genkeypair -keystore "$KEYSTORE" -alias kiosk -keyalg RSA -keysize 2048 \
        -validity 36500 -storepass "$KEYPASS" -keypass "$KEYPASS" \
        -dname "CN=Vacuum Lab kiosk" >/dev/null
    echo "build: created $KEYSTORE -- keep it, updates need it"
fi
"$BT/zipalign" -p -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-key-alias kiosk \
    --ks-pass "pass:$KEYPASS" --key-pass "pass:$KEYPASS" \
    --out "$APK" "$OUT/aligned.apk"
"$BT/apksigner" verify "$APK"

echo "built $APK  version $VERSION_NAME ($VERSION_CODE)"
