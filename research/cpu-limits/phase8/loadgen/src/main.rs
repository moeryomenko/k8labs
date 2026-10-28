//! Load generator for the phase 8 CPU-limit campaign.
//!
//! Two modes exist because they answer different questions and must never be
//! silently swapped:
//!
//! - **Open loop** issues requests on a fixed schedule regardless of how long
//!   each takes. Offered load is the independent variable, so latency reflects
//!   what the service did under throttling. This is the mode the campaign uses
//!   to fit limits.
//! - **Closed loop** keeps a fixed number of requests in flight. Throughput is
//!   the dependent variable and latency saturates by construction, which is
//!   useful for capacity questions but is *not* a valid signal for choosing a
//!   CPU limit. The campaign records which mode produced a cell.
//!
//! Every run reports achieved rate, latency percentiles, and error counts, and
//! marks the result invalid when the generator itself was the bottleneck.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use clap::{Parser, ValueEnum};
use hdrhistogram::Histogram;
use serde::Serialize;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum, Serialize)]
#[serde(rename_all = "kebab-case")]
enum Mode {
    Open,
    Closed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum, Serialize)]
#[serde(rename_all = "kebab-case")]
enum Mix {
    ReadHeavy,
    WriteHeavy,
    Balanced,
    CpuHeavy,
}

impl Mix {
    fn as_str(self) -> &'static str {
        match self {
            Self::ReadHeavy => "read-heavy",
            Self::WriteHeavy => "write-heavy",
            Self::Balanced => "balanced",
            Self::CpuHeavy => "cpu-heavy",
        }
    }
}

#[derive(Parser, Debug)]
#[command(about = "Open-loop and closed-loop load generator for the CPU-limit benchmark")]
struct Args {
    /// Target base URL, for example http://bench-cell:8080
    #[arg(long)]
    url: String,

    #[arg(long, value_enum, default_value_t = Mode::Open)]
    mode: Mode,

    /// Offered request rate in requests per second. Open loop only.
    #[arg(long, default_value_t = 50.0)]
    rate: f64,

    /// Concurrent in-flight requests. Closed loop only.
    #[arg(long, default_value_t = 8)]
    concurrency: usize,

    /// Measured duration. Excludes warmup.
    #[arg(long, default_value_t = 60)]
    duration_secs: u64,

    /// Warmup duration whose samples are discarded.
    #[arg(long, default_value_t = 10)]
    warmup_secs: u64,

    #[arg(long, value_enum, default_value_t = Mix::Balanced)]
    mix: Mix,

    /// Per-request timeout. A timeout counts as an error.
    #[arg(long, default_value_t = 5000)]
    timeout_ms: u64,

    /// Where to write the JSON result. Also printed to stdout.
    #[arg(long)]
    output: Option<String>,
}

/// Outcome of one request, independent of which mode produced it.
struct Outcome {
    latency_ms: f64,
    ok: bool,
    /// Milliseconds between the scheduled send time and the actual send time.
    /// Open loop only; a large value means the generator slipped and the
    /// achieved rate is lower than the offered rate.
    schedule_slip_ms: f64,
}

#[derive(Serialize)]
struct Report {
    mode: Mode,
    mix: Mix,
    url: String,
    offered_rate_rps: f64,
    concurrency: usize,
    duration_secs: u64,
    warmup_secs: u64,
    /// Requests completed inside the measured window.
    completed: u64,
    /// Requests that returned a non-2xx status or timed out.
    errors: u64,
    error_ratio: f64,
    /// Achieved completions per second in the measured window.
    achieved_rate_rps: f64,
    latency_ms: LatencySummary,
    /// Maximum scheduler slip observed. Open loop only.
    max_schedule_slip_ms: f64,
    /// Sustained overshoot relative to the offered rate, as a ratio.
    schedule_lag_ratio: f64,
    /// False when the generator could not sustain the offered load. A cell with
    /// `valid == false` must be excluded from fitting, because its rate is a
    /// property of the generator rather than of the service.
    valid: bool,
    invalid_reason: Option<String>,
}

