//! Kubernetes Container BPF Tracer
//!
//! A high-performance systemd service that monitors Kubernetes containers for CPU
//! throttle events using BPF, collects cgroup metrics, and exposes them via
//! Prometheus metrics.
//!
//! Architecture:
//! - Watches for new pods via cgroup filesystem notifications
//! - Attaches BPF throttle event tracer to running containers
//! - Collects cgroup cpu.stat metrics periodically
//! - Exposes metrics via HTTP endpoint for Prometheus scraping
//!
//! Features:
//! - No privileged container required (runs as host systemd service)
//! - Async/await for efficient I/O
//! - Low overhead (BPF kernel tracing)
//! - Per-container metrics with pod/container labels

use anyhow::{Context, Result};
use axum::{extract::State, http::StatusCode, response::IntoResponse, routing::get, Router};
use clap::Parser;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::fs;
use tokio::process::Command;
use tokio::sync::RwLock;
use tokio::time;
use tracing::{debug, error, info, warn};

mod bpf;
mod cgroup;
mod metrics;

use cgroup::{PodInfo, QoSClass};
use metrics::MetricsRegistry;

/// Command-line arguments
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// HTTP port for metrics endpoint
    #[arg(short, long, default_value = "9101")]
    port: u16,

    /// Collection interval in seconds
    #[arg(short, long, default_value = "10")]
    interval: u64,

    /// State directory for logs and temporary files
    #[arg(short, long, default_value = "/var/lib/k8s-bpf-tracer")]
    state_dir: PathBuf,

    /// Disable BPF tracing (cgroup metrics only)
    #[arg(long)]
    no_bpf: bool,

    /// Cgroup root path
    #[arg(long, default_value = "/sys/fs/cgroup")]
    cgroup_root: PathBuf,
}

/// Application state shared across tasks
#[derive(Clone)]
struct AppState {
    registry: Arc<RwLock<MetricsRegistry>>,
    args: Arc<Args>,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Parse CLI args
    let args = Args::parse();

    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "k8s_bpf_tracer=info,tower_http=debug".into()),
        )
        .with_target(false)
        .with_thread_ids(false)
        .with_file(true)
        .with_line_number(true)
        .init();

    info!(
        "Starting Kubernetes Container BPF Tracer v{}",
        env!("CARGO_PKG_VERSION")
    );
    info!("  State directory: {}", args.state_dir.display());
    info!("  Metrics port: {}", args.port);
    info!("  Collection interval: {}s", args.interval);
    info!(
        "  BPF tracing: {}",
        if args.no_bpf { "disabled" } else { "enabled" }
    );
    info!("  Cgroup root: {}", args.cgroup_root.display());

    // Create state directory
    fs::create_dir_all(&args.state_dir)
        .await
        .context("Failed to create state directory")?;

    // Check bpftrace availability
    let bpf_enabled = if !args.no_bpf {
        match Command::new("bpftrace").arg("--version").output().await {
            Ok(output) if output.status.success() => {
                let version = String::from_utf8_lossy(&output.stdout);
                info!("bpftrace found: {}", version.trim());
                true
            }
            _ => {
                warn!("bpftrace not found or not working. Install with: sudo dnf install bpftrace");
                warn!("Continuing without BPF tracing (cgroup metrics only)");
                false
            }
        }
    } else {
        false
    };

    // Initialize shared state
    let registry = Arc::new(RwLock::new(MetricsRegistry::new()));
    let args = Arc::new(args);
    let state = AppState {
        registry: registry.clone(),
        args: args.clone(),
    };

    // Start BPF tracer task
    let bpf_handle = if bpf_enabled {
        let registry = registry.clone();
        let state_dir = args.state_dir.clone();
        Some(tokio::spawn(async move {
            if let Err(e) = bpf::run_tracer(registry, state_dir).await {
                error!("BPF tracer failed: {}", e);
            }
        }))
    } else {
        None
    };

    // Start pod discovery task
    let discovery_handle = {
        let registry = registry.clone();
        let cgroup_root = args.cgroup_root.clone();
        tokio::spawn(async move {
            if let Err(e) = run_discovery(registry, cgroup_root).await {
                error!("Pod discovery failed: {}", e);
            }
        })
    };

    // Start metrics collection task
    let collection_handle = {
        let registry = registry.clone();
        let interval = args.interval;
        tokio::spawn(async move {
            if let Err(e) = run_collection(registry, interval).await {
                error!("Metrics collection failed: {}", e);
            }
        })
    };

    // Start HTTP server
    let server_handle = {
        let state = state.clone();
        let port = args.port;
        tokio::spawn(async move {
            if let Err(e) = run_server(state, port).await {
                error!("HTTP server failed: {}", e);
            }
        })
    };

    // Wait for tasks (any failure will cause shutdown)
    tokio::select! {
        _ = discovery_handle => warn!("Pod discovery task exited"),
        _ = collection_handle => warn!("Metrics collection task exited"),
        _ = server_handle => warn!("HTTP server task exited"),
        Some(handle) = async { bpf_handle } => {
            handle.await.ok();
            warn!("BPF tracer task exited");
        }
    }

    info!("Shutting down...");
    Ok(())
}

