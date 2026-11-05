#!/usr/bin/env bash
# Build the tpn-direct Android APK: tailscale-android at a pinned commit, plus
#   - patches/*.patch  (hooks into libtailscale, own app id and name, no self-routing)
#   - tpn/             (the Go controller: tor, go-direct, exit-node selection)
#   - libTor.so        (from the Tor Project's signed expert bundle, per ABI)
#
#   ANDROID_HOME=/path/to/sdk android/build.sh      -> dist/tpn-direct-<version>-android.apk
#
# Optional release signing (otherwise the APK carries a throwaway debug signature, and each
# build would install as a different signer). All four are needed:
#   TPN_KEYSTORE_B64  base64 of a .jks/.p12 keystore     TPN_KEY_ALIAS
#   TPN_KEYSTORE_PASS keystore password                   TPN_KEY_PASS
#
# Needs: git, curl, gpg, make, JDK 17+, an Android SDK. tailscale-android's Makefile
# installs the NDK/platform packages it wants and its own Go toolchain.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
# shellcheck source=android/upstream.env
source "$HERE/upstream.env"
: "${ANDROID_HOME:?set ANDROID_HOME to an Android SDK}"
export ANDROID_HOME ANDROID_SDK_ROOT="$ANDROID_HOME"

version=$(python3 -c "import sys; sys.path.insert(0, '$ROOT/src'); import tpn; print(tpn.__version__)")
work="$ROOT/build/android"
src="$work/tailscale-android"
mkdir -p "$work" "$ROOT/dist"

# 1. upstream at the pinned commit (full history, lazy blobs: its version tooling reads git).
if [[ ! -d $src/.git ]]; then
    git clone --quiet --filter=blob:none --no-checkout "$UPSTREAM_REPO" "$src"
fi
git -C "$src" fetch --quiet origin "$UPSTREAM_REV"
git -C "$src" checkout --quiet --force "$UPSTREAM_REV"
git -C "$src" clean -fdxq -e android/build -e .gradle

# 2. our changes
for patch in "$HERE"/patches/*.patch; do
    git -C "$src" apply --whitespace=nowarn "$patch"
done
mkdir -p "$src/tpn"
find "$HERE/tpn" -maxdepth 1 -name '*.go' ! -name '*_test.go' -exec cp {} "$src/tpn/" \;

# 3. tor for each ABI the Tor Project builds (no 32-bit x86: that ABI runs without tor).
for pair in aarch64:arm64-v8a armv7:armeabi-v7a x86_64:x86_64; do
    torarch=${pair%%:*} abi=${pair##*:}
    "$ROOT/packaging/fetch-tor.sh" "android-$torarch" "$work/tor-$torarch"
    install -D -m 0755 "$work/tor-$torarch/tor/libTor.so" \
        "$src/android/src/main/jniLibs/$abi/libTor.so"
done

# 4. build with upstream's own tooling
make -C "$src" androidsdk
make -C "$src" apk
apk="$ROOT/dist/tpn-direct-$version-android.apk"
cp "$src/tailscale-debug.apk" "$apk"

# 5. sign with a stable key when one is provided
if [[ -n ${TPN_KEYSTORE_B64:-} ]]; then
    : "${TPN_KEYSTORE_PASS:?}" "${TPN_KEY_ALIAS:?}" "${TPN_KEY_PASS:?}"
    apksigner=$(find "$ANDROID_HOME/build-tools" -name apksigner -type f | sort -V | tail -n1)
    [[ -n $apksigner ]] || { echo "build.sh: apksigner not found" >&2; exit 1; }
    keystore="$work/release.keystore"
    base64 -d <<<"$TPN_KEYSTORE_B64" > "$keystore"
    "$apksigner" sign --ks "$keystore" --ks-pass env:TPN_KEYSTORE_PASS \
        --ks-key-alias "$TPN_KEY_ALIAS" --key-pass env:TPN_KEY_PASS "$apk"
    rm -f "$keystore"
    "$apksigner" verify "$apk"
    echo "build.sh: signed with $TPN_KEY_ALIAS"
else
    echo "build.sh: WARNING: debug-signed; set TPN_KEYSTORE_B64 etc. for installable updates" >&2
fi
echo "$apk"
