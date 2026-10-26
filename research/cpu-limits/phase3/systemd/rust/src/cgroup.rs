//! Cgroup metrics reading and parsing

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tokio::fs;

/// QoS class for Kubernetes pods
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum QoSClass {
    BestEffort,
    Burstable,
    Guaranteed,
}

impl QoSClass {
    pub fn from_str(s: &str) -> Self {
        match s {
            "besteffort" => QoSClass::BestEffort,
            "burstable" => QoSClass::Burstable,
            "guaranteed" => QoSClass::Guaranteed,
            _ => QoSClass::Burstable,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            QoSClass::BestEffort => "besteffort",
            QoSClass::Burstable => "burstable",
            QoSClass::Guaranteed => "guaranteed",
        }
    }
}

/// Kubernetes pod information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PodInfo {
    pub uid: String,
    pub qos: QoSClass,
    pub cgroup_path: PathBuf,
    pub name: Option<String>,
    pub namespace: Option<String>,
}

impl PodInfo {
    /// Generate Prometheus label string
    pub fn prometheus_labels(&self) -> String {
        format!(r#"pod_uid="{}",qos="{}""#, self.uid, self.qos.as_str())
    }
}

/// CPU cgroup metrics from cpu.stat
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CgroupMetrics {
    pub usage_usec: u64,
    pub user_usec: u64,
    pub system_usec: u64,
    pub nr_periods: u64,
    pub nr_throttled: u64,
    pub throttled_usec: u64,
    pub nr_bursts: u64,
    pub burst_usec: u64,
}

impl CgroupMetrics {
    /// Parse cpu.stat file content
    pub fn from_cpu_stat(content: &str) -> Result<Self> {
        let mut metrics = CgroupMetrics::default();

        for line in content.lines() {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() != 2 {
                continue;
            }

            let key = parts[0];
            let value = parts[1].parse::<u64>().unwrap_or(0);

            match key {
                "usage_usec" => metrics.usage_usec = value,
                "user_usec" => metrics.user_usec = value,
                "system_usec" => metrics.system_usec = value,
                "nr_periods" => metrics.nr_periods = value,
                "nr_throttled" => metrics.nr_throttled = value,
                "throttled_usec" => metrics.throttled_usec = value,
                "nr_bursts" => metrics.nr_bursts = value,
                "burst_usec" => metrics.burst_usec = value,
                _ => {}
            }
        }

        Ok(metrics)
    }

    /// Calculate CPU usage percentage (requires two samples)
    pub fn usage_percent(&self, previous: &CgroupMetrics, interval_secs: u64) -> f64 {
        let delta_usec = self.usage_usec.saturating_sub(previous.usage_usec);
        let interval_usec = interval_secs * 1_000_000;
        
        if interval_usec == 0 {
            return 0.0;
        }

        (delta_usec as f64 / interval_usec as f64) * 100.0
    }

    /// Calculate throttle ratio (throttled / total)
    pub fn throttle_ratio(&self) -> f64 {
        let total_usec = self.usage_usec + self.throttled_usec;
        if total_usec == 0 {
            return 0.0;
        }
        self.throttled_usec as f64 / total_usec as f64
    }
}

/// Read cpu.stat from a cgroup path
pub async fn read_cpu_stat(cgroup_path: &Path) -> Result<CgroupMetrics> {
    let cpu_stat_path = cgroup_path.join("cpu.stat");
    
    let content = fs::read_to_string(&cpu_stat_path)
        .await
        .with_context(|| format!("Failed to read {}", cpu_stat_path.display()))?;

    CgroupMetrics::from_cpu_stat(&content)
}

/// Read PIDs from cgroup.procs
pub async fn read_procs(cgroup_path: &Path) -> Result<Vec<u32>> {
    let procs_path = cgroup_path.join("cgroup.procs");
    
    let content = fs::read_to_string(&procs_path)
        .await
        .with_context(|| format!("Failed to read {}", procs_path.display()))?;

    let pids = content
        .lines()
        .filter_map(|line| line.trim().parse::<u32>().ok())
        .collect();

    Ok(pids)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_cpu_stat() {
        let content = r#"usage_usec 123456789
user_usec 100000000
system_usec 23456789
nr_periods 1000
nr_throttled 250
throttled_usec 50000000
nr_bursts 10
burst_usec 1000000
"#;

        let metrics = CgroupMetrics::from_cpu_stat(content).unwrap();
        assert_eq!(metrics.usage_usec, 123456789);
        assert_eq!(metrics.nr_throttled, 250);
        assert_eq!(metrics.throttled_usec, 50000000);
    }

    #[test]
    fn test_throttle_ratio() {
        let metrics = CgroupMetrics {
            usage_usec: 100_000_000,
            throttled_usec: 50_000_000,
            ..Default::default()
        };

        let ratio = metrics.throttle_ratio();
        assert!((ratio - 0.333).abs() < 0.01);
    }

    #[test]
    fn test_usage_percent() {
        let prev = CgroupMetrics {
            usage_usec: 1_000_000,
            ..Default::default()
        };

        let current = CgroupMetrics {
            usage_usec: 3_000_000,
            ..Default::default()
        };

        // 2 seconds of CPU time used over 10 second interval
        let percent = current.usage_percent(&prev, 10);
        assert!((percent - 20.0).abs() < 0.1);
    }
}
