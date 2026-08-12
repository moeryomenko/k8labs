package handlers

import (
	"encoding/json"
	"math"
	"math/big"
	"math/rand"
	"net/http"
	"time"
)

// ReportHandler handles POST /api/v1/reports (heavy CPU).
// Simulates data aggregation with big.Float arithmetic for
// statistical computations and matrix-like operations.
type ReportHandler struct {
	db         QueryExecutor
	complexity float64
}

// NewReportHandler creates a ReportHandler.
func NewReportHandler(db QueryExecutor, complexity float64) *ReportHandler {
	return &ReportHandler{db: db, complexity: complexity}
}

func (h *ReportHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	period := r.URL.Query().Get("period")
	dimension := r.URL.Query().Get("dimension")

	if period == "" {
		period = "daily"
	}
	if dimension == "" {
		dimension = "revenue"
	}

	// Simulate multiple DB queries for report data.
	h.db.Exec("SELECT report_aggregate")
	h.db.Exec("SELECT report_metadata")

	// Scale computation with complexity factor.
	scale := min(max(int(50*h.complexity), 10), 200)

	// Generate synthetic data points.
	rng := rand.New(rand.NewSource(time.Now().UnixNano() - 3000))
	data := make([]float64, scale)
	for i := range data {
		data[i] = rng.Float64() * 10000
	}

	// Heavy CPU: compute statistical aggregates using big.Float.
	stats := computeStats(data, period, dimension)

	// Additional heavy computation: simulate covariance matrix.
	covMatrix := computeCovarianceSimulation(data, period)

	resp := map[string]any{
		"period":            period,
		"dimension":         dimension,
		"data_points":       len(data),
		"statistics":        stats,
		"covariance_matrix": covMatrix,
		"computed_at":       time.Now().UTC(),
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// Stats holds computed statistical aggregates.
type Stats struct {
	Mean     string `json:"mean"`
	StdDev   string `json:"std_dev"`
	Variance string `json:"variance"`
	Skewness string `json:"skewness"`
	Kurtosis string `json:"kurtosis"`
	Min      string `json:"min"`
	Max      string `json:"max"`
	P50      string `json:"p50"`
	P95      string `json:"p95"`
	P99      string `json:"p99"`
}

// computeStats calculates statistical aggregates over data using big.Float.
func computeStats(data []float64, _, _ string) Stats {
	prec := uint(128) // 128-bit precision for CPU intensity
	n := new(big.Float).SetPrec(prec).SetInt64(int64(len(data)))

	sum := new(big.Float).SetPrec(prec)
	sumSq := new(big.Float).SetPrec(prec)
	cubeSum := new(big.Float).SetPrec(prec)
	fourthSum := new(big.Float).SetPrec(prec)

	min := new(big.Float).SetPrec(prec).SetFloat64(data[0])
	max := new(big.Float).SetPrec(prec).SetFloat64(data[0])

	// First pass: sum, min, max.
	for _, v := range data {
		f := new(big.Float).SetPrec(prec).SetFloat64(v)
		sum.Add(sum, f)
		if f.Cmp(min) < 0 {
			min.Set(f)
		}
		if f.Cmp(max) > 0 {
			max.Set(f)
		}
	}

	// Compute mean.
	mean := new(big.Float).SetPrec(prec).Quo(sum, n)

	// Second pass: variance, skewness, kurtosis.
	zero := new(big.Float).SetPrec(prec)
	for _, v := range data {
		f := new(big.Float).SetPrec(prec).SetFloat64(v)
		diff := new(big.Float).SetPrec(prec).Sub(f, mean)

		// diff^2
		diff2 := new(big.Float).SetPrec(prec).Mul(diff, diff)
		sumSq.Add(sumSq, diff2)

		// diff^3
		diff3 := new(big.Float).SetPrec(prec).Mul(diff2, diff)
		cubeSum.Add(cubeSum, diff3)

		// diff^4
		diff4 := new(big.Float).SetPrec(prec).Mul(diff3, diff)
		fourthSum.Add(fourthSum, diff4)
	}

	variance := new(big.Float).SetPrec(prec).Quo(sumSq, n)
	stdDev := new(big.Float).SetPrec(prec).Sqrt(variance)

	// Skewness = (cubeSum / n) / (stdDev^3)
	var skewness *big.Float
	if stdDev.Cmp(zero) > 0 {
		stdDev3 := new(big.Float).SetPrec(prec).Mul(stdDev, stdDev)
		stdDev3.Mul(stdDev3, stdDev)
		skewness = new(big.Float).SetPrec(prec).Quo(
			new(big.Float).SetPrec(prec).Quo(cubeSum, n),
			stdDev3,
		)
	} else {
		skewness = new(big.Float).SetPrec(prec)
	}

	// Kurtosis = (fourthSum / n) / (variance^2) - 3
	var kurtosis *big.Float
	if variance.Cmp(zero) > 0 {
		varSq := new(big.Float).SetPrec(prec).Mul(variance, variance)
		kurtosis = new(big.Float).SetPrec(prec).Quo(
			new(big.Float).SetPrec(prec).Quo(fourthSum, n),
			varSq,
		)
		three := new(big.Float).SetPrec(prec).SetInt64(3)
		kurtosis.Sub(kurtosis, three)
	} else {
		kurtosis = new(big.Float).SetPrec(prec)
	}

	// Percentiles via interpolation on sorted copy.
	p50 := percentile(data, 0.50, prec)
	p95 := percentile(data, 0.95, prec)
	p99 := percentile(data, 0.99, prec)

	return Stats{
		Mean:     formatBigFloat(mean, 6),
		StdDev:   formatBigFloat(stdDev, 6),
		Variance: formatBigFloat(variance, 6),
		Skewness: formatBigFloat(skewness, 6),
		Kurtosis: formatBigFloat(kurtosis, 6),
		Min:      formatBigFloat(min, 6),
		Max:      formatBigFloat(max, 6),
		P50:      formatBigFloat(p50, 6),
		P95:      formatBigFloat(p95, 6),
		P99:      formatBigFloat(p99, 6),
	}
}

// percentile computes the p-th percentile using linear interpolation.
func percentile(data []float64, p float64, prec uint) *big.Float {
	if len(data) == 0 {
		return new(big.Float).SetPrec(prec)
	}
	sorted := make([]float64, len(data))
	copy(sorted, data)
	// Simple insertion sort (CPU-intentional, small n).
	for i := 1; i < len(sorted); i++ {
		key := sorted[i]
		j := i - 1
		for j >= 0 && sorted[j] > key {
			sorted[j+1] = sorted[j]
			j--
		}
		sorted[j+1] = key
	}

	rank := p * float64(len(sorted)-1)
	lower := int(math.Floor(rank))
	upper := int(math.Ceil(rank))
	if lower == upper {
		return new(big.Float).SetPrec(prec).SetFloat64(sorted[lower])
	}
	frac := rank - float64(lower)
	val := sorted[lower]*(1-frac) + sorted[upper]*frac
	return new(big.Float).SetPrec(prec).SetFloat64(val)
}

// computeCovarianceSimulation simulates covariance matrix computation
// using big.Float arithmetic for deliberate CPU intensity.
func computeCovarianceSimulation(data []float64, _ string) [][]string {
	// Simulate 5 dimensions of data.
	numDims := 5
	mat := make([][]string, numDims)
	prec := uint(128)
	n := new(big.Float).SetPrec(prec).SetInt64(int64(len(data)))

	for i := range numDims {
		mat[i] = make([]string, numDims)
		for j := range numDims {
			sum := new(big.Float).SetPrec(prec)
			for k := 1; k < len(data); k++ {
				xi := new(big.Float).SetPrec(prec).SetFloat64(data[k] * float64(i+1))
				xj := new(big.Float).SetPrec(prec).SetFloat64(data[k-1] * float64(j+1))
				sum.Add(sum, new(big.Float).SetPrec(prec).Mul(xi, xj))
			}
			meanMul := new(big.Float).SetPrec(prec).Quo(sum, n)
			mat[i][j] = formatBigFloat(meanMul, 4)
		}
	}
	return mat
}

// formatBigFloat formats a big.Float to the given decimal precision.
func formatBigFloat(f *big.Float, precision int) string {
	return f.Text('f', precision)
}
