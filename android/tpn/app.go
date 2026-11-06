package tpn

import (
	"context"
	"log"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnlocal"
	"tailscale.com/ipn/ipnstate"
	"tailscale.com/net/tsdial"
	"tailscale.com/tailcfg"
)

// localBackend adapts the app's ipnlocal.LocalBackend to Backend.
type localBackend struct {
	lb     *ipnlocal.LocalBackend
	dialer *tsdial.Dialer
}

func (b localBackend) Status() Status {
	return statusFrom(b.lb.Status())
}

func statusFrom(st *ipnstate.Status) Status {
	out := Status{
		Running: st.BackendState == ipn.Running.String(),
		Health:  st.Health,
	}
	if st.ExitNodeStatus != nil {
		out.ExitNodeID = string(st.ExitNodeStatus.ID)
		out.ExitNodeOnline = st.ExitNodeStatus.Online
	}
	for _, ps := range st.Peer {
		p := Peer{
			ID:             string(ps.ID),
			Name:           ps.HostName,
			Online:         ps.Online,
			ExitNodeOption: ps.ExitNodeOption,
		}
		if ps.Tags != nil {
			p.Tags = ps.Tags.AsSlice()
		}
		for _, ip := range ps.TailscaleIPs {
			if ip.Is4() {
				p.IP = ip
				break
			}
		}
		if !p.IP.IsValid() && len(ps.TailscaleIPs) > 0 {
			p.IP = ps.TailscaleIPs[0]
		}
		out.Peers = append(out.Peers, p)
	}
	return out
}

func (b localBackend) SetExitNode(id string) error {
	_, err := b.lb.EditPrefs(&ipn.MaskedPrefs{
		Prefs: ipn.Prefs{
			ExitNodeID:             tailcfg.StableNodeID(id),
			ExitNodeAllowLANAccess: true,
		},
		ExitNodeIDSet:             true,
		ExitNodeAllowLANAccessSet: true,
	})
	return err
}

func (b localBackend) DialTailnet(ctx context.Context, network, addr string) (net.Conn, error) {
	return b.dialer.UserDial(ctx, network, addr)
}

var started atomic.Bool

// holdProxy refuses connections immediately (nothing listens on port 1).
const holdProxy = "http://127.0.0.1:1"

// Start runs the controller for the app's backend. libtailscale calls it once the
// LocalBackend exists; later calls are ignored. Tor is used when the APK ships it for this
// ABI and TPN_DISABLE_TOR is unset.
func Start(lb *ipnlocal.LocalBackend, dialer *tsdial.Dialer, dataDir string) {
	if !started.CompareAndSwap(false, true) {
		return
	}
	c := &Controller{
		Backend: localBackend{lb: lb, dialer: dialer},
		Probe:   DirectProbe(DefaultProbeTargets, 10*time.Second),
		Logf:    log.Printf,
	}
	if tor := findTor(dataDir); tor != nil {
		c.Tor = tor
		// Until tor has bootstrapped, send tailscale's HTTP traffic nowhere rather than out
		// in the clear: a direct login attempt is exactly what a blocking network looks for.
		SetProxy(holdProxy)
	}
	go c.Run(context.Background())
}

func findTor(dataDir string) *Tor {
	if os.Getenv("TPN_DISABLE_TOR") != "" {
		log.Printf("tpn: tor disabled by TPN_DISABLE_TOR")
		return nil
	}
	libDir, err := NativeLibDir()
	if err != nil {
		log.Printf("tpn: cannot find native libraries: %v", err)
		return nil
	}
	bin := filepath.Join(libDir, "libTor.so")
	if _, err := os.Stat(bin); err != nil {
		log.Printf("tpn: this build ships no tor for this CPU (%v)", err)
		return nil
	}
	return &Tor{
		Binary:  bin,
		DataDir: filepath.Join(dataDir, "tpn-tor"),
		Logf:    log.Printf,
	}
}