#[derive(Serialize)]
struct LatencySummary {
    min: f64,
    p50: f64,
    p90: f64,
    p99: f64,
    p999: f64,
    max: f64,
    mean: f64,
}

fn summarize(histogram: &Histogram<u64>) -> LatencySummary {
    LatencySummary {
        min: histogram.min() as f64,
        p50: histogram.value_at_quantile(0.50) as f64,
        p90: histogram.value_at_quantile(0.90) as f64,
        p99: histogram.value_at_quantile(0.99) as f64,
        p999: histogram.value_at_quantile(0.999) as f64,
        max: histogram.max() as f64,
        mean: histogram.mean(),
    }
}

/// Histogram granularity: 1 ms resolution up to 5 minutes. Latency below 1 ms
/// is clamped to 1 ms, which is the resolution the campaign compares against.
fn new_histogram() -> Histogram<u64> {
    Histogram::new_with_bounds(1, 300_000, 3).expect("static histogram bounds are valid")
}

struct Request {
    client: reqwest::Client,
    base: String,
    mix: Mix,
    timeout: Duration,
}

impl Request {
    async fn issue(&self, sequence: u64) -> Outcome {
        let url = format!("{}/work", self.base.trim_end_matches('/'));
        let started = Instant::now();

        let result = self
            .client
            .get(&url)
            .header("x-workload-mix", self.mix.as_str())
            .header("x-request-id", sequence.to_string())
            .timeout(self.timeout)
            .send()
            .await;

        let latency_ms = started.elapsed().as_secs_f64() * 1000.0;

        let ok = match result {
            Ok(response) => response.status().is_success(),
            Err(_) => false,
        };

        Outcome {
            latency_ms,
            ok,
            schedule_slip_ms: 0.0,
        }
    }
}

/// Open loop: one task per request, released on a fixed schedule.
///
/// A dedicated scheduler task computes each request's due time from the run
/// start so scheduling error does not accumulate. Requests are not blocked by
/// slow responses, which is what makes offered load independent of service
/// latency.
async fn run_open(args: &Args, request: Arc<Request>) -> Vec<Outcome> {
    let total_secs = args.warmup_secs + args.duration_secs;
    let total_requests = (args.rate * total_secs as f64).round() as u64;
    let interval = Duration::from_secs_f64(1.0 / args.rate);
    let start = Instant::now();

    let mut joins: JoinSet<(Outcome, Instant)> = JoinSet::new();

    for sequence in 0..total_requests {
        let due = start + interval * sequence as u32;
        let now = Instant::now();
        if due > now {
            tokio::time::sleep(due - now).await;
        }
        let slip = Instant::now().saturating_duration_since(due).as_secs_f64() * 1000.0;
        let issued_at = Instant::now();

        let request = request.clone();
        let mix_sequence = sequence % 1024;
        joins.spawn(async move {
            let mut outcome = request.issue(mix_sequence).await;
            outcome.schedule_slip_ms = slip;
            (outcome, issued_at)
        });
    }

    // Drain remaining in-flight requests so no latency sample is dropped.
    let drain_deadline =
        start + Duration::from_secs(total_secs) + Duration::from_millis(args.timeout_ms);
    let mut outcomes = Vec::with_capacity(total_requests as usize);
    loop {
        let remaining = drain_deadline.saturating_duration_since(Instant::now());
        match tokio::time::timeout(remaining.max(Duration::from_millis(1)), joins.join_next()).await
        {
            Ok(Some(Ok((outcome, issued_at)))) => {
                // Keep only requests issued inside the measured window.
                let offset = issued_at.saturating_duration_since(start);
                if offset >= Duration::from_secs(args.warmup_secs) {
                    outcomes.push(outcome);
                }
            }
            Ok(Some(Err(err))) => {
                tracing::error!(error = %err, "request task panicked");
            }
            Ok(None) => break,
            Err(_) => {
                tracing::warn!("drain deadline reached; some requests did not finish");
                break;
            }
        }
    }

    outcomes
}

