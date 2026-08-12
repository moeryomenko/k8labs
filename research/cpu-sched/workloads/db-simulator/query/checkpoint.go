package query

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync"
	"time"
)

// CheckpointResult holds the outcome of a simulated checkpoint.
type CheckpointResult struct {
	DurationMs      float64 `json:"duration_ms"`
	PagesFlushed    int     `json:"pages_flushed"`
	WALBytes        int     `json:"wal_bytes"`
	Checksum        string  `json:"checksum"`
	PhaseRampUpMs   float64 `json:"phase_ramp_up_ms"`
	PhasePlateauMs  float64 `json:"phase_plateau_ms"`
	PhaseRampDownMs float64 `json:"phase_ramp_down_ms"`
}

// checkpointPage represents a simulated database page in memory.
type checkpointPage struct {
	ID       uint64
	Data     [4096]byte // 4 KB page
	Checksum [32]byte
}

// RunCheckpoint simulates a full checkpoint CPU spike:
//   - Ramp-up: build write list (sort in-memory pages by ID)
//   - Plateau: compute SHA256 checksums for each page, sort pages by checksum
//   - Ramp-down: metadata write (serialize summary)
func RunCheckpoint() CheckpointResult {
	overallStart := time.Now()

	// Determine the checkpoint size based on current table size.
	globalTable.mu.RLock()
	tableSize := len(globalTable.rows)
	globalTable.mu.RUnlock()

	// Number of pages scales with table size.
	numPages := max(min(50+(tableSize/100), 500), 10)

	// -----------------------------------------------------------------------
	// Phase 1: Ramp-up (~1s) — build the write list (sort pages by ID).
	// -----------------------------------------------------------------------
	rampUpStart := time.Now()
	pages := make([]checkpointPage, numPages)
	for i := range pages {
		pages[i].ID = rand.Uint64()
		// Fill with pseudo-random data (simulates dirty pages).
		for j := range pages[i].Data {
			pages[i].Data[j] = byte((i*4096 + j) & 0xff)
		}
	}
	// Sort by ID to simulate building a sorted write list (B-tree order).
	sort.Slice(pages, func(i, j int) bool {
		return pages[i].ID < pages[j].ID
	})

	// Ramp-up: busy-wait to ensure ~1s ramp.
	rampUpElapsed := time.Since(rampUpStart)
	targetRampUp := 800 * time.Millisecond
	if rampUpElapsed < targetRampUp {
		spinWait(targetRampUp - rampUpElapsed)
	}
	rampUpElapsed = time.Since(rampUpStart)

	// -----------------------------------------------------------------------
	// Phase 2: Plateau (2-5s) — compute SHA256 checksums and sort by them.
	// -----------------------------------------------------------------------
	plateauStart := time.Now()

	// Compute SHA256 checksum for each page.
	var wg sync.WaitGroup
	sem := make(chan struct{}, 4) // limited concurrency for CPU-bound work
	for i := range pages {
		wg.Add(1)
		i := i
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			pages[i].Checksum = sha256.Sum256(pages[i].Data[:])
		}()
	}
	wg.Wait()

	// Sort pages by checksum (simulates reordering for write efficiency).
	sort.Slice(pages, func(i, j int) bool {
		// Compare first 8 bytes of checksum as uint64.
		ci := binary.LittleEndian.Uint64(pages[i].Checksum[:8])
		cj := binary.LittleEndian.Uint64(pages[j].Checksum[:8])
		return ci < cj
	})

	// Plateau: ensure at least 2 seconds of work.  If the above finished
	// quickly, do additional CPU work (extra checksum rounds).
	plateauElapsed := time.Since(plateauStart)
	minPlateau := 2 * time.Second
	maxPlateau := 5 * time.Second
	if plateauElapsed < minPlateau {
		// Spin with additional SHA256 computations.
		extraStart := time.Now()
		for time.Since(extraStart) < minPlateau-plateauElapsed {
			var buf [64]byte
			for i := range buf {
				buf[i] = byte(rand.Intn(256))
			}
			_ = sha256.Sum256(buf[:])
		}
	}
	// If plateau is already long enough, cap it at maxPlateau.
	if plateauElapsed > maxPlateau {
		// Already exceeded max — nothing to do, we logged the time.
	}
	plateauElapsed = time.Since(plateauStart)

	// -----------------------------------------------------------------------
	// Phase 3: Ramp-down (~1s) — metadata write (compute aggregate checksum).
	// -----------------------------------------------------------------------
	rampDownStart := time.Now()

	// Compute a single aggregate checksum over all page checksums.
	aggHasher := sha256.New()
	for _, p := range pages {
		aggHasher.Write(p.Checksum[:])
	}
	aggChecksum := fmt.Sprintf("%x", aggHasher.Sum(nil))

	// Ramp-down: busy-wait to ensure ~1s.
	rampDownElapsed := time.Since(rampDownStart)
	targetRampDown := 800 * time.Millisecond
	if rampDownElapsed < targetRampDown {
		spinWait(targetRampDown - rampDownElapsed)
	}
	rampDownElapsed = time.Since(rampDownStart)

	overallElapsed := time.Since(overallStart)
	return CheckpointResult{
		DurationMs:      overallElapsed.Seconds() * 1000,
		PagesFlushed:    numPages,
		WALBytes:        numPages * 4096,
		Checksum:        aggChecksum,
		PhaseRampUpMs:   rampUpElapsed.Seconds() * 1000,
		PhasePlateauMs:  plateauElapsed.Seconds() * 1000,
		PhaseRampDownMs: rampDownElapsed.Seconds() * 1000,
	}
}

// spinWait burns CPU in a tight loop for the given duration.
func spinWait(d time.Duration) {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		_ = math.Sqrt(float64(time.Now().UnixNano()))
	}
}
