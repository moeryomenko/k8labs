// Package query implements database query pattern simulations that produce
// measurable CPU profiles for scheduler experiments.
package query

import (
	"encoding/json"
	"math"
	"math/big"
	"math/rand"
	"sort"
	"sync"
	"time"
)

// PlanCandidate represents a simulated query plan with estimated cost.
type PlanCandidate struct {
	Description string  `json:"description"`
	Cost        float64 `json:"cost"`
	RowsScanned int     `json:"rows_scanned"`
	JoinCount   int     `json:"join_count"`
	SortReq     bool    `json:"sort_required"`
	IndexAvail  bool    `json:"index_available"`
}

// SelectResult holds the outcome of a SELECT query simulation.
type SelectResult struct {
	RowsReturned int             `json:"rows_returned"`
	Complexity   string          `json:"complexity"`
	DurationUs   int64           `json:"duration_us"`
	Plan         *PlanCandidate  `json:"plan,omitempty"`
	Plans        []PlanCandidate `json:"plans,omitempty"`
	SampleRow    json.RawMessage `json:"sample_row,omitempty"`
}

// simulatedTable holds an in-memory representation of a database table used
// for CPU-intensive scan, filter, sort, and join simulations.
type simulatedTable struct {
	mu     sync.RWMutex
	rows   []tableRow
	nextID int
}

type tableRow struct {
	ID    int
	Value float64
	Label string
	Data  [32]byte // fixed-size payload for CPU-busy work
}

// globalTable is the shared simulated table.  Its size is configured via
// DB_TABLE_SIZE at startup.
var globalTable = &simulatedTable{}

// InitTable populates the global in-memory table with n rows.
func InitTable(n int) {
	globalTable.mu.Lock()
	defer globalTable.mu.Unlock()
	globalTable.rows = make([]tableRow, n)
	globalTable.nextID = n
	for i := 0; i < n; i++ {
		globalTable.rows[i] = tableRow{
			ID:    i,
			Value: rand.Float64() * 1000,
			Label: randomLabel(),
		}
		// Fill payload with deterministic data to keep CPU busy.
		for j := 0; j < 32; j++ {
			globalTable.rows[i].Data[j] = byte((i*31 + j*17) & 0xff)
		}
	}
}

// TableSize returns the current table row count.
func TableSize() int {
	globalTable.mu.RLock()
	defer globalTable.mu.RUnlock()
	return len(globalTable.rows)
}

// randomLabel generates a short random string.
func randomLabel() string {
	const letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
	b := make([]byte, 6)
	for i := range b {
		b[i] = letters[rand.Intn(len(letters))]
	}
	return string(b)
}

// ---------------------------------------------------------------------------
// SELECT complexity implementations
// ---------------------------------------------------------------------------

// SelectSimple performs a sequential scan simulating a simple SELECT with
// no filtering or sorting beyond a straight read of n rows.
func SelectSimple(n int) SelectResult {
	globalTable.mu.RLock()
	defer globalTable.mu.RUnlock()

	rows := globalTable.rows
	if n > len(rows) {
		n = len(rows)
	}

	start := time.Now()

	// Sequential scan: copy values into output (simulates reading pages).
	var sum float64
	var maxVal float64
	_ = sum // used to prevent optimisation
	for i := 0; i < n; i++ {
		v := rows[i].Value
		sum += v
		if v > maxVal {
			maxVal = v
		}
		// Busy work: XOR the data payload.
		var acc byte
		for _, b := range rows[i].Data {
			acc ^= b
		}
		_ = acc
	}

	elapsed := time.Since(start)
	return SelectResult{
		RowsReturned: n,
		Complexity:   "simple",
		DurationUs:   elapsed.Microseconds(),
	}
}

// SelectMedium performs a scan + filter + sort simulation.
func SelectMedium(n int) SelectResult {
	globalTable.mu.RLock()
	defer globalTable.mu.RUnlock()

	rows := globalTable.rows
	if n > len(rows) {
		n = len(rows)
	}

	start := time.Now()

	// Phase 1: scan + filter (keep rows where Value > threshold).
	threshold := 500.0
	type filtered struct {
		row   tableRow
		score float64
	}
	var candidates []filtered

	for i := 0; i < n; i++ {
		r := rows[i]
		if r.Value > threshold {
			// Compute a running hash on the payload (CPU busy-work).
			var h uint64
			for _, b := range r.Data {
				h = h*31 + uint64(b)
			}
			candidates = append(candidates, filtered{row: r, score: float64(h % 1000)})
		}
	}

	// Phase 2: sort candidates by score descending.
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].score > candidates[j].score
	})

	elapsed := time.Since(start)
	return SelectResult{
		RowsReturned: len(candidates),
		Complexity:   "medium",
		DurationUs:   elapsed.Microseconds(),
	}
}

