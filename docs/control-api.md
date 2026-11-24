# The daemon's control API

The tpn daemon (`tpn daemon` on Linux, the `tpn` Windows service) owns tor and tailscaled. Everything
else (`tpn up`, `tpn down`, `tpn status`, the Windows app) drives it through this API. The shapes
and the client live in [`src/tpn/control.py`](../src/tpn/control.py); this page is the prose.

## Transport and auth

- HTTP/1.1 on `127.0.0.1:<TPN_CONTROL_PORT>` (default **9081**). Never any other address.
- Every request carries `Authorization: Bearer <token>`. The token is in `state_dir/control.token`
  (`%ProgramData%\tpn\control.token`, `/var/lib/tpn/control.token`); the daemon writes a fresh one at
  every start and deletes it on a clean exit.
- File access is the permission model. Linux: `0600 root`. Windows: SYSTEM and Administrators full
  control, `BUILTIN\Users` read, inheritance removed. Everything else in `state_dir` (prefs, authkey,
  tor data, logs) stays SYSTEM/Administrators only on Windows, root only on Linux.
- Before routing, the server calls `control.check_request(headers, token=..., port=...)` and refuses
  with its status: a `Host` other than `127.0.0.1:<port>`/`localhost:<port>` → 403, any `Origin`
  header → 403, a bad token → 401.
- Bodies are JSON (`Content-Type: application/json`). Errors are `{"error": "<message>"}` with a 4xx/5xx
  status. Unknown paths → 404, wrong method → 405, body that does not parse or validate → 400.

## Endpoints

All paths are under `/v1`.

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /status` | – | 200 `Status` | |
| `POST /connect` | `{"server"?: str, "use_tor"?: bool, "keep_tor"?: bool}` | 202 `Status` (phase already moved off `disconnected`/`failed`) | 409 already connecting or connected; 409 a foreground `tpn connect` is running |
| `POST /disconnect` | – | 200 `Status` with phase `disconnected`, after teardown finished | |
| `GET /servers` | – | 200 `{"servers": [ServerInfo]}` | 503 tailscaled not answering |
| `GET /prefs` | – | 200 `Prefs` | |
| `PATCH /prefs` | partial `Prefs` | 200 `Prefs` (the stored result) | 400 unknown key / bad type |
| `PUT /auth-key` | `{"auth_key": "tskey-..."}` | 204 | 400 not a string starting `tskey-` |
| `DELETE /auth-key` | – | 204 | |
| `GET /logs?lines=N` | – | 200 `{"lines": [str]}`, the last N (default 200, max 2000) daemon log lines | |

### Status

```json
{
  "daemon": {"version": "0.3.0", "pid": 4242, "started_at": 1795000000.0},
  "phase": "connected",
  "transport": "direct",
  "server": "vpn-fra-1",
  "server_ip": "100.64.0.10",
  "detail": "",
  "connected_since": 1795000123.4,
  "retry_at": null,
  "has_auth_key": false,
  "logged_in": true
}
```

`phase` is one of the agent's phases (`tpn.bootstrap.Phase`):
`init, tor_starting, tor_ready, tailnet_up, probing, going_direct` (connecting),
`connected`, `failed`, `disconnected`. `detail` carries the agent's explanation
(e.g. why tor was kept, or the error that failed the attempt).

### ServerInfo

```json
{"hostname": "vpn-fra-1", "dns_name": "vpn-fra-1.tail1234.ts.net", "ipv4": "100.64.0.10",
 "online": true, "healthy": true, "latency_ms": 41.0, "detail": "", "selected": true}
```

Every peer tagged `TPN_SERVER_TAG` that offers itself as an exit node, health-checked
(`GET /v1/health` on the server's API, as `tpn.discovery.ServerSelector` does). Offline peers are
listed with `healthy: null`.

### Prefs

`tpn.prefs.Prefs`, stored in `state_dir/prefs.json`:
`use_tor`, `keep_tor`, `server`, `hostname`, `tor_bridges`, `auto_connect`.
A PATCH applies to the next connection, not the running one.

## Behaviour the daemon guarantees

- **One owner.** The daemon refuses to connect while a foreground `tpn connect` is alive (its pid in
  `state.json` is alive and is not the daemon's), and `tpn connect` refuses while the daemon answers.
- **Retries.** A failed attempt is retried with backoff (15 s, doubling, capped at 5 min) until
  `POST /disconnect`. Status stays `failed` with `retry_at` set. Errors a retry cannot fix (not logged
  in and no auth key) are not retried: `retry_at` is null.
- **Auth key.** `PUT /auth-key` stores the key in `state_dir/authkey` (SYSTEM/Administrators or root
  only). It is used when tailscaled needs a login and deleted once the agent reaches `tailnet_up` with
  it. The key is never returned by any endpoint or written to a log.
- **Auto-connect.** With `auto_connect` set, the daemon connects as soon as it starts.
- **Clean stop.** Stopping the daemon (service stop, SIGTERM, Ctrl-C) disconnects first: exit node
  cleared, tailscaled's proxy removed, tor stopped.
