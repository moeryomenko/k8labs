/// HTTP surface of the reference service.
///
/// `GET /work` is the measured handler. `GET /healthz` and `GET /readyz` keep
/// Kubernetes probes away from dependency latency so a throttled service is not
/// mistaken for a broken one. `GET /meta` publishes the workload identity the
/// model must match.
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use opentelemetry::trace::{TraceContextExt as _, Tracer as _};
use serde::Serialize;

use crate::config::{Config, Mix};
use crate::deps::Dependencies;
use crate::telemetry;

pub struct AppState {
    pub deps: Dependencies,
    pub config: Config,
    pub counters: Counters,
}

/// Sliding accounting for `request.rate`.
///
/// The contract asks for completed requests per second. A cheap monotonic
/// counter plus wall clock gives that without a background sampler, and the
/// rate is recorded on each completed request.
#[derive(Default)]
pub struct Counters {
    served: AtomicU64,
    errors: AtomicU64,
}

impl Counters {
    pub fn bump_served(&self) -> u64 {
        self.served.fetch_add(1, Ordering::Relaxed) + 1
    }

    pub fn bump_errors(&self) {
        self.errors.fetch_add(1, Ordering::Relaxed);
    }

    pub fn served(&self) -> u64 {
        self.served.load(Ordering::Relaxed)
    }

    pub fn errors(&self) -> u64 {
        self.errors.load(Ordering::Relaxed)
    }
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/work", get(work))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/meta", get(meta))
        .route("/metrics", get(metrics))
        .with_state(state)
}

/// CPU work scaled by mix. Written so the compiler cannot elide it: the
/// accumulator is read through `black_box`.
fn burn_cpu(iterations: u64) -> u64 {
    let mut acc: u64 = 0x9e37_79b9_7f4a_7c15;
    for index in 0..iterations {
        acc = acc
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(index ^ acc.rotate_left(17));
    }
    std::hint::black_box(acc)
}

fn resolve_mix(headers: &HeaderMap, default: Mix) -> Result<Mix, String> {
    match headers.get("x-workload-mix") {
        None => Ok(default),
        Some(value) => {
            let raw = value
                .to_str()
                .map_err(|_| "X-Workload-Mix is not valid ASCII".to_string())?;
            Mix::parse(raw).ok_or_else(|| format!("X-Workload-Mix is not a known mix: {raw:?}"))
        }
    }
}

/// One measured request: a cache read, a database read or write, a cache
/// write, and mix-scaled CPU work.
async fn execute_work(state: &AppState, mix: Mix, id: i64) -> Result<(), String> {
    let iterations = (state.config.cpu_iterations as f64 * mix.cpu_scale()) as u64;

    let (reads, writes) = mix.weights();
    let is_write = (id.unsigned_abs() % u64::from(reads + writes)) >= u64::from(reads);
    let key = format!("item:{id}");

    if is_write {
        let payload = format!("payload-{id}-{}", state.counters.served());
        state.deps.upsert_item(id, &payload).await?;
        state.deps.cache_set(&key, &payload).await?;
    } else {
        let cached = state.deps.cache_get(&key).await?;
        if cached.is_none() {
            let payload = state.deps.select_item(id).await?;
            state.deps.cache_set(&key, &payload).await?;
        }
    }

    let tracer = telemetry::tracer();
    let span = tracer
        .span_builder("cpu.work")
        .with_attributes([opentelemetry::KeyValue::new(
            "cpu.iterations",
            iterations as i64,
        )])
        .start(&tracer);
    {
        let _guard = opentelemetry::Context::current_with_span(span).attach();
        burn_cpu(iterations);
    }

    Ok(())
}

async fn work(State(state): State<Arc<AppState>>, headers: HeaderMap) -> Response {
    let started = Instant::now();
    let mix = match resolve_mix(&headers, state.config.mix) {
        Ok(mix) => mix,
        Err(message) => {
            return (StatusCode::BAD_REQUEST, message).into_response();
        }
    };

    // Deterministic key spread: the load generator sets X-Request-Id so runs are
    // reproducible, otherwise the served counter is enough to avoid one hot row.
    let id = headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or_else(|| state.counters.served() as u64) as i64
        % 1024;

    let outcome = execute_work(&state, mix, id).await;
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;

    let status = match &outcome {
        Ok(()) => StatusCode::OK,
        Err(err) => {
            tracing::error!(error = %err, "work failed");
            StatusCode::INTERNAL_SERVER_ERROR
        }
    };

    let served = state.counters.bump_served();
    if status.is_server_error() {
        state.counters.bump_errors();
    }

    // Rate is computed against process uptime so a slow single request still
    // reports a sane instantaneous value instead of dividing by its own span.
    let uptime_s = process_uptime().as_secs_f64().max(1e-3);
    let rate = served as f64 / uptime_s;

    telemetry::record_request(
        &telemetry::RequestAttributes {
            mix: mix.as_str(),
            method: "GET",
            status: status.as_u16(),
        },
        elapsed_ms,
        rate,
    );

    match status {
        StatusCode::OK => (StatusCode::OK, "ok").into_response(),
        _ => (status, "work failed").into_response(),
    }
}

fn process_uptime() -> Duration {
    use std::sync::OnceLock;
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed()
}

async fn healthz() -> StatusCode {
    StatusCode::OK
}

async fn readyz(State(state): State<Arc<AppState>>) -> Response {
    let postgres = state.deps.postgres_ping().await;
    let redis = state.deps.redis_ping().await;
    match (postgres, redis) {
        (Ok(()), Ok(())) => StatusCode::OK.into_response(),
        (postgres, redis) => (
            StatusCode::SERVICE_UNAVAILABLE,
            format!("postgres={postgres:?} redis={redis:?}"),
        )
            .into_response(),
    }
}

#[derive(Serialize)]
struct Meta {
    profile: String,
    contract_version: &'static str,
    mix: &'static str,
    cpu_iterations: u64,
    served: u64,
    errors: u64,
}

async fn meta(State(state): State<Arc<AppState>>) -> Json<Meta> {
    Json(Meta {
        profile: state.config.profile.clone(),
        contract_version: "db-cache-mixed/v1",
        mix: state.config.mix.as_str(),
        cpu_iterations: state.config.cpu_iterations,
        served: state.counters.served(),
        errors: state.counters.errors(),
    })
}

/// Minimal text exposition for the load generator and for operators. The
/// autosizer reads OTLP, not this endpoint; it exists for humans and tests.
async fn metrics(State(state): State<Arc<AppState>>) -> String {
    format!(
        "# HELP bench_requests_served_total Requests answered by the measured handler\n\
         # TYPE bench_requests_served_total counter\n\
         bench_requests_served_total {}\n\
         # HELP bench_request_errors_total Requests answered with status >= 500\n\
         # TYPE bench_request_errors_total counter\n\
         bench_request_errors_total {}\n",
        state.counters.served(),
        state.counters.errors()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_mix_header_is_rejected() {
        let mut headers = HeaderMap::new();
        headers.insert("x-workload-mix", "impossible".parse().unwrap());
        assert!(resolve_mix(&headers, Mix::Balanced).is_err());
    }

    #[test]
    fn missing_mix_header_uses_default() {
        let headers = HeaderMap::new();
        assert_eq!(resolve_mix(&headers, Mix::CpuHeavy).unwrap(), Mix::CpuHeavy);
    }

    #[test]
    fn burn_cpu_is_not_elided() {
        // Two different iteration counts must produce different work. If the
        // optimizer removed the loop the results would be equal.
        assert_ne!(burn_cpu(1_000), burn_cpu(2_000));
    }
}
