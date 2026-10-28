/// OpenTelemetry wiring for the frozen telemetry contract
/// (`research/cpu-limits/phase8/contracts/telemetry-contract.json`).
///
/// Signal names here must match the contract exactly. If a name changes here it
/// must change in the contract and in `fitter`, or the autosizer will refuse to
/// act on stale or missing data.
use std::sync::OnceLock;
use std::time::Duration;

use opentelemetry::metrics::{Counter, Histogram, Meter, MeterProvider as _};
use opentelemetry::KeyValue;
use opentelemetry::global;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_otlp::{MetricExporter, SpanExporter, WithExportConfig};
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::trace::{Sampler, SdkTracerProvider};
use opentelemetry_sdk::Resource;

use crate::config::Config;

/// Request duration buckets come from the contract. They must stay sorted and
/// must cover both sub-millisecond cache hits and throttled seconds.
pub const REQUEST_DURATION_BUCKETS_MS: &[f64] = &[
    0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0,
];

pub const DEPENDENCY_DURATION_BUCKETS_MS: &[f64] = &[
    0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 500.0,
];

pub const POOL_WAIT_BUCKETS_MS: &[f64] = &[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 25.0, 100.0, 500.0];

static METER: OnceLock<Meter> = OnceLock::new();

struct Instruments {
    request_rate: opentelemetry::metrics::Gauge<f64>,
    request_duration: Histogram<f64>,
    request_errors: Counter<u64>,
    postgres_duration: Histogram<f64>,
    postgres_pool_wait: Histogram<f64>,
    redis_duration: Histogram<f64>,
    redis_pool_wait: Histogram<f64>,
}

static INSTRUMENTS: OnceLock<Instruments> = OnceLock::new();

fn instruments() -> &'static Instruments {
    INSTRUMENTS.get_or_init(|| {
        let meter = METER
            .get_or_init(|| global::meter("cpu-limit-bench"))
            .clone();
        Instruments {
            request_rate: meter
                .f64_gauge("http.server.request.rate")
                .with_description("Completed requests per second")
                .with_unit("1/s")
                .build(),
            request_duration: meter
                .f64_histogram("http.server.request.duration")
                .with_description("End-to-end request duration")
                .with_unit("ms")
                .with_boundaries(REQUEST_DURATION_BUCKETS_MS.to_vec())
                .build(),
            request_errors: meter
                .u64_counter("http.server.request.errors")
                .with_description("Requests that failed with status >= 500")
                .build(),
            postgres_duration: meter
                .f64_histogram("db.client.operation.duration")
                .with_description("PostgreSQL query duration, excluding pool wait")
                .with_unit("ms")
                .with_boundaries(DEPENDENCY_DURATION_BUCKETS_MS.to_vec())
                .build(),
            postgres_pool_wait: meter
                .f64_histogram("db.client.pool.wait.duration")
                .with_description("Time waiting for a pooled PostgreSQL connection")
                .with_unit("ms")
                .with_boundaries(POOL_WAIT_BUCKETS_MS.to_vec())
                .build(),
            redis_duration: meter
                .f64_histogram("cache.client.operation.duration")
                .with_description("Redis command duration, excluding pool wait")
                .with_unit("ms")
                .with_boundaries(DEPENDENCY_DURATION_BUCKETS_MS.to_vec())
                .build(),
            redis_pool_wait: meter
                .f64_histogram("cache.client.pool.wait.duration")
                .with_description("Time waiting for a pooled Redis connection")
                .with_unit("ms")
                .with_boundaries(POOL_WAIT_BUCKETS_MS.to_vec())
                .build(),
        }
    })
}

/// Handles that must outlive the process so exporters flush on shutdown.
pub struct Telemetry {
    tracer_provider: Option<SdkTracerProvider>,
    meter_provider: Option<SdkMeterProvider>,
}

impl Telemetry {
    pub fn shutdown(&self) {
        if let Some(provider) = &self.tracer_provider {
            let _ = provider.force_flush();
            let _ = provider.shutdown();
        }
        if let Some(provider) = &self.meter_provider {
            let _ = provider.shutdown();
        }
    }
}

