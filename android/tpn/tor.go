package tpn

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var bootstrapRE = regexp.MustCompile(`Bootstrapped (\d+)%`)

// Tor runs the tor binary shipped in the APK (lib/<abi>/libTor.so, from the Tor Project's
// signed expert bundle) with an HTTP CONNECT tunnel on a free loopback port.
type Tor struct {
	Binary  string        // path to libTor.so
	DataDir string        // tor's DataDirectory parent; torrc lives here
	Bridges []string      // obfs4 bridge lines; needs PT
	PT      string        // path to lyrebird, for bridges
	Timeout time.Duration // bootstrap timeout
	Logf    func(string, ...any)

	mu   sync.Mutex
	cmd  *exec.Cmd
	done chan struct{}
	port int
}

func (t *Tor) logf(format string, args ...any) {
	if t.Logf != nil {
		t.Logf("tpn: tor: "+format, args...)
	}
}

// ProxyURL is the HTTP CONNECT proxy, valid after Start.
func (t *Tor) ProxyURL() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return fmt.Sprintf("http://127.0.0.1:%d", t.port)
}

// Running reports whether the tor process is alive.
func (t *Tor) Running() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.done == nil {
		return false
	}
	select {
	case <-t.done:
		return false
	default:
		return true
	}
}

func (t *Tor) torrc(port int) (string, error) {
	lines := []string{
		"DataDirectory " + filepath.Join(t.DataDir, "data"),
		"SocksPort 0",
		fmt.Sprintf("HTTPTunnelPort 127.0.0.1:%d", port),
		"Log notice stdout",
		"ClientOnly 1",
		"AvoidDiskWrites 1",
		// tor exits by itself if the app process dies, so a crash never leaves it running.
		fmt.Sprintf("__OwningControllerProcess %d", os.Getpid()),
	}
	if len(t.Bridges) > 0 {
		if t.PT == "" {
			return "", errors.New("bridges are set but no pluggable transport is available")
		}
		lines = append(lines, "UseBridges 1", "ClientTransportPlugin obfs4 exec "+t.PT)
		for _, b := range t.Bridges {
			if strings.ContainsAny(b, "\r\n") {
				return "", fmt.Errorf("invalid bridge line %q", b)
			}
			lines = append(lines, "Bridge "+strings.TrimSpace(b))
		}
	}
	return strings.Join(lines, "\n") + "\n", nil
}

// Start launches tor and blocks until it reports 100% bootstrap.
func (t *Tor) Start(ctx context.Context) error {
	if t.Running() {
		return nil
	}
	port, err := freePort()
	if err != nil {
		return err
	}
	rc, err := t.torrc(port)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(t.DataDir, "data"), 0o700); err != nil {
		return err
	}
	rcPath := filepath.Join(t.DataDir, "torrc")
	if err := os.WriteFile(rcPath, []byte(rc), 0o600); err != nil {
		return err
	}

	cmd := exec.Command(t.Binary, "-f", rcPath)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting tor: %w", err)
	}
	done := make(chan struct{})
	progress := make(chan int, 16)
	var tailMu sync.Mutex
	var tail []string
	go func() {
		sc := bufio.NewScanner(stdout)
		for sc.Scan() {
			line := sc.Text()
			tailMu.Lock()
			tail = append(tail, line)
			if len(tail) > 10 {
				tail = tail[1:]
			}
			tailMu.Unlock()
			if m := bootstrapRE.FindStringSubmatch(line); m != nil {
				var pct int
				fmt.Sscanf(m[1], "%d", &pct)
				select {
				case progress <- pct:
				default:
				}
			}
		}
		cmd.Wait()
		close(done)
	}()
	t.mu.Lock()
	t.cmd, t.done, t.port = cmd, done, port
	t.mu.Unlock()
	t.logf("started (HTTP tunnel on 127.0.0.1:%d)", port)

	timeout := t.Timeout
	if timeout == 0 {
		timeout = 3 * time.Minute
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	lastLog := func() string {
		tailMu.Lock()
		defer tailMu.Unlock()
		return strings.Join(tail, " | ")
	}
	for {
		select {
		case pct := <-progress:
			t.logf("bootstrap %d%%", pct)
			if pct == 100 {
				return nil
			}
		case <-done:
			return fmt.Errorf("tor exited during bootstrap: %s", lastLog())
		case <-timer.C:
			t.Stop()
			return fmt.Errorf("tor did not bootstrap within %v: %s", timeout, lastLog())
		case <-ctx.Done():
			t.Stop()
			return ctx.Err()
		}
	}
}

// Stop kills tor and waits for it to exit.
func (t *Tor) Stop() {
	t.mu.Lock()
	cmd, done := t.cmd, t.done
	t.cmd, t.done = nil, nil
	t.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return
	}
	cmd.Process.Kill()
	<-done
	t.logf("stopped")
}

func freePort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port, nil
}

// NativeLibDir finds the directory the APK's native libraries were extracted to, from the
// path libgojni.so was loaded from. It needs useLegacyPackaging (extractNativeLibs) so that
// libTor.so exists as a real, executable file there.
func NativeLibDir() (string, error) {
	f, err := os.Open("/proc/self/maps")
	if err != nil {
		return "", err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 6 {
			continue
		}
		p := fields[len(fields)-1]
		if strings.HasSuffix(p, "/libgojni.so") && !strings.Contains(p, "!") {
			return filepath.Dir(p), nil
		}
	}
	return "", errors.New("libgojni.so is not mapped from an extracted file")
}
