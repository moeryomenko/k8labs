// cpu-burner — HTTP server that burns CPU on demand.
//
// Endpoints:
//
//	GET /                          — health check, returns "OK"
//	GET /load?percent=N&duration=M — async CPU burn at N% for M seconds
//	GET /fibonacci?n=N             — compute fibonacci(N) recursively
//	GET /pi?digits=N               — compute pi to N digits
//	GET /stats                     — cumulative process CPU time
//	GET /metrics                   — Prometheus-format metrics
//
// Signals: SIGTERM triggers graceful shutdown.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"math"
	"math/big"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

type metrics struct {
	requestsTotal   atomic.Int64
	durationSeconds atomic.Int64 // cumulative nanoseconds, exposed as seconds
	fibonacciCalls  atomic.Int64
	cpuTimeSeconds  atomic.Int64 // cumulative nanoseconds from /proc/self/stat
}

func (m *metrics) incRequests()                { m.requestsTotal.Add(1) }
func (m *metrics) addDuration(d time.Duration) { m.durationSeconds.Add(d.Nanoseconds()) }
func (m *metrics) incFibonacci()               { m.fibonacciCalls.Add(1) }

// updateCPUTime reads utime+stime from /proc/self/stat.
func (m *metrics) updateCPUTime() {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return
	}
	fields := strings.Fields(string(data))
	if len(fields) < 15 {
		return
	}
	// fields[13] = utime (jiffies), fields[14] = stime (jiffies)
	utime, _ := strconv.ParseInt(fields[13], 10, 64)
	stime, _ := strconv.ParseInt(fields[14], 10, 64)
	// Convert jiffies to nanoseconds: jiffy = 1/CLK_TCK second
	// CLK_TCK is typically 100 on Linux.
	clkTck := int64(100)
	nsPerJiffy := int64(time.Second) / clkTck
	m.cpuTimeSeconds.Store((utime + stime) * nsPerJiffy)
}

func (m *metrics) formatPrometheus() string {
	m.updateCPUTime()
	var b strings.Builder

	// Requests total
	b.WriteString("# HELP cpu_burner_requests_total Total number of requests\n")
	b.WriteString("# TYPE cpu_burner_requests_total counter\n")
	b.WriteString(fmt.Sprintf("cpu_burner_requests_total %d\n", m.requestsTotal.Load()))

	// Duration seconds (cumulative)
	b.WriteString("# HELP cpu_burner_duration_seconds Cumulative request processing time in seconds\n")
	b.WriteString("# TYPE cpu_burner_duration_seconds counter\n")
	ns := m.durationSeconds.Load()
	b.WriteString(fmt.Sprintf("cpu_burner_duration_seconds %.9f\n", float64(ns)/1e9))

	// Fibonacci calls
	b.WriteString("# HELP cpu_burner_fibonacci_calls Total number of fibonacci computations\n")
	b.WriteString("# TYPE cpu_burner_fibonacci_calls counter\n")
	b.WriteString(fmt.Sprintf("cpu_burner_fibonacci_calls %d\n", m.fibonacciCalls.Load()))

	// CPU time seconds (process cumulative)
	b.WriteString("# HELP cpu_burner_cpu_time_seconds Cumulative CPU time used by the process in seconds\n")
	b.WriteString("# TYPE cpu_burner_cpu_time_seconds counter\n")
	ns = m.cpuTimeSeconds.Load()
	b.WriteString(fmt.Sprintf("cpu_burner_cpu_time_seconds %.9f\n", float64(ns)/1e9))

	return b.String()
}

// ---------------------------------------------------------------------------
// CPU burner
// ---------------------------------------------------------------------------

// burnCPU spins on a locked OS thread, consuming approximately p% of a core.
// It stops when ctx is cancelled or duration has elapsed.
func burnCPU(ctx context.Context, p int) {
	if p <= 0 {
		return
	}
	if p > 100 {
		p = 100
	}
	done := ctx.Done()

	busy := time.Duration(p) * time.Millisecond / 100
	idle := time.Duration(100-p) * time.Millisecond / 100

	for {
		select {
		case <-done:
			return
		default:
		}
		// Busy-wait loop for the "busy" portion.
		deadline := time.Now().Add(busy)
		for time.Now().Before(deadline) {
			// Prevent the compiler from optimising away the loop.
			_ = math.Sqrt(float64(time.Now().UnixNano()))
		}
		// Sleep for the idle portion (or context cancellation).
		select {
		case <-done:
			return
		case <-time.After(idle):
		}
	}
}

