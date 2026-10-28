//! Reference service for the phase 8 CPU-limit campaign.
//!
//! The service is a fixed measuring instrument: request mix, CPU work, and pool
//! sizes come from environment variables so campaign manifests can vary them
//! without rebuilding the image. See `contracts/` for the telemetry and workload
//! identity that `fitter` matches before it will act on the resulting model.

mod config;
mod deps;
mod http;
mod telemetry;

use std::sync::Arc;
use std::time::Duration;

use tracing_subscriber::EnvFilter;

use crate::config::Config;
use crate::deps::Dependencies;
use crate::http::{AppState, Counters};

#[tokio::main]
async fn main() {
    if let Err(err) = run().await {
        eprintln!("fatal: {err}");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), String> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .json()
        .init();

    let config = Config::from_env()?;
    let telemetry = telemetry::init(&config)?;

    let deps = Dependencies::connect(
        &config.postgres_url,
        &config.redis_url,
        config.postgres_pool_size,
        config.redis_pool_size,
    )
    .await?;

    // Dependencies may come up after us; keep trying instead of CrashLooping
    // away measurement time, but never serve traffic before the schema exists.
    migrate_with_retry(&deps).await?;

    let state = Arc::new(AppState {
        deps,
        config: config.clone(),
        counters: Counters::default(),
    });

    let listener = tokio::net::TcpListener::bind(&config.listen_addr)
        .await
        .map_err(|err| format!("binding {}: {err}", config.listen_addr))?;

    tracing::info!(
        addr = %config.listen_addr,
        mix = config.mix.as_str(),
        profile = %config.profile,
        "cpu-limit-bench listening"
    );

    let server = axum::serve(listener, http::router(state)).with_graceful_shutdown(shutdown());

    let result = server.await.map_err(|err| format!("serving: {err}"));
    telemetry.shutdown();
    result
}

async fn migrate_with_retry(deps: &Dependencies) -> Result<(), String> {
    let mut attempt = 0;
    loop {
        attempt += 1;
        match deps.migrate().await {
            Ok(()) => return Ok(()),
            Err(err) if attempt < 60 => {
                tracing::warn!(attempt, error = %err, "schema not ready; retrying");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            Err(err) => return Err(format!("schema never became ready: {err}")),
        }
    }
}

async fn shutdown() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(err) => tracing::warn!(error = %err, "no SIGTERM handler installed"),
        }
    };
    tokio::select! {
        () = ctrl_c => {}
        () = terminate => {}
    }
    tracing::info!("shutting down");
}