/// Install global tracer and meter providers.
///
/// With no OTLP endpoint the service stays fully functional with a no-op
/// pipeline; the campaign always sets an endpoint so a silent telemetry gap
/// cannot masquerade as a healthy measurement.
pub fn init(config: &Config) -> Result<Telemetry, String> {
    let attributes = vec![
        KeyValue::new("service.name", "cpu-limit-bench"),
        KeyValue::new("service.version", env!("CARGO_PKG_VERSION")),
        KeyValue::new("workload.profile", config.profile.clone()),
        KeyValue::new("workload.mix", config.mix.as_str()),
    ];

    let Some(endpoint) = &config.otlp_endpoint else {
        tracing::warn!("OTEL_EXPORTER_OTLP_ENDPOINT is unset; telemetry export is disabled");
        return Ok(Telemetry {
            tracer_provider: None,
            meter_provider: None,
        });
    };

    let resource = Resource::builder_empty()
        .with_attributes(attributes)
        .build();

    let span_exporter = SpanExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint.clone())
        .build()
        .map_err(|err| format!("building span exporter: {err}"))?;

    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(resource.clone())
        .with_sampler(Sampler::ParentBased(Box::new(Sampler::TraceIdRatioBased(
            config.trace_sample_ratio,
        ))))
        .with_batch_exporter(span_exporter)
        .build();

    let metric_exporter = MetricExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint.clone())
        .build()
        .map_err(|err| format!("building metric exporter: {err}"))?;

    let reader = PeriodicReader::builder(metric_exporter)
        .with_interval(Duration::from_secs(10))
        .build();

    let meter_provider = SdkMeterProvider::builder()
        .with_resource(resource)
        .with_reader(reader)
        .build();

    global::set_meter_provider(meter_provider.clone());
    global::set_tracer_provider(tracer_provider.clone());

    METER.get_or_init(|| meter_provider.meter("cpu-limit-bench"));

    Ok(Telemetry {
        tracer_provider: Some(tracer_provider),
        meter_provider: Some(meter_provider),
    })
}

/// Tracer for request-path spans.
///
/// The global provider is installed by `init` before the listener starts, and
/// it falls back to a no-op provider otherwise. Returning the boxed tracer
/// keeps callers free of the SDK tracer's generic `Span` type.
pub fn tracer() -> opentelemetry::global::BoxedTracer {
    global::tracer_provider().tracer("cpu-limit-bench")
}

#[derive(Debug, Clone)]
pub struct RequestAttributes {
    pub mix: &'static str,
    pub method: &'static str,
    pub status: u16,
}

impl RequestAttributes {
    fn key_values(&self) -> [KeyValue; 3] {
        [
            KeyValue::new("workload.mix", self.mix),
            KeyValue::new("http.request.method", self.method),
            KeyValue::new("http.response.status_code", i64::from(self.status)),
        ]
    }
}

pub fn record_request(attributes: &RequestAttributes, duration_ms: f64, rate_rps: f64) {
    let instruments = instruments();
    let key_values = attributes.key_values();
    instruments
        .request_duration
        .record(duration_ms, &key_values);
    instruments.request_rate.record(rate_rps, &key_values);
    if attributes.status >= 500 {
        instruments.request_errors.add(1, &key_values);
    }
}

pub fn record_postgres(duration_ms: f64, pool_wait_ms: f64, operation: &'static str) {
    let instruments = instruments();
    let key_values = [
        KeyValue::new("db.system", "postgresql"),
        KeyValue::new("db.operation.name", operation),
    ];
    instruments
        .postgres_duration
        .record(duration_ms, &key_values);
    instruments
        .postgres_pool_wait
        .record(pool_wait_ms, &[KeyValue::new("db.system", "postgresql")]);
}

pub fn record_redis(duration_ms: f64, pool_wait_ms: f64, operation: &'static str) {
    let instruments = instruments();
    let key_values = [
        KeyValue::new("cache.system", "redis"),
        KeyValue::new("db.operation.name", operation),
    ];
    instruments.redis_duration.record(duration_ms, &key_values);
    instruments
        .redis_pool_wait
        .record(pool_wait_ms, &[KeyValue::new("cache.system", "redis")]);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_buckets_cover_sub_ms_to_seconds() {
        assert!(REQUEST_DURATION_BUCKETS_MS[0] < 1.0);
        assert!(*REQUEST_DURATION_BUCKETS_MS.last().unwrap() >= 5000.0);
    }

    #[test]
    fn buckets_are_strictly_ascending() {
        for window in REQUEST_DURATION_BUCKETS_MS.windows(2) {
            assert!(window[0] < window[1], "buckets must ascend: {window:?}");
        }
    }
}
