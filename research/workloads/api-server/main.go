// api-server — HTTP API server workload with realistic patterns for CPU
// throttling experiments.
//
// Endpoints:
//
//	GET  /api/v1/users             — Light CPU: JSON serialization
//	GET  /api/v1/orders            — Medium CPU: filter + sort + paginate
//	POST /api/v1/orders            — Light CPU: order creation
//	GET  /api/v1/search            — Variable CPU: string matching + ranking
//	POST /api/v1/reports           — Heavy CPU: big.Float aggregation
//	GET  /health                   — Liveness probe
//	GET  /ready                    — Readiness probe
//	GET  /metrics                  — Prometheus-format metrics
//
// All endpoints are configurable via environment variables:
//
//	API_DB_MODE              — DB mode: "local" (default) or "remote"
//	API_DB_SERVICE_HOST      — db-simulator hostname (default "db-simulator")
//	API_DB_SERVICE_PORT      — db-simulator port (default "8080")
//	API_DB_TIMEOUT_MS        — HTTP client timeout for remote mode (default 5000)
//	API_DB_LATENCY_MIN_MS    — minimum DB latency (default 5)
//	API_DB_LATENCY_MAX_MS    — maximum DB latency (default 20)
//	API_DB_POOL_SIZE         — connection pool size (default 10)
//	API_COMPLEXITY_FACTOR    — CPU intensity multiplier (0.1-3.0, default 1.0)
//	API_PORT                 — HTTP listen port (default 8080)
//
// Signals: SIGTERM triggers graceful shutdown.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/eryoma/k8labs/research/workloads/api-server/dbpool"
	"github.com/eryoma/k8labs/research/workloads/api-server/handlers"
	"github.com/eryoma/k8labs/research/workloads/api-server/metrics"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("api-server: %v", err)
	}
}

func run() error {
	// --- Configuration from environment ---
	port := envInt("API_PORT", 8080)
	dbPoolSize := envInt("API_DB_POOL_SIZE", 10)
	dbLatencyMin := time.Duration(envInt("API_DB_LATENCY_MIN_MS", 5)) * time.Millisecond
	dbLatencyMax := time.Duration(envInt("API_DB_LATENCY_MAX_MS", 20)) * time.Millisecond
	complexityFactor := envFloat("API_COMPLEXITY_FACTOR", 1.0)

	// Clamp complexity factor to [0.1, 3.0].
	if complexityFactor < 0.1 {
		complexityFactor = 0.1
	}
	if complexityFactor > 3.0 {
		complexityFactor = 3.0
	}

	log.Printf("config: port=%d pool_size=%d latency=[%v,%v] complexity=%.2f",
		port, dbPoolSize, dbLatencyMin, dbLatencyMax, complexityFactor)

	// --- DB Pool configuration ---
	pool := dbpool.New(dbPoolSize, dbLatencyMin, dbLatencyMax)

	dbMode := envStr("API_DB_MODE", dbpool.ModeLocal)
	if dbMode == dbpool.ModeRemote {
		dbHost := envStr("API_DB_SERVICE_HOST", "db-simulator")
		dbPort := envStr("API_DB_SERVICE_PORT", "8080")
		dbTimeout := time.Duration(envInt("API_DB_TIMEOUT_MS", 5000)) * time.Millisecond
		pool.SetRemote(dbHost, dbPort, dbTimeout)
		log.Printf("db: remote mode, host=%s port=%s timeout=%v", dbHost, dbPort, dbTimeout)
	} else {
		log.Printf("db: local mode")
	}

	// --- Components ---
	collector := metrics.NewCollector()

	mux := http.NewServeMux()

	// Register API handlers.
	mux.Handle("/api/v1/users", instrumented("users", collector,
		handlers.NewUserHandler(pool, complexityFactor)))
	mux.Handle("/api/v1/orders", instrumented("orders", collector,
		handlers.NewOrderHandler(pool, complexityFactor)))
	mux.Handle("/api/v1/search", instrumented("search", collector,
		handlers.NewSearchHandler(pool, complexityFactor)))
	mux.Handle("/api/v1/reports", instrumented("reports", collector,
		handlers.NewReportHandler(pool, complexityFactor)))

	// Health / readiness.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ready"}`))
	})

	// Prometheus metrics. Must NOT be wrapped in instrumented() to avoid
	// infinite recursion (the scraper also generates a request).
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		// Sync DB pool metrics before format.
		collector.DbActive.Store(pool.Active())
		collector.DbIdle.Store(pool.Idle())
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprint(w, collector.FormatPrometheus())
	})

	// Background goroutine to periodically update DB pool metrics.
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				collector.DbActive.Store(pool.Active())
				collector.DbIdle.Store(pool.Idle())
			case <-done:
				return
			}
		}
	}()

	// --- HTTP server ---
	addr := fmt.Sprintf(":%d", port)
	srv := &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	// Graceful shutdown on SIGTERM.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)

	go func() {
		log.Printf("api-server listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("ListenAndServe: %v", err)
		}
	}()

	<-stop
	log.Println("signal received, shutting down gracefully...")

	close(done)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		return fmt.Errorf("shutdown: %w", err)
	}
	log.Println("server stopped")
	return nil
}

// instrumented wraps an http.Handler with request metrics collection.
func instrumented(endpoint string, c *metrics.Collector, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		// Wrap ResponseWriter to capture status code.
		lw := &loggingWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(lw, r)
		elapsed := time.Since(start)
		c.ObserveRequest(endpoint, lw.status, elapsed)
	})
}

// loggingWriter wraps http.ResponseWriter to capture the status code.
type loggingWriter struct {
	http.ResponseWriter
	status int
}

func (lw *loggingWriter) WriteHeader(code int) {
	lw.status = code
	lw.ResponseWriter.WriteHeader(code)
}

// envInt reads an environment variable as an integer with a default value.
func envInt(key string, defaultVal int) int {
	if val := os.Getenv(key); val != "" {
		if parsed, err := strconv.Atoi(val); err == nil {
			return parsed
		}
	}
	return defaultVal
}

// envFloat reads an environment variable as a float64 with a default value.
func envFloat(key string, defaultVal float64) float64 {
	if val := os.Getenv(key); val != "" {
		if parsed, err := strconv.ParseFloat(val, 64); err == nil {
			return parsed
		}
	}
	return defaultVal
}

// envStr reads an environment variable as a string with a default value.
func envStr(key string, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}
