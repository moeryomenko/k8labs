// Package dbpool provides a simulated database connection pool with
// configurable latency, concurrency limits, and Prometheus metrics.
//
// The pool supports two modes:
//   - ModeLocal (default): simulates query latency in-process
//   - ModeRemote: makes HTTP calls to a remote db-simulator service
package dbpool

import (
	"fmt"
	"math/rand"
	"net/http"
	"sync"
	"sync/atomic"
	"time"
)

const (
	// ModeLocal uses simulated latency (default).
	ModeLocal = "local"
	// ModeRemote makes HTTP calls to a remote db-simulator service.
	ModeRemote = "remote"
)

// Pool simulates a database connection pool with goroutine-based
// concurrency control and configurable query latency.
type Pool struct {
	mu         sync.Mutex
	sem        chan struct{}
	minLatency time.Duration
	maxLatency time.Duration
	rng        *rand.Rand
	mode       string

	// Remote mode fields (set by SetRemote, immutable after startup).
	client  *http.Client
	baseURL string

	active atomic.Int64
	idle   atomic.Int64
	queued atomic.Int64
	total  atomic.Int64
}

// New creates a Pool with the given size and latency bounds.
// The pool starts in local mode by default.
func New(size int, minLatency, maxLatency time.Duration) *Pool {
	p := &Pool{
		sem:        make(chan struct{}, size),
		minLatency: minLatency,
		maxLatency: maxLatency,
		rng:        rand.New(rand.NewSource(time.Now().UnixNano())),
		mode:       ModeLocal,
	}
	// Fill the semaphore to represent idle connections.
	for i := 0; i < size; i++ {
		p.sem <- struct{}{}
	}
	p.idle.Store(int64(size))
	return p
}

// SetRemote configures the pool for remote mode targeting a db-simulator
// service at the given host and port. Must be called before serving requests.
func (p *Pool) SetRemote(host, port string, timeout time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.mode = ModeRemote
	p.baseURL = fmt.Sprintf("http://%s:%s", host, port)
	p.client = &http.Client{Timeout: timeout}
}

// Exec simulates executing a database query of the given kind.
//
// In local mode, it acquires a connection from the pool, waits for simulated
// latency, then releases the connection.
//
// In remote mode, it makes an HTTP call to the db-simulator service mapped
// from the query kind.
func (p *Pool) Exec(kind string) {
	if p.mode == ModeRemote {
		p.execRemote(kind)
		return
	}
	p.execLocal(kind)
}

// execLocal simulates query latency in-process.
func (p *Pool) execLocal(kind string) {
	p.queued.Add(1)
	<-p.sem // acquire (blocks until a slot is free)
	p.queued.Add(-1)

	p.idle.Add(-1)
	p.active.Add(1)
	p.total.Add(1)

	// Simulate query latency.
	p.mu.Lock()
	latency := p.minLatency + time.Duration(p.rng.Int63n(int64(p.maxLatency-p.minLatency+1)))
	p.mu.Unlock()
	time.Sleep(latency)

	p.active.Add(-1)
	p.idle.Add(1)
	p.sem <- struct{}{} // release
}

// execRemote makes an HTTP call to the db-simulator service.
func (p *Pool) execRemote(kind string) {
	p.queued.Add(1)
	<-p.sem // acquire (blocks until a slot is free)
	p.queued.Add(-1)

	p.idle.Add(-1)
	p.active.Add(1)
	p.total.Add(1)

	endpoint, method := p.remoteEndpoint(kind)
	req, err := http.NewRequest(method, p.baseURL+endpoint, nil)
	if err == nil {
		resp, err := p.client.Do(req)
		if err == nil {
			resp.Body.Close()
		}
	}

	p.active.Add(-1)
	p.idle.Add(1)
	p.sem <- struct{}{} // release
}

// remoteEndpoint maps query kind strings to db-simulator endpoints.
func (p *Pool) remoteEndpoint(kind string) (string, string) {
	switch kind {
	case "SELECT users":
		return "/query/select?rows=100&complexity=simple", http.MethodGet
	case "SELECT orders":
		return "/query/select?rows=200&complexity=medium", http.MethodGet
	case "INSERT orders":
		return "/query/insert?rows=1", http.MethodPost
	case "SELECT documents":
		return "/query/select?rows=500&complexity=complex", http.MethodGet
	case "SELECT report_aggregate", "SELECT report_metadata":
		return "/query/select?rows=1000&complexity=complex", http.MethodGet
	default:
		return "/query/select?rows=100&complexity=simple", http.MethodGet
	}
}

// Active returns the number of currently active connections.
func (p *Pool) Active() int64 { return p.active.Load() }

// Idle returns the number of currently idle connections.
func (p *Pool) Idle() int64 { return p.idle.Load() }

// Queued returns the number of queries waiting for a connection.
func (p *Pool) Queued() int64 { return p.queued.Load() }

// Total returns the total number of queries executed.
func (p *Pool) Total() int64 { return p.total.Load() }

// Size returns the pool capacity.
func (p *Pool) Size() int { return cap(p.sem) }
