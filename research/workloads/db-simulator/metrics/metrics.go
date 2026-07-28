// Package metrics provides Prometheus-format metric collection for the
// database workload simulator.
package metrics

import (
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// HistogramBuckets defines exponential histogram bucket boundaries in
// milliseconds.  Buckets span 0.25ms to ~32s.
var HistogramBuckets = []float64{
	0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512,
	1024, 2048, 4096, 8192, 16384, 32768,
}

// QueryType labels for metrics.
const (
	TypeSelect     = "select"
	TypeInsert     = "insert"
	TypeUpdate     = "update"
	TypeCheckpoint = "checkpoint"
)

// Metrics accumulates all db-simulator metrics.
type Metrics struct {
	mu sync.Mutex

	queriesTotal       atomic.Int64
	queryDurationNanos atomic.Int64 // cumulative nanoseconds
	queriesByType      map[string]*atomic.Int64

	// Per-type histogram counters.  Each entry maps type -> bucket-index -> count.
	histograms map[string][]uint64

	checkpointCount      atomic.Int64
	checkpointDurationNs atomic.Int64

	poolActive atomic.Int64
	poolQueued atomic.Int64

	cpuTimeSeconds atomic.Int64 // cumulative nanoseconds from /proc/self/stat
}

// New returns an initialised Metrics.
func New() *Metrics {
	return &Metrics{
		queriesByType: map[string]*atomic.Int64{
			TypeSelect:     {},
			TypeInsert:     {},
			TypeUpdate:     {},
			TypeCheckpoint: {},
		},
		histograms: map[string][]uint64{
			TypeSelect:     make([]uint64, len(HistogramBuckets)+1),
			TypeInsert:     make([]uint64, len(HistogramBuckets)+1),
			TypeUpdate:     make([]uint64, len(HistogramBuckets)+1),
			TypeCheckpoint: make([]uint64, len(HistogramBuckets)+1),
		},
	}
}

// IncQueries increments the total and per-type query counters.
func (m *Metrics) IncQueries(qtype string) {
	m.queriesTotal.Add(1)
	if c, ok := m.queriesByType[qtype]; ok {
		c.Add(1)
	}
}

// ObserveDuration records a query duration in the per-type histogram.
func (m *Metrics) ObserveDuration(qtype string, d time.Duration) {
	m.queryDurationNanos.Add(d.Nanoseconds())
	ms := float64(d) / float64(time.Millisecond)
	bucket := bucketIndex(ms)
	m.mu.Lock()
	if h, ok := m.histograms[qtype]; ok {
		if bucket < len(h) {
			h[bucket]++
		}
	}
	m.mu.Unlock()
}

// IncCheckpoint records a checkpoint event.
func (m *Metrics) IncCheckpoint(d time.Duration) {
	m.checkpointCount.Add(1)
	m.checkpointDurationNs.Add(d.Nanoseconds())
	m.IncQueries(TypeCheckpoint)
	m.ObserveDuration(TypeCheckpoint, d)
}

// SetPoolActive sets the current active connection count.
func (m *Metrics) SetPoolActive(n int64) {
	m.poolActive.Store(n)
}

// SetPoolQueued sets the current queue depth.
func (m *Metrics) SetPoolQueued(n int64) {
	m.poolQueued.Store(n)
}

// TotalQueries returns the cumulative query count.
func (m *Metrics) TotalQueries() int64 { return m.queriesTotal.Load() }

// AvgLatencyMs returns the average query latency in milliseconds.
func (m *Metrics) AvgLatencyMs() float64 {
	total := m.queriesTotal.Load()
	if total == 0 {
		return 0
	}
	ns := m.queryDurationNanos.Load()
	return (float64(ns) / float64(total)) / float64(time.Millisecond)
}

// CPUTimeSeconds returns cumulative process CPU time in seconds.
func (m *Metrics) CPUTimeSeconds() float64 {
	m.updateCPUTime()
	ns := m.cpuTimeSeconds.Load()
	return float64(ns) / 1e9
}

// QueriesByType returns a copy of per-type query counts.
func (m *Metrics) QueriesByType() map[string]int64 {
	out := make(map[string]int64, len(m.queriesByType))
	for k, c := range m.queriesByType {
		out[k] = c.Load()
	}
	return out
}

// FormatPrometheus renders all metrics in Prometheus text format.
func (m *Metrics) FormatPrometheus() string {
	m.updateCPUTime()
	m.mu.Lock()
	defer m.mu.Unlock()

	var b strings.Builder

	// --- db_queries_total ---
	b.WriteString("# HELP db_queries_total Total number of database queries executed.\n")
	b.WriteString("# TYPE db_queries_total counter\n")
	for k, c := range m.queriesByType {
		b.WriteString(fmt.Sprintf("db_queries_total{type=%q} %d\n", k, c.Load()))
	}

	// --- db_query_duration_milliseconds histogram ---
	b.WriteString("# HELP db_query_duration_milliseconds Query duration in milliseconds.\n")
	b.WriteString("# TYPE db_query_duration_milliseconds histogram\n")
	for qtype, buckets := range m.histograms {
		var cum uint64
		for i, count := range buckets {
			cum += count
			upper := math.Inf(1)
			if i < len(HistogramBuckets) {
				upper = HistogramBuckets[i]
			}
			le := fmt.Sprintf("%g", upper)
			b.WriteString(fmt.Sprintf("db_query_duration_milliseconds_bucket{type=%q,le=%q} %d\n", qtype, le, cum))
		}
		b.WriteString(fmt.Sprintf("db_query_duration_milliseconds_count{type=%q} %d\n", qtype, cum))
	}

	// --- db_checkpoint ---
	b.WriteString("# HELP db_checkpoint_duration_milliseconds Duration of the last checkpoint in ms.\n")
	b.WriteString("# TYPE db_checkpoint_duration_milliseconds gauge\n")
	ckNs := m.checkpointDurationNs.Load()
	b.WriteString(fmt.Sprintf("db_checkpoint_duration_milliseconds %g\n", float64(ckNs)/1e6))

	// --- pool ---
	b.WriteString("# HELP db_pool_active_connections Currently active pool connections.\n")
	b.WriteString("# TYPE db_pool_active_connections gauge\n")
	b.WriteString(fmt.Sprintf("db_pool_active_connections %d\n", m.poolActive.Load()))

	b.WriteString("# HELP db_pool_queue_depth Current query queue depth.\n")
	b.WriteString("# TYPE db_pool_queue_depth gauge\n")
	b.WriteString(fmt.Sprintf("db_pool_queue_depth %d\n", m.poolQueued.Load()))

	// --- process CPU ---
	b.WriteString("# HELP process_cpu_seconds_total Total user and system CPU time in seconds.\n")
	b.WriteString("# TYPE process_cpu_seconds_total counter\n")
	ns := m.cpuTimeSeconds.Load()
	b.WriteString(fmt.Sprintf("process_cpu_seconds_total %.9f\n", float64(ns)/1e9))

	return b.String()
}

// bucketIndex returns the histogram bucket index for a value in milliseconds.
func bucketIndex(v float64) int {
	for i, b := range HistogramBuckets {
		if v <= b {
			return i
		}
	}
	return len(HistogramBuckets)
}

// updateCPUTime reads utime+stime from /proc/self/stat.
func (m *Metrics) updateCPUTime() {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return
	}
	fields := strings.Fields(string(data))
	if len(fields) < 15 {
		return
	}
	utime, _ := strconv.ParseInt(fields[13], 10, 64)
	stime, _ := strconv.ParseInt(fields[14], 10, 64)
	clkTck := int64(100)
	nsPerJiffy := int64(time.Second) / clkTck
	m.cpuTimeSeconds.Store((utime + stime) * nsPerJiffy)
}
