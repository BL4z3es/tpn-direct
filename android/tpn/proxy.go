// Package tpn adds the tpn-direct behaviour to the Tailscale Android app: log in and reach DERP
// through an embedded tor, switch to the direct path once it is proven to work, and route all
// traffic through a healthy tag:vpn-server exit node.
//
// It is the Android counterpart of the Python client in src/tpn and follows the same state
// machine. It is copied into a pinned tailscale-android checkout by android/build.sh.
package tpn

import (
	"net/url"
	"sync/atomic"

	"tailscale.com/net/tshttpproxy"
)

// proxy is the HTTP CONNECT proxy tailscale's control, DERP and log clients use, or nil for
// the direct path. It is read on every request, so switching needs no backend restart.
var proxy atomic.Pointer[url.URL]

// InstallProxy routes tailscale's HTTP clients through whatever SetProxy last chose. It must
// run before the backend starts: tshttpproxy only accepts a proxy func before its first use.
func InstallProxy() error {
	return tshttpproxy.SetProxyFunc(proxyFor)
}

func proxyFor(u *url.URL) (*url.URL, error) {
	p := proxy.Load()
	if p == nil || u == nil || isLoopback(u.Hostname()) {
		return nil, nil
	}
	return p, nil
}

// SetProxy points tailscale at proxyURL ("" for direct).
func SetProxy(proxyURL string) error {
	if proxyURL == "" {
		proxy.Store(nil)
		return nil
	}
	u, err := url.Parse(proxyURL)
	if err != nil {
		return err
	}
	proxy.Store(u)
	return nil
}

func isLoopback(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}
