package tpn

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"time"
)

// Substrings of tailscale health warnings that mean "can't reach the coordination server".
// Same list as src/tpn/tailscale.py; confirm them against the M0 runbook.
var controlHealthMarkers = []string{"coordination server", "map poll", "not connected to control"}

// Peer is the part of a tailnet peer the controller needs.
type Peer struct {
	ID             string
	Name           string
	IP             netip.Addr
	Online         bool
	Tags           []string
	ExitNodeOption bool
}

// Status is a snapshot of the local tailscale backend.
type Status struct {
	Running        bool // BackendState == Running: logged in and the VPN is on
	Health         []string
	Peers          []Peer
	ExitNodeID     string
	ExitNodeOnline bool
}

// ControlHealthy reports whether the backend is running and in touch with the coordination server.
func (s Status) ControlHealthy() bool {
	if !s.Running {
		return false
	}
	for _, msg := range s.Health {
		lower := strings.ToLower(msg)
		for _, marker := range controlHealthMarkers {
			if strings.Contains(lower, marker) {
				return false
			}
		}
	}
	return true
}

// Backend is the local tailscale backend (an ipnlocal.LocalBackend in the app).
type Backend interface {
	Status() Status
	SetExitNode(id string) error
	// DialTailnet dials a tailnet address through the in-process netstack.
	DialTailnet(ctx context.Context, network, addr string) (net.Conn, error)
}

// TorRunner is what the controller needs from Tor; a fake in tests.
type TorRunner interface {
	Start(ctx context.Context) error
	Stop()
	Running() bool
	ProxyURL() string
}

// Transport is how tailscale reaches the coordination server and DERP.
type Transport string

const (
	TransportTor    Transport = "tor"
	TransportDirect Transport = "direct"
)

// Controller drives the tor -> direct switch and exit-node selection.
type Controller struct {
	Backend Backend
	Tor     TorRunner // nil when this ABI ships no tor, or tor is disabled
	// Probe checks Tailscale's infrastructure is reachable without tor; nil error means yes.
	Probe     func(ctx context.Context) error
	ServerTag string
	APIPort   int
	KeepTor   bool

	Interval       time.Duration // watchdog period
	DirectVerify   time.Duration // how long the direct path gets to reach control
	ControlTimeout time.Duration // how long a rollback to tor gets
	Failures       int           // unhealthy ticks before falling back to tor
	HealthTimeout  time.Duration
	DirectGrace    time.Duration // wait after dropping tor before trusting health
	Poll           time.Duration // health polling interval while waiting
	Logf           func(string, ...any)

	mu        sync.Mutex
	transport Transport
	server    string // ID of the exit node we chose, if any
	optedOut  bool   // the user cleared the exit node we set: leave it off this session
	unhealthy int
	triedDir  bool
	wasUp     bool
}

func (c *Controller) logf(format string, args ...any) {
	if c.Logf != nil {
		c.Logf("tpn: "+format, args...)
	}
}

func (c *Controller) defaults() {
	if c.ServerTag == "" {
		c.ServerTag = "tag:vpn-server"
	}
	if c.APIPort == 0 {
		c.APIPort = 8443
	}
	if c.Interval == 0 {
		c.Interval = 30 * time.Second
	}
	if c.DirectVerify == 0 {
		c.DirectVerify = 60 * time.Second
	}
	if c.ControlTimeout == 0 {
		c.ControlTimeout = 90 * time.Second
	}
	if c.Failures == 0 {
		c.Failures = 3
	}
	if c.HealthTimeout == 0 {
		c.HealthTimeout = 10 * time.Second
	}
	if c.DirectGrace == 0 {
		c.DirectGrace = 5 * time.Second
	}
	if c.Poll == 0 {
		c.Poll = time.Second
	}
}

// CurrentTransport reports how tailscale currently reaches control and DERP.
func (c *Controller) CurrentTransport() Transport {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.transport
}

