#!/usr/bin/env bash
# Run the tpn Go package's tests against the tailscale.com version the pinned app uses,
# without building the whole app: borrow upstream's go.mod/go.sum and drop tpn/ beside them.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=android/upstream.env
source "$HERE/upstream.env"
raw="https://raw.githubusercontent.com/tailscale/tailscale-android/$UPSTREAM_REV"
mod=$(mktemp -d)
trap 'rm -rf "$mod"' EXIT
curl -fsSL --retry 4 -o "$mod/go.mod" "$raw/go.mod"
curl -fsSL --retry 4 -o "$mod/go.sum" "$raw/go.sum"
mkdir "$mod/tpn"
cp "$HERE"/tpn/*.go "$mod/tpn/"
cd "$mod"
unformatted=$(gofmt -l tpn)
[[ -z $unformatted ]] || { echo "gofmt needed: $unformatted" >&2; exit 1; }
GOFLAGS=-mod=mod go vet ./tpn
GOFLAGS=-mod=mod go test -race -count=1 ./tpn
# The app is built for Android; make sure the package compiles there too.
GOFLAGS=-mod=mod GOOS=android GOARCH=arm64 CGO_ENABLED=0 go build ./tpn
