// Package metrics provides Prometheus-format metrics using atomic counters.
// No external dependencies are required — all metrics are exposed as
// text/plain output compatible with Prometheus scraping.
package metrics

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// HistogramBucket represents a single Prometheus histogram bucket.
type HistogramBucket struct {
	Le    string // bucket upper bound (e.g. "5.0", "+Inf")
	Count atomic.Int64
}

// EndpointMetrics holds per-endpoint counters and duration histogram.
type EndpointMetrics struct {
	Total   atomic.Int64
	Success atomic.Int64
	Errors  atomic.Int64
	Buckets []HistogramBucket
}

// Histogram bounds in milliseconds (exponential, ~1.5x factor).
var histogramBounds = []float64{
	1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192,
}

// Collector aggregates all server metrics.
type Collector struct {
	mu        sync.RWMutex
	endpoints map[string]*EndpointMetrics
	cpuTimeNs atomic.Int64

	// DB pool metrics (set externally by dbpool).
	DbActive atomic.Int64
	DbIdle   atomic.Int64
	DbQueued atomic.Int64
	DbTotal  atomic.Int64
}

// NewCollector creates a new metrics collector.
func NewCollector() *Collector {
	return &Collector{
		endpoints: make(map[string]*EndpointMetrics),
	}
}

// Endpoint returns (or creates) the metrics for the given endpoint name.
func (c *Collector) Endpoint(name string) *EndpointMetrics {
	c.mu.Lock()
	defer c.mu.Unlock()
	em, ok := c.endpoints[name]
	if !ok {
		em = &EndpointMetrics{}
		for _, b := range histogramBounds {
			em.Buckets = append(em.Buckets, HistogramBucket{
				Le: fmt.Sprintf("%.1f", b),
			})
		}
		// +Inf bucket (always present).
		em.Buckets = append(em.Buckets, HistogramBucket{Le: "+Inf"})
		c.endpoints[name] = em
	}
	return em
}

// ObserveRequest records a request with its duration and status.
func (c *Collector) ObserveRequest(endpoint string, status int, duration time.Duration) {
	em := c.Endpoint(endpoint)
	em.Total.Add(1)
	if status >= 200 && status < 400 {
		em.Success.Add(1)
	} else {
		em.Errors.Add(1)
	}

	// Record into histogram buckets.
	ms := duration.Seconds() * 1000
	for i := range em.Buckets {
		b := &em.Buckets[i]
		if b.Le == "+Inf" {
			b.Count.Add(1)
			break
		}
		v, _ := strconv.ParseFloat(b.Le, 64)
		if ms <= v {
			b.Count.Add(1)
			break
		}
	}
}

// UpdateCPUTime reads utime+stime from /proc/self/stat and stores
// the cumulative CPU time in nanoseconds.
func (c *Collector) UpdateCPUTime() {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return
	}
	fields := strings.Fields(string(data))
	if len(fields) < 15 {
		return
	}
	// fields[13] = utime (jiffies), fields[14] = stime (jiffies).
	utime, _ := strconv.ParseInt(fields[13], 10, 64)
	stime, _ := strconv.ParseInt(fields[14], 10, 64)
	// CLK_TCK is typically 100 on Linux.
	clkTck := int64(100)
	nsPerJiffy := int64(time.Second) / clkTck
	c.cpuTimeNs.Store((utime + stime) * nsPerJiffy)
}

// CPUtimeNs returns the cumulative CPU time in nanoseconds.
func (c *Collector) CPUtimeNs() int64 { return c.cpuTimeNs.Load() }

// FormatPrometheus generates a Prometheus text/plain metrics dump.
func (c *Collector) FormatPrometheus() string {
	c.UpdateCPUTime()
	c.mu.RLock()
	defer c.mu.RUnlock()

	var b strings.Builder

	// Per-endpoint request counts.
	b.WriteString("# HELP api_requests_total Total number of API requests by endpoint and status class\n")
	b.WriteString("# TYPE api_requests_total counter\n")
	for ep, em := range c.endpoints {
		b.WriteString(fmt.Sprintf("api_requests_total{endpoint=%q,status=\"2xx\"} %d\n", ep, em.Success.Load()))
		b.WriteString(fmt.Sprintf("api_requests_total{endpoint=%q,status=\"error\"} %d\n", ep, em.Errors.Load()))
	}

	// Per-endpoint duration histogram.
	b.WriteString("# HELP api_request_duration_milliseconds Request duration histogram by endpoint\n")
	b.WriteString("# TYPE api_request_duration_milliseconds histogram\n")
	for ep, em := range c.endpoints {
		for i := range em.Buckets {
			b.WriteString(fmt.Sprintf("api_request_duration_milliseconds_bucket{endpoint=%q,le=%q} %d\n",
				ep, em.Buckets[i].Le, em.Buckets[i].Count.Load()))
		}
	}

	// CPU time.
	ns := c.cpuTimeNs.Load()
	b.WriteString("# HELP process_cpu_seconds_total Cumulative process CPU time in seconds\n")
	b.WriteString("# TYPE process_cpu_seconds_total counter\n")
	b.WriteString(fmt.Sprintf("process_cpu_seconds_total %.9f\n", float64(ns)/1e9))

	// DB pool metrics.
	b.WriteString("# HELP db_connections_active Active database connections\n")
	b.WriteString("# TYPE db_connections_active gauge\n")
	b.WriteString(fmt.Sprintf("db_connections_active %d\n", c.DbActive.Load()))

	b.WriteString("# HELP db_connections_idle Idle database connections\n")
	b.WriteString("# TYPE db_connections_idle gauge\n")
	b.WriteString(fmt.Sprintf("db_connections_idle %d\n", c.DbIdle.Load()))

	b.WriteString("# HELP db_query_duration_milliseconds Database query duration (placeholder, simulated)\n")
	b.WriteString("# TYPE db_query_duration_milliseconds gauge\n")
	b.WriteString(fmt.Sprintf("db_query_duration_milliseconds %d\n", int64(0)))

	return b.String()
}
