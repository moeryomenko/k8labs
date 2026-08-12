// db-simulator — PostgreSQL-like database workload simulator for CPU
// throttling experiments.
//
// HTTP Endpoints:
//
//	GET  /query/select          — Simulates SELECT query with configurable complexity
//	POST /query/insert           — Simulates INSERT workload with WAL simulation
//	POST /query/update           — Simulates UPDATE workload with WAL simulation
//	GET  /query/checkpoint       — Triggers simulated checkpoint CPU spike
//	GET  /mix                    — Runs workload mix for a duration
//	GET  /stats                  — Cumulative query statistics
//	GET  /health                 — Health check
//	GET  /ready                  — Readiness probe
//	GET  /metrics                — Prometheus-format metrics
//
// Signals: SIGTERM triggers graceful shutdown.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/eryoma/k8labs/research/workloads/db-simulator/metrics"
	"github.com/eryoma/k8labs/research/workloads/db-simulator/pool"
	"github.com/eryoma/k8labs/research/workloads/db-simulator/query"
)

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

type config struct {
	tableSize      int
	checkpointIntS int
	poolSize       int
	workloadType   string
	baseLatencyMs  int
	port           string
}

func loadConfig() config {
	return config{
		tableSize:      envInt("DB_TABLE_SIZE", 10000),
		checkpointIntS: envInt("DB_CHECKPOINT_INTERVAL_S", 30),
		poolSize:       envInt("DB_POOL_SIZE", 10),
		workloadType:   envStr("DB_WORKLOAD_TYPE", "oltp_mixed"),
		baseLatencyMs:  envInt("DB_QUERY_LATENCY_BASE_MS", 1),
		port:           envStr("DB_PORT", "8080"),
	}
}

func envInt(key string, defaultVal int) int {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return defaultVal
	}
	return v
}

func envStr(key string, defaultVal string) string {
	s := os.Getenv(key)
	if s == "" {
		return defaultVal
	}
	return s
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

type server struct {
	cfg  config
	mux  *http.ServeMux
	met  *metrics.Metrics
	pool *pool.Pool
	srv  *http.Server

	mixRunning     atomic.Bool
	checkpointLast atomic.Pointer[query.CheckpointResult]
}

func newServer(cfg config) *server {
	s := &server{
		cfg: cfg,
		mux: http.NewServeMux(),
		met: metrics.New(),
	}
	s.pool = pool.New(cfg.poolSize, cfg.poolSize*2)

	s.mux.HandleFunc("/query/select", s.handleSelect)
	s.mux.HandleFunc("/query/insert", s.handleInsert)
	s.mux.HandleFunc("/query/update", s.handleUpdate)
	s.mux.HandleFunc("/query/checkpoint", s.handleCheckpoint)
	s.mux.HandleFunc("/mix", s.handleMix)
	s.mux.HandleFunc("/stats", s.handleStats)
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/ready", s.handleReady)
	s.mux.HandleFunc("/metrics", s.handleMetrics)

	return s
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, `{"status":"ok"}`)
}

func (s *server) handleReady(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, `{"status":"ready"}`)
}

