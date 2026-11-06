package tpn

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func fakeTorBinary(t *testing.T, script string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("needs a POSIX shell")
	}
	p := filepath.Join(t.TempDir(), "libTor.so")
	if err := os.WriteFile(p, []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestTorWaitsForFullBootstrap(t *testing.T) {
	bin := fakeTorBinary(t, `echo "Bootstrapped 10% (conn)"; echo "Bootstrapped 100% (done)"; exec sleep 30`)
	tor := &Tor{Binary: bin, DataDir: t.TempDir(), Timeout: 5 * time.Second}
	if err := tor.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !tor.Running() || !strings.HasPrefix(tor.ProxyURL(), "http://127.0.0.1:") {
		t.Fatalf("running %v url %s", tor.Running(), tor.ProxyURL())
	}
	rc, _ := os.ReadFile(filepath.Join(tor.DataDir, "torrc"))
	for _, want := range []string{"SocksPort 0", "HTTPTunnelPort 127.0.0.1:", "__OwningControllerProcess"} {
		if !strings.Contains(string(rc), want) {
			t.Errorf("torrc lacks %q", want)
		}
	}
	tor.Stop()
	if tor.Running() {
		t.Fatal("still running after Stop")
	}
}

func TestTorExitDuringBootstrapIsReported(t *testing.T) {
	bin := fakeTorBinary(t, `echo "[err] bad torrc"; exit 1`)
	tor := &Tor{Binary: bin, DataDir: t.TempDir(), Timeout: 5 * time.Second}
	err := tor.Start(context.Background())
	if err == nil || !strings.Contains(err.Error(), "bad torrc") {
		t.Fatalf("err = %v", err)
	}
}

func TestTorBootstrapTimeout(t *testing.T) {
	bin := fakeTorBinary(t, `echo "Bootstrapped 5%"; exec sleep 30`)
	tor := &Tor{Binary: bin, DataDir: t.TempDir(), Timeout: 200 * time.Millisecond}
	if err := tor.Start(context.Background()); err == nil || !strings.Contains(err.Error(), "within") {
		t.Fatalf("err = %v", err)
	}
	if tor.Running() {
		t.Fatal("timed-out tor must be stopped")
	}
}

func TestTorrcRejectsInjectedDirectives(t *testing.T) {
	tor := &Tor{PT: "/lib/lyrebird", Bridges: []string{"1.2.3.4:443\nExitNodes {us}"}}
	if _, err := tor.torrc(9080); err == nil {
		t.Fatal("expected error")
	}
}