// SelectComplex performs a nested-loop join simulation + aggregation with
// big.Float.  It also runs the query planner simulation.
func SelectComplex(n int) SelectResult {
	globalTable.mu.RLock()
	defer globalTable.mu.RUnlock()

	rows := globalTable.rows
	if n > len(rows) {
		n = len(rows)
	}

	start := time.Now()

	// Phase 1: Query planner simulation (generate and evaluate plans).
	plans := generatePlans(n)
	optimal := evaluatePlans(plans)

	// Phase 2: Nested-loop join simulation.
	// Join each row with every other row in a smaller subset (log n).
	joinLimit := int(math.Log2(float64(n))) + 1
	if joinLimit > n {
		joinLimit = n
	}

	var joinSum float64
	// Use big.Float for the aggregation to add CPU load.
	agg := new(big.Float).SetPrec(256)

	for i := 0; i < joinLimit; i++ {
		rowI := rows[i]
		for j := i + 1; j < joinLimit; j++ {
			rowJ := rows[j]
			// Simulate join condition: sum of values + payload hash.
			prod := rowI.Value * rowJ.Value
			for k := 0; k < 4; k++ {
				prod *= float64(rowI.Data[k] ^ rowJ.Data[k])
			}
			joinSum += prod
			bigProd := new(big.Float).SetPrec(256).SetFloat64(prod)
			agg.Add(agg, bigProd)
		}
	}
	_ = joinSum

	// Phase 3: Format the big.Float to a string (forces computation).
	aggStr := agg.Text('g', 10)
	_ = aggStr

	elapsed := time.Since(start)
	return SelectResult{
		RowsReturned: n,
		Complexity:   "complex",
		DurationUs:   elapsed.Microseconds(),
		Plan:         &optimal,
		Plans:        plans,
	}
}

// RunSelect dispatches to the correct complexity level.
func RunSelect(n int, complexity string) SelectResult {
	switch complexity {
	case "medium":
		return SelectMedium(n)
	case "complex":
		return SelectComplex(n)
	default:
		return SelectSimple(n)
	}
}

// ---------------------------------------------------------------------------
// Query planner simulation
// ---------------------------------------------------------------------------

// generatePlans creates 3-5 random plan candidates.
func generatePlans(rows int) []PlanCandidate {
	count := 3 + rand.Intn(3) // 3-5
	plans := make([]PlanCandidate, count)
	for i := range plans {
		desc := ""
		switch rand.Intn(4) {
		case 0:
			desc = "sequential_scan"
		case 1:
			desc = "index_scan"
		case 2:
			desc = "bitmap_scan"
		case 3:
			desc = "hash_join"
		}
		plans[i] = PlanCandidate{
			Description: desc,
			RowsScanned: rows / (1 + rand.Intn(4)),
			JoinCount:   rand.Intn(4),
			SortReq:     rand.Intn(2) == 0,
			IndexAvail:  rand.Intn(2) == 0,
		}
		plans[i].Cost = costFunction(plans[i])
	}
	return plans
}

// evaluatePlans picks the plan with the lowest cost.
func evaluatePlans(plans []PlanCandidate) PlanCandidate {
	best := plans[0]
	for _, p := range plans[1:] {
		if p.Cost < best.Cost {
			best = p
		}
	}
	return best
}

// costFunction computes a weighted cost estimate for a plan candidate.
// Cost = rows_scanned * 1.0 + join_count * 10.0 + sort_penalty - index_bonus
func costFunction(p PlanCandidate) float64 {
	cost := float64(p.RowsScanned)
	cost += float64(p.JoinCount) * 10.0
	if p.SortReq {
		cost *= 1.5
	}
	if p.IndexAvail {
		cost *= 0.5
	}
	return cost
}
