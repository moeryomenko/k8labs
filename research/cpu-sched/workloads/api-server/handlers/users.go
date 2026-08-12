// Package handlers implements HTTP API endpoints with varying CPU intensity.
package handlers

import (
	"encoding/json"
	"math/rand"
	"net/http"
	"strconv"
	"time"
)

// User represents a simulated user record.
type User struct {
	ID        int       `json:"id"`
	Name      string    `json:"name"`
	Email     string    `json:"email"`
	Role      string    `json:"role"`
	CreatedAt time.Time `json:"created_at"`
	Active    bool      `json:"active"`
}

// UserHandler handles the GET /api/v1/users endpoint (light CPU).
// It generates simulated user records and JSON-serializes them.
// CPU usage scales with the limit parameter.
type UserHandler struct {
	db         QueryExecutor
	complexity float64
}

// NewUserHandler creates a UserHandler with the given DB executor.
func NewUserHandler(db QueryExecutor, complexity float64) *UserHandler {
	return &UserHandler{db: db, complexity: complexity}
}

func (h *UserHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	limitStr := r.URL.Query().Get("limit")
	limit := 10 // default
	if limitStr != "" {
		if v, err := strconv.Atoi(limitStr); err == nil && v > 0 && v <= 100 {
			limit = v
		}
	}

	// Simulate DB query for user data.
	h.db.Exec("SELECT users")

	// Scale work with limit and complexity.
	work := int(float64(limit) * h.complexity)
	users := generateUsers(work)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(users)
}

// generateUsers creates simulated user records with CPU work proportional to count.
func generateUsers(count int) []User {
	roles := []string{"admin", "editor", "viewer", "manager", "auditor"}
	firstNames := []string{"Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank", "Iris", "Jack"}
	lastNames := []string{"Smith", "Jones", "Brown", "Taylor", "Wilson", "Lee", "Miller", "Davis", "Garcia", "Martinez"}

	users := make([]User, 0, count)
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	for i := 0; i < count; i++ {
		first := firstNames[rng.Intn(len(firstNames))]
		last := lastNames[rng.Intn(len(lastNames))]
		role := roles[rng.Intn(len(roles))]
		users = append(users, User{
			ID:        i + 1,
			Name:      first + " " + last,
			Email:     first + "." + last + "@example.com",
			Role:      role,
			CreatedAt: time.Date(2024, time.January, 1, 0, 0, 0, 0, time.UTC).Add(time.Duration(i) * 24 * time.Hour),
			Active:    i%3 != 0,
		})
	}
	return users
}