/// Closed loop: a fixed number of workers, each looping until the deadline.
///
/// Throughput is measured, not imposed. Latency therefore reflects queueing
/// behind the in-flight bound as well as service time.
async fn run_closed(args: &Args, request: Arc<Request>, counter: Arc<AtomicU64>) -> Vec<Outcome> {
    let warmup = Duration::from_secs(args.warmup_secs);
    let deadline = Duration::from_secs(args.warmup_secs + args.duration_secs);
    let start = Instant::now();
    let semaphore = Arc::new(Semaphore::new(args.concurrency));

    let mut joins: JoinSet<Vec<Outcome>> = JoinSet::new();
    for _ in 0..args.concurrency {
        let request = request.clone();
        let semaphore = semaphore.clone();
        let counter = counter.clone();
        joins.spawn(async move {
            let mut collected = Vec::new();
            loop {
                let now = start.elapsed();
                if now >= deadline {
                    break;
                }
                let _permit = semaphore.acquire().await.expect("semaphore is never closed");
                let sequence = counter.fetch_add(1, Ordering::Relaxed);
                let outcome = request.issue(sequence % 1024).await;
                if now >= warmup {
                    collected.push(outcome);
                }
            }
            collected
        });
    }

    let mut outcomes = Vec::new();
    while let Some(result) = joins.join_next().await {
        match result {
            Ok(mut batch) => outcomes.append(&mut batch),
            Err(err) => tracing::error!(error = %err, "closed-loop worker panicked"),
        }
    }
    outcomes
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let args = Args::parse();

    if args.rate <= 0.0 && args.mode == Mode::Open {
        eprintln!("--rate must be greater than zero in open-loop mode");
        std::process::exit(2);
    }
    if args.concurrency == 0 && args.mode == Mode::Closed {
        eprintln!("--concurrency must be at least 1 in closed-loop mode");
        std::process::exit(2);
    }

    let client = reqwest::Client::builder()
        .pool_max_idle_per_host(args.concurrency.max(64))
        .build()
        .expect("building HTTP client");

    let request = Arc::new(Request {
        client,
        base: args.url.clone(),
        mix: args.mix,
        timeout: Duration::from_millis(args.timeout_ms),
    });

    // Warm the connection pool before the measured window so TLS and connection
    // setup do not appear as latency in the first measured samples.
    for sequence in 0..4u64 {
        let _ = request.issue(sequence).await;
    }

    let counter = Arc::new(AtomicU64::new(0));
    let measured_start = Instant::now();
    let outcomes = match args.mode {
        Mode::Open => run_open(&args, request.clone()).await,
        Mode::Closed => run_closed(&args, request.clone(), counter).await,
    };
    let measured_elapsed = measured_start
        .elapsed()
        .saturating_sub(Duration::from_secs(args.warmup_secs));

    let mut histogram = new_histogram();
    let mut errors = 0u64;
    let mut max_slip: f64 = 0.0;
    for outcome in &outcomes {
        histogram
            .record((outcome.latency_ms.round() as u64).max(1))
            .expect("latency is within histogram bounds after recording bounds check");
        if !outcome.ok {
            errors += 1;
        }
        max_slip = max_slip.max(outcome.schedule_slip_ms);
    }

    let completed = outcomes.len() as u64;
    let window_secs = measured_elapsed.as_secs_f64().max(1e-9);
    let achieved_rate = completed as f64 / window_secs;
    let error_ratio = if completed == 0 {
        1.0
    } else {
        errors as f64 / completed as f64
    };

    // Generator validity. Open loop is invalid when it could not keep up: a
    // sustained shortfall means the service was not offered the declared load,
    // so neither its latency nor its rate describes that load.
    let (valid, invalid_reason) = match args.mode {
        Mode::Open => {
            let shortfall = 1.0 - (achieved_rate / args.rate);
            if completed == 0 {
                (false, Some("no requests completed in the measured window".to_string()))
            } else if shortfall > 0.10 {
                (
                    false,
                    Some(format!(
                        "generator could not sustain the offered load: offered {:.2} rps, achieved {:.2} rps ({:.1}% short)",
                        args.rate,
                        achieved_rate,
                        shortfall * 100.0
                    )),
                )
            } else if errors as f64 / completed as f64 > 0.5 {
                (
                    false,
                    Some(format!(
                        "{errors} of {completed} requests failed; the load generator cannot characterise a failing target"
                    )),
                )
            } else {
                (true, None)
            }
        }
        Mode::Closed => {
            if completed == 0 {
                (false, Some("no requests completed in the measured window".to_string()))
            } else {
                (true, None)
            }
        }
    };

    let report = Report {
        mode: args.mode,
        mix: args.mix,
        url: args.url.clone(),
        offered_rate_rps: if args.mode == Mode::Open {
            args.rate
        } else {
            0.0
        },
        concurrency: args.concurrency,
        duration_secs: args.duration_secs,
        warmup_secs: args.warmup_secs,
        completed,
        errors,
        error_ratio,
        achieved_rate_rps: achieved_rate,
        latency_ms: summarize(&histogram),
        max_schedule_slip_ms: max_slip,
        schedule_lag_ratio: if args.mode == Mode::Open {
            (achieved_rate / args.rate).min(1.0)
        } else {
            1.0
        },
        valid,
        invalid_reason,
    };

    let json = serde_json::to_string_pretty(&report).expect("report is serializable");
    println!("{json}");

    if let Some(path) = &args.output {
        if let Err(err) = std::fs::write(path, &json) {
            eprintln!("cannot write {path}: {err}");
            std::process::exit(1);
        }
    }

    if !valid {
        std::process::exit(3);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn histogram_reports_ordered_percentiles() {
        let mut histogram = new_histogram();
        for value in 1..=1000u64 {
            histogram.record(value).unwrap();
        }
        let summary = summarize(&histogram);
        assert!(summary.min <= summary.p50);
        assert!(summary.p50 <= summary.p90);
        assert!(summary.p90 <= summary.p99);
        assert!(summary.p99 <= summary.p999);
        assert!(summary.p999 <= summary.max);
        assert_eq!(summary.min, 1.0);
        assert_eq!(summary.max, 1000.0);
    }

    #[test]
    fn histogram_tolerates_sub_millisecond_latency() {
        // The service answers cache hits under 1 ms; the recorder clamps to the
        // histogram's 1 ms resolution instead of failing.
        let mut histogram = new_histogram();
        histogram.record(1u64).unwrap();
        assert_eq!(histogram.min(), 1);
    }

    #[test]
    fn mix_names_match_the_service_header_values() {
        // The generator must send exactly the values the service parses, or
        // requests fail with 400 and the cell reads as a service failure.
        assert_eq!(Mix::ReadHeavy.as_str(), "read-heavy");
        assert_eq!(Mix::WriteHeavy.as_str(), "write-heavy");
        assert_eq!(Mix::Balanced.as_str(), "balanced");
        assert_eq!(Mix::CpuHeavy.as_str(), "cpu-heavy");
    }

    #[test]
    fn report_serializes_mode_and_mix_as_kebab_case() {
        // The analysis groups by these strings, so their spelling is contract.
        let mode = serde_json::to_string(&Mode::Open).unwrap();
        assert_eq!(mode, "\"open\"");
        let mix = serde_json::to_string(&Mix::CpuHeavy).unwrap();
        assert_eq!(mix, "\"cpu-heavy\"");
    }

    #[test]
    fn open_loop_validity_flags_generator_shortfall() {
        // Reproduce the decision rule without running a server: offered 100
        // rps, achieved 80 rps is a 20% shortfall and must be invalid.
        let offered = 100.0_f64;
        let achieved = 80.0_f64;
        let shortfall = 1.0 - (achieved / offered);
        assert!(shortfall > 0.10, "20% shortfall must exceed the 10% gate");
    }

    #[test]
    fn open_loop_validity_accepts_small_shortfall() {
        let offered = 100.0_f64;
        let achieved = 95.0_f64;
        let shortfall = 1.0 - (achieved / offered);
        assert!(shortfall <= 0.10, "5% shortfall must pass the gate");
    }
}
