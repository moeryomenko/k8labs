package handlers

import (
	"encoding/json"
	"math/rand"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Document represents a searchable document record.
type Document struct {
	ID      int      `json:"id"`
	Title   string   `json:"title"`
	Content string   `json:"content"`
	Tags    []string `json:"tags"`
	Score   float64  `json:"score"`
}

// SearchHandler handles GET /api/v1/search (variable CPU).
// Performs string matching with Levenshtein-based relevance scoring
// and result aggregation. CPU scales with query complexity.
type SearchHandler struct {
	db         QueryExecutor
	complexity float64
}

// NewSearchHandler creates a SearchHandler.
func NewSearchHandler(db QueryExecutor, complexity float64) *SearchHandler {
	return &SearchHandler{db: db, complexity: complexity}
}

func (h *SearchHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	query := r.URL.Query().Get("q")
	docType := r.URL.Query().Get("type")
	pageStr := r.URL.Query().Get("page")

	if query == "" {
		http.Error(w, "query parameter 'q' is required", http.StatusBadRequest)
		return
	}

	page := 1
	if v, err := strconv.Atoi(pageStr); err == nil && v > 0 {
		page = v
	}
	perPage := 10

	h.db.Exec("SELECT documents")

	// Scale dataset with complexity.
	docCount := min(max(int(100*h.complexity), 20), 500)

	corpus := generateDocuments(docCount, docType)

	// Filter by type if specified.
	if docType != "" {
		filtered := make([]Document, 0, len(corpus))
		for _, d := range corpus {
			for _, t := range d.Tags {
				if strings.EqualFold(t, docType) {
					filtered = append(filtered, d)
					break
				}
			}
		}
		corpus = filtered
	}

	// Score each document using relevance matching.
	queryLower := strings.ToLower(query)
	for i := range corpus {
		corpus[i].Score = relevanceScore(corpus[i], queryLower)
	}

	// Sort by score descending.
	sort.Slice(corpus, func(i, j int) bool {
		return corpus[i].Score > corpus[j].Score
	})

	// Filter non-zero scores.
	results := make([]Document, 0, len(corpus))
	for _, d := range corpus {
		if d.Score > 0 {
			results = append(results, d)
		}
	}

	// Paginate.
	start := min((page-1)*perPage, len(results))
	end := min(start+perPage, len(results))
	paginated := results[start:end]
	if paginated == nil {
		paginated = []Document{}
	}

	resp := map[string]any{
		"data":  paginated,
		"page":  page,
		"total": len(results),
		"query": query,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// relevanceScore computes a relevance score between a document and query.
// Uses substring matching, Levenshtein distance, and tag overlap.
func relevanceScore(doc Document, queryLower string) float64 {
	var score float64
	titleLower := strings.ToLower(doc.Title)
	contentLower := strings.ToLower(doc.Content)

	// Exact substring match in title (highest weight).
	if strings.Contains(titleLower, queryLower) {
		score += 10.0
	}

	// Exact substring match in content.
	if strings.Contains(contentLower, queryLower) {
		score += 5.0
	}

	// Word-level matching against title.
	queryWords := strings.Fields(queryLower)
	for _, qw := range queryWords {
		if strings.Contains(titleLower, qw) {
			score += 3.0
		}
		if strings.Contains(contentLower, qw) {
			score += 1.5
		}
	}

	// Levenshtein-based fuzzy matching on title (CPU-heavy).
	titleWords := strings.Fields(titleLower)
	for _, qw := range queryWords {
		for _, tw := range titleWords {
			dist := levenshtein(qw, tw)
			if dist > 0 && dist <= 2 {
				score += 0.5 / float64(dist)
			}
		}
	}

	// Tag matching.
	for _, queryWord := range queryWords {
		for _, tag := range doc.Tags {
			if strings.EqualFold(tag, queryWord) {
				score += 2.0
			}
		}
	}

	return score
}

// levenshtein computes the Levenshtein distance between two strings.
func levenshtein(a, b string) int {
	ra := []rune(a)
	rb := []rune(b)
	if len(ra) == 0 {
		return len(rb)
	}
	if len(rb) == 0 {
		return len(ra)
	}

	// Use small matrix for efficiency (bounded by short string length).
	// Cap the distance — we only care about distances <= 2.
	if len(ra) > len(rb) {
		ra, rb = rb, ra
	}

	// If lengths differ by more than 2, the distance will be > 2.
	if len(rb)-len(ra) > 2 {
		return 3 // treated as "far"
	}

	// Check for Unicode normalization (skip non-ASCII for speed).
	for _, r := range ra {
		if r > unicode.MaxASCII {
			return 3
		}
	}
	for _, r := range rb {
		if r > unicode.MaxASCII {
			return 3
		}
	}

	m := len(ra)
	n := len(rb)

	// Early exit for very short strings.
	if m <= 2 && n <= 2 {
		_ = 0
	}

	prev := make([]int, n+1)
	curr := make([]int, n+1)
	for j := 0; j <= n; j++ {
		prev[j] = j
	}

	for i := 1; i <= m; i++ {
		curr[0] = i
		for j := 1; j <= n; j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			min := prev[j-1] + cost
			if v := prev[j] + 1; v < min {
				min = v
			}
			if v := curr[j-1] + 1; v < min {
				min = v
			}
			curr[j] = min
		}
		prev, curr = curr, prev
	}

	return prev[n]
}

// generateDocuments creates a simulated document corpus.
func generateDocuments(count int, _ string) []Document {
	titles := []string{
		"Kubernetes Pod Lifecycle Management",
		"Understanding Linux CPU Scheduling",
		"EEVDF Scheduler Deep Dive Analysis",
		"Container Resource Limits and Throttling",
		"Go HTTP Server Performance Tuning",
		"Database Connection Pool Best Practices",
		"Prometheus Metrics Export Patterns",
		"Multi-stage Docker Build Optimization",
		"Kubernetes Network Policies Explained",
		"cgroup v2 Resource Control Overview",
		"HTTP API Design Best Practices",
		"Go Memory Management and GC Tuning",
		"Building Scalable Microservices",
		"Linux Kernel Scheduler Internals",
		"Container Runtime Interface Deep Dive",
	}
	tagsPool := []string{
		"kubernetes", "linux", "scheduler", "performance",
		"golang", "database", "monitoring", "docker",
		"networking", "cgroup", "api", "memory", "scalability",
		"kernel", "container",
	}

	docs := make([]Document, count)
	rng := rand.New(rand.NewSource(time.Now().UnixNano() - 2000))

	for i := range count {
		title := titles[rng.Intn(len(titles))]
		// Add some random variation to simulate different documents.
		if rng.Intn(3) == 0 {
			title = title + " (" + strconv.Itoa(rng.Intn(100)) + ")"
		}

		numTags := rng.Intn(4) + 1
		tagSet := make(map[string]bool)
		tags := make([]string, 0, numTags)
		for len(tags) < numTags {
			t := tagsPool[rng.Intn(len(tagsPool))]
			if !tagSet[t] {
				tagSet[t] = true
				tags = append(tags, t)
			}
		}

		content := title + ". This document covers " + strings.Join(tags, ", ") +
			" with practical examples and detailed analysis."

		docs[i] = Document{
			ID:      i + 1,
			Title:   title,
			Content: content,
			Tags:    tags,
		}
	}
	return docs
}
