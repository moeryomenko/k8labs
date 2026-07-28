package handlers

// QueryExecutor is an interface for executing simulated database queries.
// Implementations control the simulated latency and concurrency.
type QueryExecutor interface {
	Exec(kind string)
}
