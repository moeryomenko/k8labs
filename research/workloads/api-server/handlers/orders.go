package handlers

import (
	"encoding/json"
	"math/rand"
	"net/http"
	"sort"
	"strconv"
	"time"
)

// Order represents a simulated order record.
type Order struct {
	ID        int        `json:"id"`
	UserID    int        `json:"user_id"`
	Status    string     `json:"status"`
	Total     float64    `json:"total"`
	Currency  string     `json:"currency"`
	Items     int        `json:"items"`
	CreatedAt time.Time  `json:"created_at"`
	ShippedAt *time.Time `json:"shipped_at,omitempty"`
}

// OrderHandler handles GET /api/v1/orders (medium CPU).
// Filters by status, sorts, and paginates in-memory order records.
type OrderHandler struct {
	db         QueryExecutor
	complexity float64
}

// NewOrderHandler creates an OrderHandler.
func NewOrderHandler(db QueryExecutor, complexity float64) *OrderHandler {
	return &OrderHandler{db: db, complexity: complexity}
}

func (h *OrderHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		h.createOrder(w, r)
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	h.listOrders(w, r)
}

func (h *OrderHandler) createOrder(w http.ResponseWriter, r *http.Request) {
	// Simulate DB insert.
	h.db.Exec("INSERT orders")

	var o Order
	if err := json.NewDecoder(r.Body).Decode(&o); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	o.ID = int(time.Now().UnixNano())
	o.CreatedAt = time.Now()
	o.Status = "pending"

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(o)
}

func (h *OrderHandler) listOrders(w http.ResponseWriter, r *http.Request) {
	h.db.Exec("SELECT orders")

	statusFilter := r.URL.Query().Get("status")
	pageStr := r.URL.Query().Get("page")
	perPageStr := r.URL.Query().Get("per_page")

	page := 1
	perPage := 20
	if v, err := strconv.Atoi(pageStr); err == nil && v > 0 {
		page = v
	}
	if v, err := strconv.Atoi(perPageStr); err == nil && v > 0 && v <= 100 {
		perPage = v
	}

	// Scale dataset size with complexity factor.
	totalOrders := int(200 * h.complexity)
	if totalOrders < 50 {
		totalOrders = 50
	}
	if totalOrders > 1000 {
		totalOrders = 1000
	}

	orders := generateOrders(totalOrders)

	// Filter by status.
	if statusFilter != "" {
		filtered := make([]Order, 0, len(orders))
		for _, o := range orders {
			if o.Status == statusFilter {
				filtered = append(filtered, o)
			}
		}
		orders = filtered
	}

	// Sort by created_at descending (CPU work: O(n log n)).
	sort.Slice(orders, func(i, j int) bool {
		return orders[i].CreatedAt.After(orders[j].CreatedAt)
	})

	// Paginate.
	start := (page - 1) * perPage
	if start >= len(orders) {
		start = len(orders)
	}
	end := start + perPage
	if end > len(orders) {
		end = len(orders)
	}
	paginated := orders[start:end]
	if paginated == nil {
		paginated = []Order{}
	}

	// Build response with metadata.
	resp := map[string]interface{}{
		"data":     paginated,
		"page":     page,
		"per_page": perPage,
		"total":    len(orders),
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// generateOrders creates simulated order records.
func generateOrders(count int) []Order {
	statuses := []string{"pending", "confirmed", "processing", "shipped", "delivered", "cancelled"}
	currencies := []string{"USD", "EUR", "GBP", "JPY"}

	orders := make([]Order, count)
	rng := rand.New(rand.NewSource(time.Now().UnixNano() - 1000))
	baseTime := time.Date(2024, time.June, 1, 0, 0, 0, 0, time.UTC)

	for i := 0; i < count; i++ {
		status := statuses[rng.Intn(len(statuses))]
		created := baseTime.Add(time.Duration(i) * time.Hour)
		var shipped *time.Time
		if status == "shipped" || status == "delivered" {
			t := created.Add(2 * time.Hour)
			shipped = &t
		}
		orders[i] = Order{
			ID:        i + 1,
			UserID:    rng.Intn(50) + 1,
			Status:    status,
			Total:     float64(rng.Intn(10000)) / 100,
			Currency:  currencies[rng.Intn(len(currencies))],
			Items:     rng.Intn(10) + 1,
			CreatedAt: created,
			ShippedAt: shipped,
		}
	}
	return orders
}