// Run starts tor, then watches the backend until ctx ends. It never returns early: a
// failure is logged and retried on the next tick, because the app keeps running regardless.
func (c *Controller) Run(ctx context.Context) {
	c.Start(ctx)
	for {
		c.Tick(ctx)
		select {
		case <-ctx.Done():
			if c.Tor != nil {
				c.Tor.Stop()
			}
			return
		case <-time.After(c.Interval):
		}
	}
}

// Start applies defaults and brings up the initial transport.
func (c *Controller) Start(ctx context.Context) {
	c.defaults()
	if c.Tor != nil {
		c.useTor(ctx)
	} else {
		c.setTransport(TransportDirect)
		c.logf("no tor available; connecting directly")
	}
}

func (c *Controller) setTransport(t Transport) {
	c.mu.Lock()
	c.transport = t
	c.mu.Unlock()
}

// useTor starts tor (retrying until it bootstraps) and points tailscale at it.
func (c *Controller) useTor(ctx context.Context) {
	for {
		err := c.Tor.Start(ctx)
		if err == nil {
			break
		}
		c.logf("tor failed to start: %v; retrying in %v", err, c.Interval)
		select {
		case <-ctx.Done():
			return
		case <-time.After(c.Interval):
		}
	}
	SetProxy(c.Tor.ProxyURL())
	c.setTransport(TransportTor)
	c.logf("tailscale now reaches control and DERP through tor")
}

// Tick is one watchdog pass.
func (c *Controller) Tick(ctx context.Context) {
	st := c.Backend.Status()
	if !st.Running {
		// Logged out, or the VPN is switched off: nothing to steer until it comes back.
		if c.wasUp {
			c.logf("tailscale is not running")
		}
		c.wasUp, c.optedOut, c.unhealthy = false, false, 0
		return
	}
	if !c.wasUp {
		c.wasUp = true
		c.server = ""
	}

	if c.CurrentTransport() == TransportTor && !c.Tor.Running() {
		c.logf("tor died, restarting it")
		c.useTor(ctx)
	}

	healthy := st.ControlHealthy()
	if healthy {
		c.unhealthy = 0
	} else {
		c.unhealthy++
	}

	if healthy && !c.triedDir && c.CurrentTransport() == TransportTor && !c.KeepTor {
		c.triedDir = true
		c.tryDirect(ctx)
		st = c.Backend.Status()
	}

	if c.unhealthy >= c.Failures && c.CurrentTransport() == TransportDirect && c.Tor != nil {
		c.logf("coordination server unreachable directly, falling back to tor")
		c.useTor(ctx)
		c.unhealthy = 0
		return
	}

	if healthy {
		c.manageExitNode(ctx, st)
	}
}

// tryDirect drops tor if, and only if, the direct path is proven to work.
func (c *Controller) tryDirect(ctx context.Context) {
	// Probe before an exit node is set, or the probe would pass by going through the VPN.
	// This app is excluded from its own VPN, so its sockets take the direct route anyway.
	if c.Probe != nil {
		if err := c.Probe(ctx); err != nil {
			c.logf("direct path blocked, keeping tor: %v", err)
			return
		}
	}
	c.logf("direct path reachable, dropping tor")
	SetProxy("")
	c.Tor.Stop() // kills the control and DERP connections that went through it
	c.setTransport(TransportDirect)
	// Give tailscale a moment to notice the dropped connections before trusting its health.
	sleepCtx(ctx, c.DirectGrace)
	if c.waitHealthy(ctx, c.DirectVerify) {
		c.logf("direct path works, tor stopped")
		return
	}
	c.logf("lost the coordination server after going direct, rolling back to tor")
	c.useTor(ctx)
	if !c.waitHealthy(ctx, c.ControlTimeout) {
		c.logf("still no coordination server after rolling back to tor")
	}
}

func (c *Controller) waitHealthy(ctx context.Context, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if c.Backend.Status().ControlHealthy() {
			return true
		}
		if time.Now().After(deadline) || !sleepCtx(ctx, c.Poll) {
			return false
		}
	}
}

