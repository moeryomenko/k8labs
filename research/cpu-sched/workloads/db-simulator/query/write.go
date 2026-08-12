package query

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync/atomic"
	"time"
)

// WriteResult holds the outcome of an INSERT or UPDATE simulation.
type WriteResult struct {
	Operation    string `json:"operation"`
	RowsAffected int    `json:"rows_affected"`
	DurationUs   int64  `json:"duration_us"`
	WALHash      string `json:"wal_hash,omitempty"`
}

// writeSequence is a monotonic counter for simulated WAL LSN.
var writeSequence atomic.Uint64

// ---------------------------------------------------------------------------
// INSERT simulation
// ---------------------------------------------------------------------------

// RunInsert simulates an INSERT workload: data structure building, index
// maintenance (insert into sorted order), and WAL append (SHA256 hash as
// fsync proxy).
func RunInsert(rows int) WriteResult {
	start := time.Now()

	// Phase 1: Build row data structures.
	type newRow struct {
		ID    int
		Value float64
		Label string
		Data  [32]byte
	}
	batch := make([]newRow, rows)
	for i := 0; i < rows; i++ {
		seq := int(writeSequence.Add(1))
		batch[i] = newRow{
			ID:    seq,
			Value: rand.Float64() * 1000,
			Label: fmt.Sprintf("row_%d", seq),
		}
		// Fill payload.
		for j := 0; j < 32; j++ {
			batch[i].Data[j] = byte((seq*31 + j*17) & 0xff)
		}
	}

	// Phase 2: Simulate index maintenance — insert into sorted order.
	// We sort the batch by Value to simulate B-tree index insert cost.
	sort.Slice(batch, func(i, j int) bool {
		return batch[i].Value < batch[j].Value
	})

	// Phase 3: Simulate WAL append.
	// Compute a rolling SHA256 hash across the batch as a proxy for WAL fsync.
	h := sha256.New()
	for _, row := range batch {
		var buf [8]byte
		binary.LittleEndian.PutUint64(buf[:], uint64(row.ID))
		h.Write(buf[:])
		binary.LittleEndian.PutUint64(buf[:], math.Float64bits(row.Value))
		h.Write(buf[:])
		h.Write([]byte(row.Label))
		h.Write(row.Data[:])
	}
	walHash := fmt.Sprintf("%x", h.Sum(nil))

	// Add the rows to the global table.
	globalTable.mu.Lock()
	for _, row := range batch {
		globalTable.rows = append(globalTable.rows, tableRow{
			ID:    row.ID,
			Value: row.Value,
			Label: row.Label,
			Data:  row.Data,
		})
	}
	globalTable.nextID += rows
	globalTable.mu.Unlock()

	elapsed := time.Since(start)
	return WriteResult{
		Operation:    "insert",
		RowsAffected: rows,
		DurationUs:   elapsed.Microseconds(),
		WALHash:      walHash,
	}
}

// ---------------------------------------------------------------------------
// UPDATE simulation
// ---------------------------------------------------------------------------

// RunUpdate simulates UPDATE: read(select)+modify+write cycle, index update,
// and WAL append.
func RunUpdate(rows int, cols int) WriteResult {
	start := time.Now()

	globalTable.mu.Lock()
	totalRows := len(globalTable.rows)
	if totalRows == 0 {
		globalTable.mu.Unlock()
		return WriteResult{Operation: "update", RowsAffected: 0, DurationUs: time.Since(start).Microseconds()}
	}
	if rows > totalRows {
		rows = totalRows
	}

	// Phase 1: Read (select) the rows to update.
	indices := rand.Perm(totalRows)[:rows]
	selected := make([]tableRow, rows)
	for i, idx := range indices {
		selected[i] = globalTable.rows[idx]
	}

	// Phase 2: Modify values.  For each "col" we do extra CPU work.
	for i := range selected {
		// Simulate column updates by doing CPU work proportional to cols.
		for c := 0; c < cols; c++ {
			selected[i].Value = rand.Float64() * 1000
			// Hash the label to simulate encode overhead.
			h := sha256.Sum256([]byte(selected[i].Label))
			selected[i].Label = fmt.Sprintf("updated_%x", h[:4])
		}
		// Update payload bytes.
		for j := 0; j < 32; j++ {
			selected[i].Data[j] = byte((selected[i].ID*31 + j*17 + cols) & 0xff)
		}
	}

	// Phase 3: Write back.  Simulate index maintenance by sorting on ID.
	sort.Slice(selected, func(i, j int) bool {
		return selected[i].ID < selected[j].ID
	})
	for i, idx := range indices {
		globalTable.rows[idx] = selected[i]
	}

	// Phase 4: WAL append (SHA256).
	hasher := sha256.New()
	for _, row := range selected {
		var buf [8]byte
		binary.LittleEndian.PutUint64(buf[:], uint64(row.ID))
		hasher.Write(buf[:])
		binary.LittleEndian.PutUint64(buf[:], math.Float64bits(row.Value))
		hasher.Write(buf[:])
		hasher.Write([]byte(row.Label))
		hasher.Write(row.Data[:])
	}
	walHash := fmt.Sprintf("%x", hasher.Sum(nil))

	globalTable.mu.Unlock()

	elapsed := time.Since(start)
	return WriteResult{
		Operation:    "update",
		RowsAffected: rows,
		DurationUs:   elapsed.Microseconds(),
		WALHash:      walHash,
	}
}

// WriteResultJSON returns the JSON encoding of a WriteResult.
func WriteResultJSON(r WriteResult) string {
	data, _ := json.Marshal(r)
	return string(data)
}
