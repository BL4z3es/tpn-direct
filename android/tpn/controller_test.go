package tpn

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

const torProxy = "http://127.0.0.1:9080"

type fakeTor struct {
	mu      sync.Mutex
	running bool
	starts  int
}

func (t *fakeTor) Start(context.Context) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.running = true
	t.starts++
	return nil
}
func (t *fakeTor) Stop()            { t.mu.Lock(); t.running = false; t.mu.Unlock() }
func (t *fakeTor) Running() bool    { t.mu.Lock(); defer t.mu.Unlock(); return t.running }
func (t *fakeTor) ProxyURL() string { return torProxy }

// fakeBackend reports control health depending on the active proxy, like the real thing.
type fakeBackend struct {
	mu           sync.Mutex
	running      bool
	directWorks  bool
	torWorks     bool
	peers        []Peer
	exitNode     string
	exitOnline   bool
	setCalls     []string
	healthServer *httptest.Server
	healthy      map[string]bool // peer name -> /v1/health says ok
}

func (b *fakeBackend) Status() Status {
	b.mu.Lock()
	defer b.mu.Unlock()
	st := Status{Running: b.running, Peers: b.peers, ExitNodeID: b.exitNode, ExitNodeOnline: b.exitOnline}
	viaTor := proxy.Load() != nil
	if (viaTor && !b.torWorks) || (!viaTor && !b.directWorks) {
		st.Health = []string{"Unable to connect to the Tailscale coordination server"}
	}
	return st
}

func (b *fakeBackend) SetExitNode(id string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.setCalls = append(b.setCalls, id)
	b.exitNode, b.exitOnline = id, true
	return nil
}

// DialTailnet sends every peer to one local test server and tells it which peer was meant.
func (b *fakeBackend) DialTailnet(ctx context.Context, network, addr string) (net.Conn, error) {
	host, _, _ := net.SplitHostPort(addr)
	for _, p := range b.peers {
		if p.IP.String() == host && !b.healthy[p.Name] {
			return nil, errors.New("connection refused")
		}
	}
	var d net.Dialer
	return d.DialContext(ctx, network, b.healthServer.Listener.Addr().String())
}

