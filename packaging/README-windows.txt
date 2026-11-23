tpn for Windows (x64 and ARM64)
===============================

Needs: Windows 10/11 and the official Tailscale client
(https://tailscale.com/download/windows). Tor is included in tor\.
On Windows on ARM, tpn.exe and tpn-app.exe are native ARM64; tor.exe is x64
and runs under Windows' built-in x64 emulation.

Install and connect
-------------------
Run tpn-<version>-windows-<arch>-setup.exe as an administrator. It installs
the tpn Windows service (LocalSystem, automatic delayed start) and the tpn
tray application. The optional "launch at logon" task starts the tray app for
every user who signs in. Start "tpn" from the Start menu if it is not already
in the notification area.

Set a Tailscale auth key tagged tag:vpn-client once. Use the tray app's
Settings window, or an elevated (Administrator) PowerShell in the install
folder:

    .\tpn.exe set-key

Then connect from the tray app. The same operations are available from the
command line:

    .\tpn.exe up
    .\tpn.exe status
    .\tpn.exe down

The service and tray app communicate only through the local control API on
127.0.0.1:9081. You do not need to keep a command prompt open.

Using the zip without the installer
-----------------------------------
Keep tpn.exe, tpn-app.exe, tor\ and README.txt together. In an elevated
PowerShell in that folder, register and start the service:

    .\tpn.exe service-install
    Start-Service tpn
    .\tpn-app.exe

Use tpn.exe service-uninstall to stop and remove the service later. Wait for
the service to finish disconnecting (up to about 90 seconds) before removing
the folder or starting a foreground connection. Service installation needs
administrator rights. The zip does not create Start-menu shortcuts or
configure the tray app to start at logon.

If Tor itself is blocked
------------------------
Add obfs4 bridges in the tray app's Settings before connecting (lyrebird.exe
is bundled). For a foreground connection, they can instead be supplied as a
JSON list:

    $env:TPN_TOR_BRIDGES = '["obfs4 1.2.3.4:443 FINGERPRINT cert=... iat-mode=0"]'

Foreground fallback
-------------------
For troubleshooting, stop the background service so there is only one owner
of Tailscale, then run tpn in the console from an elevated prompt:

    Stop-Service tpn
    $env:TPN_AUTH_KEY = "tskey-auth-..."    # if no key has been saved
    .\tpn.exe connect                       # Ctrl-C disconnects cleanly

Flags include --no-tor, --keep-tor and --server NAME. Restart the service
with Start-Service tpn when finished. With the service stopped, status and
disconnect control the foreground connection.

How it works on Windows
-----------------------
The tpn service starts Tor with an HTTP CONNECT proxy on 127.0.0.1:9080. It
sets HTTPS_PROXY in the Tailscale service's registry Environment value
(HKLM\SYSTEM\CurrentControlSet\Services\Tailscale), then restarts Tailscale
so login and coordination traffic use Tor. Once the direct path is proven,
it removes the proxy and stops Tor; if that fails, it rolls back to Tor.
It selects a healthy tag:vpn-server exit node and watches the connection.
Disconnecting clears the exit node, removes the proxy and stops Tor.

State, preferences and logs live in %ProgramData%\tpn. The installer offers
to remove this directory when uninstalling; keep it if you want to preserve
settings and diagnostic logs.
