# tpn-direct for Android (APK)

An Android app, not a Termux script. Android gives one app the VPN slot, so tpn can't sit
beside the Tailscale app the way it sits beside tailscaled on Linux. Instead this is a build of
[tailscale-android](https://github.com/tailscale/tailscale-android) (BSD-3) at a pinned commit,
with tpn's controller compiled into its Go backend and tor bundled inside it.

| Piece | Where |
|---|---|
| Pinned upstream commit | [`upstream.env`](upstream.env) |
| Changes to upstream (19 lines) | [`patches/0001-tpn-direct.patch`](patches/0001-tpn-direct.patch) |
| The controller (Go) | [`tpn/`](tpn/) |
| tor | `libTor.so` from the Tor Project's expert bundle, signature-checked by [`../packaging/fetch-tor.sh`](../packaging/fetch-tor.sh) |
| Build | [`build.sh`](build.sh), run by `.github/workflows/build.yml` |

## What it does

Same state machine as the Linux and Windows client:

1. On launch, before tailscale opens a single connection, its HTTP clients (control, DERP, logs)
   are pointed at a dead loopback port, so nothing goes out in the clear. tor starts from
   `lib/<abi>/libTor.so` with an HTTP CONNECT tunnel on a random loopback port; once it
   reports 100% bootstrap, tailscale's traffic goes through it. This uses tailscale's own
   `tshttpproxy.SetProxyFunc`, so switching never restarts the backend.
2. You log in with an auth key (login screen menu: **Use an auth key**, a key tagged
   `tag:vpn-client`) or the browser flow. The browser flow opens login.tailscale.com in your
   browser, which does **not** go through tor: on a network that blocks Tailscale, use a key.
3. Once tailscale is running, the controller probes `controlplane.tailscale.com` and a DERP
   server directly. If both answer, it stops tor and checks the coordination server is still
   reachable; if not within 60 s, it starts tor again and rolls back.
4. It health-checks every online `tag:vpn-server` exit node on `GET /v1/health` (over the
   tailnet, through the in-process netstack) and sets the fastest as the exit node.
5. Every 30 s: restart tor if it died, fall back to tor after 3 unhealthy checks on the direct
   path, replace the exit node if it goes offline.

Your choices win: if you pick a different exit node, it's left alone; if you turn the exit
node off, it stays off until the VPN is next started.

## Differences from the official app

- Its own app id (`io.github.bl4z3es.tpndirect`) and name (**tpn-direct**), so it installs
  beside the official Tailscale app. Only one of them can hold the VPN at a time.
- The app's own traffic is excluded from its VPN (in the default "all apps" mode). Without
  that, with UDP blocked, DERP-over-tor would have to travel through the tunnel it carries.
- Native libraries are extracted at install (`useLegacyPackaging`), so `libTor.so` exists as
  an executable file.

## Building

CI builds it on every push touching `android/`, `src/` or `packaging/`, and attaches it to
`v*` releases. Locally (Linux, JDK 17, an Android SDK; the upstream Makefile installs the NDK
and its own Go toolchain):

```bash
ANDROID_HOME=$HOME/Android/Sdk android/build.sh
```

Without a signing key the APK is debug-signed with a key that changes every CI run, so each
build installs as a *different* app signer and Android refuses to update in place. For
updatable builds, add repository secrets `TPN_KEYSTORE_B64` (base64 of a keystore),
`TPN_KEYSTORE_PASS`, `TPN_KEY_ALIAS`, `TPN_KEY_PASS`:

```bash
keytool -genkeypair -v -keystore tpn.jks -alias tpn -keyalg RSA -keysize 4096 -validity 10000
```

```bash
base64 -w0 tpn.jks
```

Go tests for the controller (no Android SDK needed): `android/test.sh`.

## Known gaps

- **Not yet run on a device.** The controller is unit-tested and the patched backend
  type-checks for android/arm64; the first CI APK is the first real build. Treat v0.2 as a
  field test, and check `adb logcat | grep tpn:` when something looks wrong.
- **No settings screen.** Bridges (`obfs4`), "keep tor" and "no tor" are not configurable in
  the app yet, so a network that blocks tor itself defeats it.
- **32-bit x86** devices get no tor (the Tor Project doesn't build it) and connect directly.
- **Rebasing on upstream** is manual: bump `UPSTREAM_REV`, re-run, fix the patch if it no
  longer applies. Upstream ships security fixes; don't let the pin go stale.
- **Other apps on the device can use the tor tunnel port** while it runs (loopback only).
