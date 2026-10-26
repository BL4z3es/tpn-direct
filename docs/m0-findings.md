# M0 findings

_Not run yet. Fill this in from [`m0-runbook.md`](m0-runbook.md)._

| Question | Result | Evidence |
|---|---|---|
| tailscaled logs in through tor HTTPTunnelPort | ? | |
| Time for `tailscale up` over tor | ? | |
| Errors / rate limits from Tailscale for tor exits | ? | |
| Data path under block: direct UDP or DERP | ? | |
| Throughput through exit node (blocked / unblocked) | ? | |
| Egress survives after tor stopped (blocked) | ? (for how long) | |
| Exact health text when control is unreachable | ? | |
| Restart without proxy reuses node key (no re-login) | ? | |

## Changes required in code
- [ ] Update `CONTROL_HEALTH_MARKERS` in `src/tpn/tailscale.py` with the real strings.
- [ ] Tune `TPN_CONTROL_TIMEOUT`, `TPN_DIRECT_VERIFY_TIMEOUT` and `TPN_WATCHDOG_FAILURES` defaults.
