#!/usr/bin/env bash
# Build dist/tpn-server-<version>-linux.tar.gz: the wheel, a systemd unit and install.sh.
# The server is Linux-only; the wheel is pure Python, so one tarball covers x86_64 and arm64.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
version=$(python3 -c 'import sys; sys.path.insert(0, "src"); import tpn; print(tpn.__version__)')
name="tpn-server-$version-linux"
stage="build/$name"
rm -rf "$stage" && mkdir -p "$stage" dist
if command -v uv >/dev/null; then
    uv build --quiet --wheel --out-dir "$stage"
else
    python3 -m pip wheel --quiet --no-deps --wheel-dir "$stage" .
fi
rm -f "$stage/.gitignore"
cp packaging/server/install.sh packaging/server/tpn-server.service "$stage/"
cp policy/tailnet.hujson "$stage/"
tar -C build -czf "dist/$name.tar.gz" "$name"
echo "dist/$name.tar.gz"