func (s *server) handleSelect(w http.ResponseWriter, r *http.Request) {
	rows := queryParamInt(r, "rows", s.cfg.tableSize)
	complexity := r.URL.Query().Get("complexity")
	if complexity == "" {
		complexity = "simple"
	}

	start := time.Now()

	ctx := r.Context()
	done := make(chan struct{})
	var result query.SelectResult
	if err := s.pool.Submit(ctx, func() {
		result = query.RunSelect(rows, complexity)
		close(done)
	}); err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	// Wait for the pool to finish the job.
	select {
	case <-done:
	case <-ctx.Done():
		http.Error(w, ctx.Err().Error(), http.StatusGatewayTimeout)
		return
	}

	elapsed := time.Since(start)
	s.met.IncQueries(metrics.TypeSelect)
	s.met.ObserveDuration(metrics.TypeSelect, elapsed)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func (s *server) handleInsert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	rows := queryParamInt(r, "rows", 100)

	start := time.Now()
	done := make(chan struct{})
	var result query.WriteResult
	ctx := r.Context()
	if err := s.pool.Submit(ctx, func() {
		result = query.RunInsert(rows)
		close(done)
	}); err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	select {
	case <-done:
	case <-ctx.Done():
		http.Error(w, ctx.Err().Error(), http.StatusGatewayTimeout)
		return
	}

	elapsed := time.Since(start)
	s.met.IncQueries(metrics.TypeInsert)
	s.met.ObserveDuration(metrics.TypeInsert, elapsed)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func (s *server) handleUpdate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST required", http.StatusMethodNotAllowed)
		return
	}
	rows := queryParamInt(r, "rows", 100)
	cols := queryParamInt(r, "cols", 1)

	start := time.Now()
	done := make(chan struct{})
	var result query.WriteResult
	ctx := r.Context()
	if err := s.pool.Submit(ctx, func() {
		result = query.RunUpdate(rows, cols)
		close(done)
	}); err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	select {
	case <-done:
	case <-ctx.Done():
		http.Error(w, ctx.Err().Error(), http.StatusGatewayTimeout)
		return
	}

	elapsed := time.Since(start)
	s.met.IncQueries(metrics.TypeUpdate)
	s.met.ObserveDuration(metrics.TypeUpdate, elapsed)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func (s *server) handleCheckpoint(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	result := query.RunCheckpoint()
	elapsed := time.Since(start)

	s.met.IncCheckpoint(elapsed)

	s.checkpointLast.Store(&result)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

func (s *server) handleMix(w http.ResponseWriter, r *http.Request) {
	if s.mixRunning.Load() {
		http.Error(w, "mix already running", http.StatusConflict)
		return
	}

	mixType := r.URL.Query().Get("type")
	if mixType == "" {
		mixType = s.cfg.workloadType
	}
	durationSec := queryParamInt(r, "duration", 30)

	s.mixRunning.Store(true)
	defer s.mixRunning.Store(false)

	runner := query.NewMixRunner(mixType, time.Duration(durationSec)*time.Second, s.cfg.tableSize, s.cfg.baseLatencyMs)
	stats := runner.Run()
	stats.CPUTimeSeconds = s.met.CPUTimeSeconds()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(stats)
}

func (s *server) handleStats(w http.ResponseWriter, r *http.Request) {
	byType := s.met.QueriesByType()
	resp := map[string]any{
		"total_queries":    s.met.TotalQueries(),
		"avg_latency_ms":   s.met.AvgLatencyMs(),
		"cpu_time_seconds": s.met.CPUTimeSeconds(),
		"queries_by_type":  byType,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (s *server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	// Update pool metrics before rendering.
	s.met.SetPoolActive(s.pool.Active())
	s.met.SetPoolQueued(s.pool.Queued())

	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprint(w, s.met.FormatPrometheus())
}

// ---------------------------------------------------------------------------
// Background checkpoint loop
// ---------------------------------------------------------------------------

func (s *server) backgroundCheckpoint(ctx context.Context) {
	ticker := time.NewTicker(time.Duration(s.cfg.checkpointIntS) * time.Second)
	defer ticker.Stop()

	log.Printf("checkpoint: running every %ds", s.cfg.checkpointIntS)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			start := time.Now()
			result := query.RunCheckpoint()
			elapsed := time.Since(start)
			s.met.IncCheckpoint(elapsed)
			s.checkpointLast.Store(&result)
			log.Printf("checkpoint: completed in %.0fms (%d pages, %d WAL bytes)",
				result.DurationMs, result.PagesFlushed, result.WALBytes)
		}
	}
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

func queryParamInt(r *http.Request, key string, defaultVal int) int {
	s := r.URL.Query().Get(key)
	if s == "" {
		return defaultVal
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return defaultVal
	}
	if v < 1 {
		return 1
	}
	return v
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	cfg := loadConfig()

	// Initialise the global simulated table.
	query.InitTable(cfg.tableSize)
	log.Printf("db-simulator: table initialised with %d rows", cfg.tableSize)

	s := newServer(cfg)
	addr := ":" + cfg.port
	s.srv = &http.Server{
		Addr:    addr,
		Handler: s.mux,
	}

	// Background checkpoint loop.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.backgroundCheckpoint(ctx)

	// Graceful shutdown on SIGTERM.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM)

	go func() {
		log.Printf("db-simulator listening on %s", addr)
		log.Printf("config: table_size=%d checkpoint_interval=%d pool_size=%d workload=%s",
			cfg.tableSize, cfg.checkpointIntS, cfg.poolSize, cfg.workloadType)
		if err := s.srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("ListenAndServe: %v", err)
		}
	}()

	<-stop
	log.Println("SIGTERM received, shutting down gracefully...")
	cancel() // stop background checkpoint
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := s.srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("Shutdown: %v", err)
	}
	s.pool.Stop()
	log.Println("db-simulator stopped")
}