func (c *Controller) manageExitNode(ctx context.Context, st Status) {
	switch {
	case c.optedOut:
		return
	case c.server != "" && st.ExitNodeID == "":
		c.logf("exit node cleared by the user; leaving it off until the VPN restarts")
		c.optedOut, c.server = true, ""
		return
	case st.ExitNodeID != "" && st.ExitNodeID != c.server:
		if !c.isServer(st, st.ExitNodeID) {
			return // the user picked some other exit node: respect it
		}
		c.server = st.ExitNodeID
	}
	if c.server != "" && st.ExitNodeOnline {
		return
	}
	if c.server != "" {
		c.logf("exit node %s is offline, selecting another", c.server)
	}
	peer, err := c.selectServer(ctx, st)
	if err != nil {
		c.logf("no usable VPN server: %v", err)
		return
	}
	if err := c.Backend.SetExitNode(peer.ID); err != nil {
		c.logf("setting exit node %s: %v", peer.Name, err)
		return
	}
	c.server = peer.ID
	c.logf("exit node set to %s (%s)", peer.Name, peer.IP)
}

func (c *Controller) isServer(st Status, id string) bool {
	for _, p := range st.Peers {
		if p.ID == id {
			return slices.Contains(p.Tags, c.ServerTag)
		}
	}
	return false
}

// Candidates are online, approved exit nodes carrying the server tag.
func (c *Controller) Candidates(st Status) []Peer {
	var out []Peer
	for _, p := range st.Peers {
		if p.Online && p.ExitNodeOption && p.IP.IsValid() && slices.Contains(p.Tags, c.ServerTag) {
			out = append(out, p)
		}
	}
	return out
}

func (c *Controller) selectServer(ctx context.Context, st Status) (Peer, error) {
	peers := c.Candidates(st)
	if len(peers) == 0 {
		return Peer{}, fmt.Errorf("no online exit node tagged %s", c.ServerTag)
	}
	client := &http.Client{
		Timeout: c.HealthTimeout,
		Transport: &http.Transport{
			Proxy:             nil, // tailnet traffic never goes through tor
			DialContext:       c.Backend.DialTailnet,
			DisableKeepAlives: true,
		},
	}
	var best Peer
	bestLatency := time.Duration(-1)
	var failures []string
	for _, p := range peers {
		started := time.Now()
		err := checkHealth(ctx, client, netip.AddrPortFrom(p.IP, uint16(c.APIPort)))
		if err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", p.Name, err))
			continue
		}
		if latency := time.Since(started); bestLatency < 0 || latency < bestLatency {
			best, bestLatency = p, latency
		}
	}
	if bestLatency < 0 {
		return Peer{}, errors.New("no VPN server passed its health check: " + strings.Join(failures, "; "))
	}
	return best, nil
}

func checkHealth(ctx context.Context, client *http.Client, ap netip.AddrPort) error {
	req, err := http.NewRequestWithContext(ctx, "GET", "http://"+ap.String()+"/v1/health", nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var body struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return err
	}
	if body.Status != "ok" {
		return fmt.Errorf("reported %q", body.Status)
	}
	return nil
}

// DefaultProbeTargets matches src/tpn/probe.py.
var DefaultProbeTargets = []string{
	"https://controlplane.tailscale.com/key?v=71",
	"https://derp1.tailscale.com/derp/probe",
}

// DirectProbe returns a Probe that fetches each target without any proxy. Any non-5xx answer
// over verified TLS means the host is reachable.
func DirectProbe(targets []string, timeout time.Duration) func(context.Context) error {
	client := &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{Proxy: nil},
	}
	return func(ctx context.Context) error {
		var blocked []string
		for _, target := range targets {
			req, err := http.NewRequestWithContext(ctx, "GET", target, nil)
			if err != nil {
				return err
			}
			resp, err := client.Do(req)
			if err != nil {
				blocked = append(blocked, fmt.Sprintf("%s: %v", target, err))
				continue
			}
			resp.Body.Close()
			if resp.StatusCode >= 500 {
				blocked = append(blocked, fmt.Sprintf("%s: HTTP %d", target, resp.StatusCode))
			}
		}
		if len(blocked) > 0 {
			return errors.New(strings.Join(blocked, "; "))
		}
		return nil
	}
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	select {
	case <-ctx.Done():
		return false
	case <-time.After(d):
		return true
	}
}