// startCPUBurn launches a goroutine (locked to OS thread) that burns CPU
// at the given percentage for the given duration.
func startCPUBurn(percent, durationSec int) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(durationSec)*time.Second)
	go func() {
		defer cancel()
		// Pin to OS thread for accurate cgroup CPU accounting.
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		burnCPU(ctx, percent)
	}()
}

// ---------------------------------------------------------------------------
// Fibonacci (recursive)
// ---------------------------------------------------------------------------

// fibRecursive computes fibonacci(N) recursively using *big.Int.
// It panics for n < 0.
func fibRecursive(n int) *big.Int {
	if n < 0 {
		panic("fibonacci: n must be non-negative")
	}
	if n <= 1 {
		return big.NewInt(int64(n))
	}
	var a, b big.Int
	a.Set(fibRecursive(n - 1))
	b.Set(fibRecursive(n - 2))
	return a.Add(&a, &b)
}

// ---------------------------------------------------------------------------
// Pi computation (Machin-like formula with big.Float)
// ---------------------------------------------------------------------------

// piMachin computes pi to approximately digits decimal places using the
// Machin-like formula:
//
//	pi/4 = 4*arctan(1/5) - arctan(1/239)
//
// arctan(x) is computed via the Taylor series:
//
//	arctan(x) = x - x^3/3 + x^5/5 - x^7/7 + ...
func piMachin(digits int) string {
	if digits < 1 {
		digits = 1
	}
	// Each decimal digit requires ~log2(10) ≈ 3.322 bits.
	prec := uint(digits * 4) // extra margin for rounding
	pi := new(big.Float).SetPrec(prec)

	// arctan(1/5)
	arc5 := arctanBig(new(big.Float).SetPrec(prec).SetInt64(5), prec)
	// arctan(1/239)
	arc239 := arctanBig(new(big.Float).SetPrec(prec).SetInt64(239), prec)

	// pi = 4*(4*arc5 - arc239)
	four := new(big.Float).SetPrec(prec).SetInt64(4)
	t4a := new(big.Float).SetPrec(prec).Mul(four, arc5)
	diff := new(big.Float).SetPrec(prec).Sub(t4a, arc239)
	pi.Mul(four, diff)

	// Format to the requested number of decimal places.
	s := pi.Text('f', digits)
	return s
}

// arctanBig computes arctan(1/x) using the Taylor series.
func arctanBig(x *big.Float, prec uint) *big.Float {
	// We compute arctan(1/x) = 1/x - 1/(3*x^3) + 1/(5*x^5) - ...
	one := new(big.Float).SetPrec(prec).SetInt64(1)
	invX := new(big.Float).SetPrec(prec).Quo(one, x) // 1/x
	xSq := new(big.Float).SetPrec(prec).Mul(x, x)    // x^2 (we'll multiply invX by invX^2 each iteration)

	result := new(big.Float).SetPrec(prec).Set(invX)
	term := new(big.Float).SetPrec(prec).Set(invX)
	invXSq := new(big.Float).SetPrec(prec).Quo(one, xSq) // 1/x^2

	// For N decimal digits, we need ~N/log10(25) terms for arctan(1/5).
	// With extra margin, iterate until the term is negligible.
	maxIter := 2 * int(prec) // safe upper bound
	sign := int64(-1)

	for i := 3; i < maxIter; i += 2 {
		// term *= -1 / x^2
		term.Mul(term, invXSq)
		termNeg := new(big.Float).SetPrec(prec).Neg(term)
		divisor := new(big.Float).SetPrec(prec).SetInt64(int64(i))

		delta := new(big.Float).SetPrec(prec).Quo(termNeg, divisor)
		if sign > 0 {
			delta.Neg(delta)
		}

		// If the term is smaller than 1e-(prec/3), stop.
		eps := new(big.Float).SetPrec(prec).SetInt64(1)
		eps.Quo(eps, new(big.Float).SetPrec(prec).SetInt64(int64(math.Pow10(int(prec/3)+1))))

		if delta.Cmp(eps) < 0 && delta.Sign() >= 0 {
			result.Add(result, delta)
			break
		}
		if delta.Sign() < 0 {
			deltaNeg := new(big.Float).SetPrec(prec).Neg(delta)
			if deltaNeg.Cmp(eps) < 0 {
				result.Add(result, delta)
				break
			}
		}

		result.Add(result, delta)
		sign = -sign
	}
	return result
}

