package query

import (
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"
)

// MixConfig defines the percentage distribution for a workload mix.
type MixConfig struct {
	SelectPct     int `json:"select_pct"`
	InsertPct     int `json:"insert_pct"`
	UpdatePct     int `json:"update_pct"`
	CheckpointPct int `json:"checkpoint_pct"`
}

// MixType constants.
const (
	MixReadHeavy   = "read_heavy"
	MixWriteHeavy  = "write_heavy"
	MixOLTPMixed   = "oltp_mixed"
	MixBatchReport = "batch_report"
)

// mixConfigs maps mix type names to their distributions.
var mixConfigs = map[string]MixConfig{
	MixReadHeavy:   {SelectPct: 70, InsertPct: 20, UpdatePct: 0, CheckpointPct: 10},
	MixWriteHeavy:  {SelectPct: 20, InsertPct: 70, UpdatePct: 0, CheckpointPct: 10},
	MixOLTPMixed:   {SelectPct: 40, InsertPct: 30, UpdatePct: 20, CheckpointPct: 10},
	MixBatchReport: {SelectPct: 100, InsertPct: 0, UpdatePct: 0, CheckpointPct: 0},
}

// MixStats tracks aggregate statistics for a mix run.
type MixStats struct {
	QueriesExecuted int64            `json:"queries_executed"`
	AvgLatencyMs    float64          `json:"avg_latency_ms"`
	CPUTimeSeconds  float64          `json:"cpu_time_seconds"`
	QueriesByType   map[string]int64 `json:"queries_by_type"`
	MixType         string           `json:"mix_type"`
	DurationSeconds float64          `json:"duration_seconds"`
}

// MixRunner drives a workload mix for a given duration.
type MixRunner struct {
	cfg           MixConfig
	mixType       string
	duration      time.Duration
	tableSize     int
	baseLatencyMs int

	mu             sync.Mutex
	queriesByType  map[string]int64
	totalLatencyNs atomic.Int64
	totalQueries   atomic.Int64
	stopCh         chan struct{}
	stopped        atomic.Bool
}

// NewMixRunner creates a new mix runner for the given type.
func NewMixRunner(mixType string, duration time.Duration, tableSize int, baseLatencyMs int) *MixRunner {
	cfg, ok := mixConfigs[mixType]
	if !ok {
		cfg = mixConfigs[MixOLTPMixed]
		mixType = MixOLTPMixed
	}
	return &MixRunner{
		cfg:           cfg,
		mixType:       mixType,
		duration:      duration,
		tableSize:     tableSize,
		baseLatencyMs: baseLatencyMs,
		queriesByType: map[string]int64{},
		stopCh:        make(chan struct{}),
	}
}

// pickQueryType randomly selects a query type based on the configured percentages.
func (m *MixRunner) pickQueryType() string {
	r := rand.Intn(100)
	sel := m.cfg.SelectPct
	ins := sel + m.cfg.InsertPct
	upd := ins + m.cfg.UpdatePct
	switch {
	case r < sel:
		return "select"
	case r < ins:
		return "insert"
	case r < upd:
		return "update"
	default:
		return "checkpoint"
	}
}

// runQuery executes a single query based on type and simulates base latency.
func (m *MixRunner) runQuery(qtype string) {
	start := time.Now()

	// Simulate base network/context switch latency.
	baseSleep := time.Duration(m.baseLatencyMs) * time.Millisecond
	if baseSleep > 0 {
		time.Sleep(baseSleep)
	}

	// Random subset of the table for query targets.
	n := 10 + rand.Intn(100)
	if n > m.tableSize {
		n = m.tableSize
	}

	switch qtype {
	case "select":
		complexities := []string{"simple", "medium", "complex"}
		c := complexities[rand.Intn(len(complexities))]
		RunSelect(n, c)
	case "insert":
		RunInsert(n)
	case "update":
		RunUpdate(n, 1+rand.Intn(5))
	case "checkpoint":
		RunCheckpoint()
	}

	elapsed := time.Since(start)
	m.totalLatencyNs.Add(elapsed.Nanoseconds())
	m.totalQueries.Add(1)

	m.mu.Lock()
	m.queriesByType[qtype]++
	m.mu.Unlock()
}

// Run executes the workload mix for the configured duration.
// It blocks until completion and returns aggregate stats.
func (m *MixRunner) Run() MixStats {
	log.Printf("mix: starting %s workload for %.0fs", m.mixType, m.duration.Seconds())
	deadline := time.Now().Add(m.duration)

	// Launch worker goroutines.
	var wg sync.WaitGroup
	numWorkers := 4
	for w := 0; w < numWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				// Check if stopped or time is up.
				select {
				case <-m.stopCh:
					return
				default:
				}
				if time.Now().After(deadline) {
					return
				}
				qtype := m.pickQueryType()
				m.runQuery(qtype)
			}
		}()
	}
	wg.Wait()

	total := m.totalQueries.Load()
	latencyNs := m.totalLatencyNs.Load()
	avgMs := float64(0)
	if total > 0 {
		avgMs = (float64(latencyNs) / float64(total)) / float64(time.Millisecond)
	}

	m.mu.Lock()
	qbt := make(map[string]int64, len(m.queriesByType))
	for k, v := range m.queriesByType {
		qbt[k] = v
	}
	m.mu.Unlock()

	return MixStats{
		QueriesExecuted: total,
		AvgLatencyMs:    avgMs,
		CPUTimeSeconds:  0, // filled by caller from global metrics
		QueriesByType:   qbt,
		MixType:         m.mixType,
		DurationSeconds: m.duration.Seconds(),
	}
}

// Stop signals the mix runner to stop early.
func (m *MixRunner) Stop() {
	m.stopped.Store(true)
	close(m.stopCh)
}

// MixStatsJSON returns the JSON representation of MixStats.
func MixStatsJSON(s MixStats) string {
	data, _ := json.Marshal(s)
	return string(data)
}

// EnsureConfig ensures a mix configuration exists for the given type.
func EnsureConfig(mixType string) MixConfig {
	cfg, ok := mixConfigs[mixType]
	if !ok {
		fmt.Printf("mix: unknown type %q, using oltp_mixed\n", mixType)
		return mixConfigs[MixOLTPMixed]
	}
	return cfg
}
