/// Configuration for the reference service.
///
/// Every knob that changes measured behaviour is an environment variable so a
/// campaign manifest can set it without rebuilding the image. That keeps the
/// service a fixed measuring instrument and the experiment a data file.
use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub listen_addr: String,
    pub postgres_url: String,
    pub redis_url: String,
    pub postgres_pool_size: usize,
    pub redis_pool_size: usize,
    pub mix: Mix,
    pub cpu_iterations: u64,
    pub otlp_endpoint: Option<String>,
    pub trace_sample_ratio: f64,
    pub profile: String,
}

/// Request mix supplied by the `X-Workload-Mix` header or the default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mix {
    ReadHeavy,
    WriteHeavy,
    Balanced,
    CpuHeavy,
}

impl Mix {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "read-heavy" => Some(Self::ReadHeavy),
            "write-heavy" => Some(Self::WriteHeavy),
            "balanced" => Some(Self::Balanced),
            "cpu-heavy" => Some(Self::CpuHeavy),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadHeavy => "read-heavy",
            Self::WriteHeavy => "write-heavy",
            Self::Balanced => "balanced",
            Self::CpuHeavy => "cpu-heavy",
        }
    }

    /// SQL/Redis/cache reads against a cache write, as `(read, write)` weights.
    pub fn weights(self) -> (u32, u32) {
        match self {
            Self::ReadHeavy => (9, 1),
            Self::WriteHeavy => (2, 8),
            Self::Balanced => (5, 5),
            Self::CpuHeavy => (3, 7),
        }
    }

    /// CPU work relative to the base iterations.
    pub fn cpu_scale(self) -> f64 {
        match self {
            Self::ReadHeavy => 0.25,
            Self::WriteHeavy => 1.0,
            Self::Balanced => 0.5,
            Self::CpuHeavy => 4.0,
        }
    }
}

fn env_or(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
}

fn env_parse<T>(name: &str, default: T) -> Result<T, String>
where
    T: std::str::FromStr,
{
    match env::var(name) {
        Ok(raw) => raw
            .parse()
            .map_err(|_| format!("{name} is not a valid number: {raw:?}")),
        Err(_) => Ok(default),
    }
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        let mix_name = env_or("WORKLOAD_MIX", "balanced");
        let mix = Mix::parse(&mix_name)
            .ok_or_else(|| format!("WORKLOAD_MIX is not a known mix: {mix_name:?}"))?;

        let trace_sample_ratio: f64 = env_parse("OTEL_TRACES_SAMPLER_ARG", 0.1)?;
        if !(0.0..=1.0).contains(&trace_sample_ratio) {
            return Err(format!(
                "OTEL_TRACES_SAMPLER_ARG must be within 0..=1, got {trace_sample_ratio}"
            ));
        }

        let postgres_pool_size = env_parse("POSTGRES_POOL_SIZE", 16usize)?;
        let redis_pool_size = env_parse("REDIS_POOL_SIZE", 16usize)?;
        let cpu_iterations = env_parse("CPU_ITERATIONS", 20_000u64)?;

        for (name, size) in [
            ("POSTGRES_POOL_SIZE", postgres_pool_size),
            ("REDIS_POOL_SIZE", redis_pool_size),
        ] {
            if size == 0 {
                return Err(format!("{name} must be at least 1"));
            }
        }

        Ok(Self {
            listen_addr: env_or("LISTEN_ADDR", "0.0.0.0:8080"),
            postgres_url: env_or("POSTGRES_URL", "postgres://bench:bench@postgres:5432/bench"),
            redis_url: env_or("REDIS_URL", "redis://redis:6379"),
            postgres_pool_size,
            redis_pool_size,
            mix,
            cpu_iterations,
            otlp_endpoint: env::var("OTEL_EXPORTER_OTLP_ENDPOINT")
                .ok()
                .filter(|value| !value.is_empty()),
            trace_sample_ratio,
            profile: env_or("WORKLOAD_PROFILE", "db-cache-mixed"),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mix_parse_rejects_unknown() {
        assert_eq!(Mix::parse("balanced"), Some(Mix::Balanced));
        assert_eq!(Mix::parse("cpu-heavy"), Some(Mix::CpuHeavy));
        assert_eq!(Mix::parse("nightmare"), None);
    }

    #[test]
    fn read_heavy_mix_weights_more_reads() {
        let (read, write) = Mix::ReadHeavy.weights();
        assert!(read > write);
    }

    #[test]
    fn cpu_heavy_scales_up_cpu_work() {
        assert!(Mix::CpuHeavy.cpu_scale() > Mix::ReadHeavy.cpu_scale());
    }
}