// ---------------------------------------------------------------------------
// HTTP handlers
// ---------------------------------------------------------------------------

type server struct {
	mux     *http.ServeMux
	metrics *metrics
	server  *http.Server
}

func newServer() *server {
	s := &server{
		mux:     http.NewServeMux(),
		metrics: &metrics{},
	}
	s.mux.HandleFunc("/", s.handleHealth)
	s.mux.HandleFunc("/load", s.handleLoad)
	s.mux.HandleFunc("/fibonacci", s.handleFibonacci)
	s.mux.HandleFunc("/pi", s.handlePi)
	s.mux.HandleFunc("/stats", s.handleStats)
	s.mux.HandleFunc("/metrics", s.handleMetrics)
	return s
}

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, "OK")
}

func (s *server) handleLoad(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	percentStr := r.URL.Query().Get("percent")
	durationStr := r.URL.Query().Get("duration")

	percent, err := strconv.Atoi(percentStr)
	if err != nil || percent < 1 || percent > 100 {
		http.Error(w, "query param 'percent' must be integer 1-100", http.StatusBadRequest)
		return
	}
	duration, err := strconv.Atoi(durationStr)
	if err != nil || duration < 1 {
		http.Error(w, "query param 'duration' must be positive integer (seconds)", http.StatusBadRequest)
		return
	}
	startCPUBurn(percent, duration)
	w.Header().Set("Content-Type", "text/plain")
	fmt.Fprintf(w, "CPU burn started: %d%% for %ds\n", percent, duration)
}

func (s *server) handleFibonacci(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	nStr := r.URL.Query().Get("n")
	n, err := strconv.Atoi(nStr)
	if err != nil || n < 0 {
		http.Error(w, "query param 'n' must be non-negative integer", http.StatusBadRequest)
		return
	}
	if n > 40 {
		// Beyond 40 the recursive implementation becomes extremely slow
		// and may exhaust memory. Reject large values.
		http.Error(w, "n must be <= 40 (recursive implementation is exponential)", http.StatusBadRequest)
		return
	}

	s.metrics.incFibonacci()
	start := time.Now()
	result := fibRecursive(n)
	elapsed := time.Since(start)
	s.metrics.addDuration(elapsed)

	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"n":%d,"result":"%s","elapsed":"%s"}`+"\n", n, result.String(), elapsed)
}

func (s *server) handlePi(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	digitsStr := r.URL.Query().Get("digits")
	digits, err := strconv.Atoi(digitsStr)
	if err != nil || digits < 1 {
		http.Error(w, "query param 'digits' must be positive integer", http.StatusBadRequest)
		return
	}
	if digits > 10000 {
		http.Error(w, "digits must be <= 10000", http.StatusBadRequest)
		return
	}

	start := time.Now()
	pi := piMachin(digits)
	elapsed := time.Since(start)
	s.metrics.addDuration(elapsed)

	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"digits":%d,"result":"%s","elapsed":"%s"}`+"\n", digits, pi, elapsed)
}

func (s *server) handleStats(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	s.metrics.updateCPUTime()
	ns := s.metrics.cpuTimeSeconds.Load()

	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"cpu_time_seconds":%.9f}`+"\n", float64(ns)/1e9)
}

func (s *server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	s.metrics.incRequests()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprint(w, s.metrics.formatPrometheus())
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	s := newServer()
	addr := ":8080"
	s.server = &http.Server{
		Addr:    addr,
		Handler: s.mux,
	}

	// Graceful shutdown on SIGTERM.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM)

	go func() {
		log.Printf("cpu-burner listening on %s", addr)
		if err := s.server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("ListenAndServe: %v", err)
		}
	}()

	<-stop
	log.Println("SIGTERM received, shutting down gracefully...")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := s.server.Shutdown(ctx); err != nil {
		log.Fatalf("Shutdown: %v", err)
	}
	log.Println("server stopped")
}
