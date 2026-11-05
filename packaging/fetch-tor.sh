#!/usr/bin/env bash
# Download, verify and extract a tor expert bundle.
#
#   packaging/fetch-tor.sh windows-x86_64 build/tor/windows-x86_64
#   packaging/fetch-tor.sh android-aarch64 build/tor/android-aarch64
#
# Verification: the detached signature must be good and made by a key whose primary
# fingerprint is TOR_SIGNING_FPR (packaging/tor.env). Anything else aborts.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=packaging/tor.env
source "$HERE/tor.env"

platform=${1:?platform, e.g. windows-x86_64}
out=${2:?output directory}
file="tor-expert-bundle-$platform-$TOR_VERSION.tar.gz"
url="$TOR_BASE_URL/$TOR_VERSION/$file"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 4 -o "$tmp/$file" "$url"
curl -fsSL --retry 4 -o "$tmp/$file.asc" "$url.asc"

export GNUPGHOME="$tmp/gnupg"
mkdir -m 700 "$GNUPGHOME"
gpg --batch --quiet --import "$HERE/tor-signing-key.asc" 2>/dev/null
status=$(gpg --batch --status-fd 1 --verify "$tmp/$file.asc" "$tmp/$file" 2>/dev/null || true)
grep -q '^\[GNUPG:\] GOODSIG ' <<<"$status" || { echo "fetch-tor: bad signature on $file" >&2; exit 1; }
primary=$(awk '$2 == "VALIDSIG" {print $NF}' <<<"$status")
if [[ $primary != "$TOR_SIGNING_FPR" ]]; then
    echo "fetch-tor: $file is signed by $primary, expected $TOR_SIGNING_FPR" >&2
    exit 1
fi

rm -rf "$out" && mkdir -p "$out"
tar -xzf "$tmp/$file" -C "$out"
echo "fetch-tor: verified $file -> $out"