/// Pod discovery task - scans cgroup filesystem for Kubernetes pods
async fn run_discovery(registry: Arc<RwLock<MetricsRegistry>>, cgroup_root: PathBuf) -> Result<()> {
    let kubelet_cgroup = cgroup_root.join("kubepods.slice");

    // Wait for kubelet cgroup to exist
    while !kubelet_cgroup.exists() {
        warn!(
            "Kubelet cgroup not found: {}, retrying...",
            kubelet_cgroup.display()
        );
        time::sleep(Duration::from_secs(10)).await;
    }

    let pod_pattern = Regex::new(r"kubepods-(\w+)-pod([a-f0-9-]+)\.slice$")?;

    loop {
        // Scan for pods
        let mut discovered_pods = HashMap::new();

        for qos in &["besteffort", "burstable", "guaranteed"] {
            let qos_slice = kubelet_cgroup.join(format!("kubepods-{}.slice", qos));

            if !qos_slice.exists() {
                continue;
            }

            let mut entries = match fs::read_dir(&qos_slice).await {
                Ok(e) => e,
                Err(e) => {
                    warn!("Failed to read {}: {}", qos_slice.display(), e);
                    continue;
                }
            };

            while let Some(entry) = entries.next_entry().await? {
                let path = entry.path();

                if !path.is_dir() {
                    continue;
                }

                let dir_name = match path.file_name() {
                    Some(name) => name.to_string_lossy(),
                    None => continue,
                };

                if let Some(caps) = pod_pattern.captures(&dir_name) {
                    let qos_str = &caps[1];
                    let uid = &caps[2];

                    let qos = QoSClass::from_str(qos_str);

                    let pod = PodInfo {
                        uid: uid.to_string(),
                        qos,
                        cgroup_path: path.clone(),
                        name: None,
                        namespace: None,
                    };

                    discovered_pods.insert(uid.to_string(), pod);
                }
            }
        }

        // Update registry
        {
            let mut registry = registry.write().await;

            // Add new pods
            for (uid, pod) in &discovered_pods {
                if !registry.has_pod(uid) {
                    info!("Discovered pod: {} (QoS: {:?})", uid, pod.qos);
                    registry.add_pod(pod.clone());
                }
            }

            // Remove terminated pods
            let current_pods: Vec<String> = registry.list_pods();
            for uid in current_pods {
                if !discovered_pods.contains_key(&uid) {
                    info!("Pod terminated: {}", uid);
                    registry.remove_pod(&uid);
                }
            }
        }

        // Sleep before next scan
        time::sleep(Duration::from_secs(5)).await;
    }
}

/// Metrics collection task - periodically reads cgroup metrics
async fn run_collection(registry: Arc<RwLock<MetricsRegistry>>, interval_secs: u64) -> Result<()> {
    let mut interval = time::interval(Duration::from_secs(interval_secs));

    loop {
        interval.tick().await;

        let pods = {
            let registry = registry.read().await;
            registry.list_pods()
        };

        for uid in pods {
            let pod = {
                let registry = registry.read().await;
                registry.get_pod(&uid).cloned()
            };

            if let Some(pod) = pod {
                match cgroup::read_cpu_stat(&pod.cgroup_path).await {
                    Ok(metrics) => {
                        let mut registry = registry.write().await;
                        registry.update_metrics(&uid, metrics);
                    }
                    Err(e) => {
                        debug!("Failed to read cpu.stat for {}: {}", uid, e);
                    }
                }
            }
        }
    }
}

/// HTTP server for Prometheus metrics
async fn run_server(state: AppState, port: u16) -> Result<()> {
    let app = Router::new()
        .route("/metrics", get(metrics_handler))
        .route("/health", get(health_handler))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", port))
        .await
        .context("Failed to bind metrics server")?;

    info!("Metrics server listening on port {}", port);

    axum::serve(listener, app)
        .await
        .context("HTTP server error")?;

    Ok(())
}

/// GET /metrics - Prometheus metrics endpoint
async fn metrics_handler(State(state): State<AppState>) -> impl IntoResponse {
    let registry = state.registry.read().await;
    let metrics = registry.render_prometheus();

    (
        StatusCode::OK,
        [("Content-Type", "text/plain; version=0.0.4")],
        metrics,
    )
}

/// GET /health - Health check endpoint
async fn health_handler() -> impl IntoResponse {
    (StatusCode::OK, "OK\n")
}
