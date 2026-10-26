//! BPF tracing for throttle events

use anyhow::{Context, Result};
use std::path::PathBuf;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

use crate::cgroup;
use crate::metrics::{MetricsRegistry, ThrottleEvent};

/// BPF script for tracing CPU throttle events
const THROTTLE_BPF_SCRIPT: &str = r#"
BEGIN {
    printf("timestamp_ms,comm,pid,throttled_usec\n");
}

tracepoint:sched:sched_cfs_quota_throttled {
    printf("%lld,%s,%d,%lld\n",
        nsecs / 1000000,
        args->comm,
        args->pid,
        args->throttled_usec
    );
}
"#;

/// Run BPF tracer and feed events to registry
pub async fn run_tracer(
    registry: Arc<RwLock<MetricsRegistry>>,
    state_dir: PathBuf,
) -> Result<()> {
    info!("Starting BPF tracer...");

    // Write BPF script to file
    let script_path = state_dir.join("throttle.bt");
    tokio::fs::write(&script_path, THROTTLE_BPF_SCRIPT)
        .await
        .context("Failed to write BPF script")?;

    // Start bpftrace
    let mut child = Command::new("bpftrace")
        .arg(&script_path)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .context("Failed to spawn bpftrace")?;

    let stdout = child
        .stdout
        .take()
        .context("Failed to capture bpftrace stdout")?;

    info!("BPF tracer started");

    // Read events from stdout
    let mut reader = BufReader::new(stdout).lines();
    let mut header_seen = false;

    while let Some(line) = reader.next_line().await? {
        let line = line.trim();

        // Skip empty lines
        if line.is_empty() {
            continue;
        }

        // Skip CSV header
        if !header_seen && line.starts_with("timestamp_ms") {
            header_seen = true;
            continue;
        }

        // Parse CSV: timestamp_ms,comm,pid,throttled_usec
        match parse_throttle_event(line) {
            Ok(event) => {
                // Find which pod this PID belongs to
                if let Some(pod_uid) = find_pod_for_pid(&registry, event.pid).await {
                    let mut registry = registry.write().await;
                    registry.add_throttle_event(&pod_uid, event);
                }
            }
            Err(e) => {
                debug!("Failed to parse BPF event '{}': {}", line, e);
            }
        }
    }

    // Wait for child process
    let status = child.wait().await?;
    warn!("BPF tracer exited with status: {}", status);

    Ok(())
}

/// Parse a CSV line into a ThrottleEvent
fn parse_throttle_event(line: &str) -> Result<ThrottleEvent> {
    let parts: Vec<&str> = line.split(',').collect();
    
    if parts.len() != 4 {
        anyhow::bail!("Expected 4 fields, got {}", parts.len());
    }

    Ok(ThrottleEvent {
        timestamp_ms: parts[0].parse().context("Invalid timestamp")?,
        comm: parts[1].to_string(),
        pid: parts[2].parse().context("Invalid PID")?,
        throttled_usec: parts[3].parse().context("Invalid throttled_usec")?,
    })
}

/// Find which pod a PID belongs to by checking cgroup.procs
async fn find_pod_for_pid(
    registry: &Arc<RwLock<MetricsRegistry>>,
    pid: u32,
) -> Option<String> {
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
            match cgroup::read_procs(&pod.cgroup_path).await {
                Ok(pids) => {
                    if pids.contains(&pid) {
                        return Some(uid);
                    }
                }
                Err(e) => {
                    debug!("Failed to read procs for {}: {}", uid, e);
                }
            }
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_throttle_event() {
        let line = "1234567890,stress-ng-cpu,12345,100000";
        let event = parse_throttle_event(line).unwrap();
        
        assert_eq!(event.timestamp_ms, 1234567890);
        assert_eq!(event.comm, "stress-ng-cpu");
        assert_eq!(event.pid, 12345);
        assert_eq!(event.throttled_usec, 100000);
    }

    #[test]
    fn test_parse_invalid_event() {
        let line = "invalid,data";
        assert!(parse_throttle_event(line).is_err());
    }
}
