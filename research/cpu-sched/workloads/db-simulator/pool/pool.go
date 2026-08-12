// Package pool provides a simulated database connection pool backed by a
// worker goroutine pool.  Queries are submitted as jobs and executed on
// one of the available workers, modelling per-query CPU overhead for
// network processing and context switching.
package pool

import (
	"context"
	"sync"
	"sync/atomic"
)

// A Job is a function to execute inside the pool.
type Job func()

// Pool is a fixed-size worker goroutine pool.
type Pool struct {
	workers int
	jobs    chan Job
	wg      sync.WaitGroup

	active atomic.Int64
	queued atomic.Int64
}

// New creates a pool with n workers and the given queue depth.
func New(n int, queueDepth int) *Pool {
	p := &Pool{
		workers: n,
		jobs:    make(chan Job, queueDepth),
	}
	p.wg.Add(n)
	for range n {
		go p.worker()
	}
	return p
}

// worker dequeue and run jobs.
func (p *Pool) worker() {
	defer p.wg.Done()
	for fn := range p.jobs {
		p.queued.Add(-1)
		p.active.Add(1)
		fn()
		p.active.Add(-1)
	}
}

// Submit enqueues a job for execution.  It blocks if the queue is full until
// ctx is cancelled.
func (p *Pool) Submit(ctx context.Context, fn Job) error {
	p.queued.Add(1)
	select {
	case <-ctx.Done():
		p.queued.Add(-1)
		return ctx.Err()
	case p.jobs <- fn:
		return nil
	}
}

// Active returns the current number of active workers.
func (p *Pool) Active() int64 { return p.active.Load() }

// Queued returns the current queue depth.
func (p *Pool) Queued() int64 { return p.queued.Load() }

// Stop waits for all workers to finish and closes the pool.
func (p *Pool) Stop() {
	close(p.jobs)
	p.wg.Wait()
}
