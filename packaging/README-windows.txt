tpn for Windows (x64 and ARM64)
===============================

Needs: Windows 10/11, and the official Tailscale client installed
(https://tailscale.com/download/windows). tor.exe is included in tor\.
On Windows on ARM, tpn.exe is native ARM64; tor.exe is x64 and runs under
Windows' built-in x64 emulation (the Tor Project ships no ARM64 build).

Run from an elevated (Administrator) PowerShell:

    $env:TPN_AUTH_KEY = "tskey-auth-..."      # a key tagged tag:vpn-client
    .\tpn.exe connect                          # stays in the foreground; Ctrl-C disconnects

From another elevated prompt:

    .\tpn.exe status
    .\tpn.exe disconnect

How it works on Windows: tpn sets HTTPS_PROXY in the Tailscale service's
registry Environment value (HKLM\SYSTEM\CurrentControlSet\Services\Tailscale),
restarts the service so it logs in through tor, and removes the value again
once the direct path is proven, or when you disconnect. State lives in
%ProgramData%\tpn.

If tor itself is blocked, give it obfs4 bridges (lyrebird.exe is bundled):

    $env:TPN_TOR_BRIDGES = '["obfs4 1.2.3.4:443 FINGERPRINT cert=... iat-mode=0"]'

Keep this folder at a path without spaces if you use bridges.