func newFixture(t *testing.T) (*Controller, *fakeBackend, *fakeTor) {
	t.Helper()
	proxy.Store(nil)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"status":"ok"}`)
	}))
	t.Cleanup(srv.Close)
	b := &fakeBackend{
		running: true, directWorks: true, torWorks: true, healthServer: srv,
		healthy: map[string]bool{"vpn1": true, "vpn2": true},
		peers: []Peer{
			server("vpn1", "100.64.0.10"),
			server("vpn2", "100.64.0.11"),
			{ID: "nLaptop", Name: "laptop", IP: netip.MustParseAddr("100.64.0.5"), Online: true},
		},
	}
	tor := &fakeTor{}
	c := &Controller{
		Backend: b, Tor: tor,
		Probe:          func(context.Context) error { return nil },
		Interval:       time.Millisecond,
		DirectVerify:   50 * time.Millisecond,
		ControlTimeout: 50 * time.Millisecond,
		DirectGrace:    time.Millisecond,
		Poll:           time.Millisecond,
		Logf:           t.Logf,
	}
	return c, b, tor
}

func server(name, ip string) Peer {
	return Peer{
		ID: "n" + name, Name: name, IP: netip.MustParseAddr(ip), Online: true,
		Tags: []string{"tag:vpn-server"}, ExitNodeOption: true,
	}
}

func TestStartRoutesTailscaleThroughTor(t *testing.T) {
	c, _, tor := newFixture(t)
	c.Start(context.Background())
	if c.CurrentTransport() != TransportTor || !tor.Running() {
		t.Fatalf("transport %q, tor running %v", c.CurrentTransport(), tor.Running())
	}
	got, _ := proxyFor(mustURL("https://controlplane.tailscale.com/"))
	if got == nil || got.String() != torProxy {
		t.Fatalf("control traffic proxy = %v", got)
	}
	if got, _ := proxyFor(mustURL("http://127.0.0.1:41112/")); got != nil {
		t.Fatalf("loopback must not be proxied, got %v", got)
	}
}

func TestHappyPathGoesDirectAndPicksAServer(t *testing.T) {
	c, b, tor := newFixture(t)
	c.Start(context.Background())
	c.Tick(context.Background())
	if c.CurrentTransport() != TransportDirect || tor.Running() || proxy.Load() != nil {
		t.Fatalf("expected direct with tor stopped; transport %q", c.CurrentTransport())
	}
	if len(b.setCalls) != 1 || !strings.HasPrefix(b.setCalls[0], "nvpn") {
		t.Fatalf("exit node calls %v", b.setCalls)
	}
}

func TestBlockedProbeKeepsTor(t *testing.T) {
	c, b, tor := newFixture(t)
	c.Probe = func(context.Context) error { return errors.New("blocked") }
	c.Start(context.Background())
	c.Tick(context.Background())
	if c.CurrentTransport() != TransportTor || !tor.Running() {
		t.Fatal("tor must stay when the probe fails")
	}
	if len(b.setCalls) != 1 {
		t.Fatal("the exit node is still set over tor")
	}
}

func TestDirectFailureRollsBackToTor(t *testing.T) {
	c, b, tor := newFixture(t)
	b.directWorks = false
	c.Start(context.Background())
	c.Tick(context.Background())
	if c.CurrentTransport() != TransportTor || !tor.Running() || tor.starts != 2 {
		t.Fatalf("transport %q starts %d", c.CurrentTransport(), tor.starts)
	}
}

func TestKeepTorNeverProbes(t *testing.T) {
	c, _, _ := newFixture(t)
	c.KeepTor = true
	c.Probe = func(context.Context) error { t.Fatal("probed"); return nil }
	c.Start(context.Background())
	c.Tick(context.Background())
	if c.CurrentTransport() != TransportTor {
		t.Fatal("keep tor")
	}
}

func TestWatchdogFallsBackToTorAfterRepeatedLoss(t *testing.T) {
	c, b, tor := newFixture(t)
	c.Start(context.Background())
	c.Tick(context.Background()) // goes direct
	b.mu.Lock()
	b.directWorks = false
	b.mu.Unlock()
	for range 3 {
		c.Tick(context.Background())
	}
	if c.CurrentTransport() != TransportTor || !tor.Running() {
		t.Fatal("expected fallback to tor")
	}
}

func TestNoTorConnectsDirectly(t *testing.T) {
	c, b, _ := newFixture(t)
	c.Tor = nil
	c.Start(context.Background())
	c.Tick(context.Background())
	if c.CurrentTransport() != TransportDirect || len(b.setCalls) != 1 {
		t.Fatal("direct without tor")
	}
}

func TestOfflineExitNodeIsReplaced(t *testing.T) {
	c, b, _ := newFixture(t)
	c.Start(context.Background())
	c.Tick(context.Background())
	first := b.setCalls[0]
	b.mu.Lock()
	b.exitOnline = false
	for i := range b.peers {
		if b.peers[i].ID == first {
			b.peers[i].Online = false
		}
	}
	b.mu.Unlock()
	c.Tick(context.Background())
	if len(b.setCalls) != 2 || b.setCalls[1] == first {
		t.Fatalf("exit node calls %v", b.setCalls)
	}
}

func TestUnhealthyServerIsSkipped(t *testing.T) {
	c, b, _ := newFixture(t)
	b.healthy["vpn1"] = false
	c.Start(context.Background())
	c.Tick(context.Background())
	if len(b.setCalls) != 1 || b.setCalls[0] != "nvpn2" {
		t.Fatalf("exit node calls %v", b.setCalls)
	}
}

func TestUserChoicesAreRespected(t *testing.T) {
	c, b, _ := newFixture(t)
	c.Start(context.Background())
	c.Tick(context.Background())

	// The user turns the exit node off: leave it off.
	b.mu.Lock()
	b.exitNode = ""
	b.mu.Unlock()
	c.Tick(context.Background())
	c.Tick(context.Background())
	if len(b.setCalls) != 1 {
		t.Fatalf("re-enabled an exit node the user cleared: %v", b.setCalls)
	}

	// After the VPN restarts, tpn manages it again.
	b.mu.Lock()
	b.running = false
	b.mu.Unlock()
	c.Tick(context.Background())
	b.mu.Lock()
	b.running = true
	b.mu.Unlock()
	c.Tick(context.Background())
	if len(b.setCalls) != 2 {
		t.Fatalf("expected a fresh selection after restart: %v", b.setCalls)
	}

	// A non-server exit node the user picked is left alone.
	b.mu.Lock()
	b.exitNode, b.exitOnline = "nLaptop", true
	b.mu.Unlock()
	c.Tick(context.Background())
	if len(b.setCalls) != 2 {
		t.Fatalf("overrode the user's exit node: %v", b.setCalls)
	}
}

func TestNotRunningDoesNothing(t *testing.T) {
	c, b, _ := newFixture(t)
	b.running = false
	c.Start(context.Background())
	c.Tick(context.Background())
	if len(b.setCalls) != 0 || c.CurrentTransport() != TransportTor {
		t.Fatal("must wait for login")
	}
}

func TestDirectProbe(t *testing.T) {
	ok := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden) // any non-5xx answer means reachable
	}))
	defer ok.Close()
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer bad.Close()
	if err := DirectProbe([]string{ok.URL}, time.Second)(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := DirectProbe([]string{ok.URL, bad.URL}, time.Second)(context.Background()); err == nil {
		t.Fatal("expected 502 to count as blocked")
	}
}

func TestControlHealthy(t *testing.T) {
	if (Status{Running: true, Health: []string{"some unrelated warning"}}).ControlHealthy() != true {
		t.Fatal("unrelated warnings are healthy")
	}
	if (Status{Running: true, Health: []string{"not connected to control"}}).ControlHealthy() {
		t.Fatal("lost control is unhealthy")
	}
	if (Status{}).ControlHealthy() {
		t.Fatal("not running is unhealthy")
	}
}
