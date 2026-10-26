//! Metrics registry and Prometheus rendering

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::cgroup::{CgroupMetrics, PodInfo};

/// Single BPF throttle event
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThrottleEvent {
    pub timestamp_ms: u64,
    pub comm: String,
    pub pid: u32,
    pub throttled_usec: u64,
}

/// Central registry for all metrics
pub struct MetricsRegistry {
    pods: HashMap<String, PodInfo>,
    metrics: HashMap<String, CgroupMetrics>,
    throttle_events: HashMap<String, Vec<ThrottleEvent>>,
}

impl MetricsRegistry {
    pub fn new() -> Self {
        Self {
            pods: HashMap::new(),
            metrics: HashMap::new(),
            throttle_events: HashMap::new(),
        }
    }

    pub fn add_pod(&mut self, pod: PodInfo) {
        let uid = pod.uid.clone();
        self.pods.insert(uid.clone(), pod);
        self.throttle_events.insert(uid, Vec::new());
    }

    pub fn remove_pod(&mut self, uid: &str) {
        self.pods.remove(uid);
        self.metrics.remove(uid);
        self.throttle_events.remove(uid);
    }

    pub fn has_pod(&self, uid: &str) -> bool {
        self.pods.contains_key(uid)
    }

    pub fn get_pod(&self, uid: &str) -> Option<&PodInfo> {
        self.pods.get(uid)
    }

    pub fn list_pods(&self) -> Vec<String> {
        self.pods.keys().cloned().collect()
    }

    pub fn update_metrics(&mut self, uid: &str, metrics: CgroupMetrics) {
        self.metrics.insert(uid.to_string(), metrics);
    }

    pub fn add_throttle_event(&mut self, uid: &str, event: ThrottleEvent) {
        if let Some(events) = self.throttle_events.get_mut(uid) {
            events.push(event);
        }
    }

    /// Render all metrics in Prometheus text format
    pub fn render_prometheus(&self) -> String {
        let mut lines = Vec::new();

        // CPU usage metrics
        lines.push(
            "# HELP k8s_container_cpu_usage_usec Total CPU usage in microseconds".to_string(),
        );
        lines.push("# TYPE k8s_container_cpu_usage_usec counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();

                lines.push(format!(
                    "k8s_container_cpu_usage_usec{{{}}} {}",
                    labels, metrics.usage_usec
                ));
            }
        }

        lines.push("".to_string());

        // User/system CPU time
        lines.push(
            "# HELP k8s_container_cpu_user_usec CPU time spent in user mode in microseconds"
                .to_string(),
        );
        lines.push("# TYPE k8s_container_cpu_user_usec counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();
                lines.push(format!(
                    "k8s_container_cpu_user_usec{{{}}} {}",
                    labels, metrics.user_usec
                ));
            }
        }

        lines.push("".to_string());

        lines.push(
            "# HELP k8s_container_cpu_system_usec CPU time spent in system mode in microseconds"
                .to_string(),
        );
        lines.push("# TYPE k8s_container_cpu_system_usec counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();
                lines.push(format!(
                    "k8s_container_cpu_system_usec{{{}}} {}",
                    labels, metrics.system_usec
                ));
            }
        }

        lines.push("".to_string());

        // Throttle metrics
        lines.push(
            "# HELP k8s_container_throttled_periods_total Number of throttled periods".to_string(),
        );
        lines.push("# TYPE k8s_container_throttled_periods_total counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();
                lines.push(format!(
                    "k8s_container_throttled_periods_total{{{}}} {}",
                    labels, metrics.nr_throttled
                ));
            }
        }

        lines.push("".to_string());

        lines.push(
            "# HELP k8s_container_throttled_usec_total Total throttled time in microseconds"
                .to_string(),
        );
        lines.push("# TYPE k8s_container_throttled_usec_total counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();
                lines.push(format!(
                    "k8s_container_throttled_usec_total{{{}}} {}",
                    labels, metrics.throttled_usec
                ));
            }
        }

        lines.push("".to_string());

        // Throttle ratio (gauge)
        lines.push(
            "# HELP k8s_container_throttle_ratio Ratio of throttled time to total time".to_string(),
        );
        lines.push("# TYPE k8s_container_throttle_ratio gauge".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                let labels = pod.prometheus_labels();
                let ratio = metrics.throttle_ratio();
                lines.push(format!(
                    "k8s_container_throttle_ratio{{{}}} {:.6}",
                    labels, ratio
                ));
            }
        }

        lines.push("".to_string());

        // BPF throttle events
        lines.push(
            "# HELP k8s_container_bpf_throttle_events_total BPF-traced throttle events".to_string(),
        );
        lines.push("# TYPE k8s_container_bpf_throttle_events_total counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(events) = self.throttle_events.get(uid) {
                let labels = pod.prometheus_labels();
                lines.push(format!(
                    "k8s_container_bpf_throttle_events_total{{{}}} {}",
                    labels,
                    events.len()
                ));
            }
        }

        lines.push("".to_string());

        // BPF total throttled time
        lines.push("# HELP k8s_container_bpf_throttled_usec_total Total BPF-traced throttled time in microseconds".to_string());
        lines.push("# TYPE k8s_container_bpf_throttled_usec_total counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(events) = self.throttle_events.get(uid) {
                let labels = pod.prometheus_labels();
                let total_usec: u64 = events.iter().map(|e| e.throttled_usec).sum();
                lines.push(format!(
                    "k8s_container_bpf_throttled_usec_total{{{}}} {}",
                    labels, total_usec
                ));
            }
        }

        lines.push("".to_string());

        // Burst metrics (if available)
        lines.push(
            "# HELP k8s_container_burst_periods_total Number of CPU burst periods".to_string(),
        );
        lines.push("# TYPE k8s_container_burst_periods_total counter".to_string());

        for (uid, pod) in &self.pods {
            if let Some(metrics) = self.metrics.get(uid) {
                if metrics.nr_bursts > 0 {
                    let labels = pod.prometheus_labels();
                    lines.push(format!(
                        "k8s_container_burst_periods_total{{{}}} {}",
                        labels, metrics.nr_bursts
                    ));
                }
            }
        }

        lines.push("".to_string());

        lines.join("\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cgroup::QoSClass;
    use std::path::PathBuf;

    #[test]
    fn test_registry_basic() {
        let mut registry = MetricsRegistry::new();

        let pod = PodInfo {
            uid: "test-uid".to_string(),
            qos: QoSClass::Burstable,
            cgroup_path: PathBuf::from("/test"),
            name: None,
            namespace: None,
        };

        registry.add_pod(pod);
        assert!(registry.has_pod("test-uid"));

        let metrics = CgroupMetrics {
            usage_usec: 100_000,
            throttled_usec: 50_000,
            nr_throttled: 10,
            ..Default::default()
        };

        registry.update_metrics("test-uid", metrics);

        let output = registry.render_prometheus();
        assert!(output.contains("k8s_container_cpu_usage_usec"));
        assert!(output.contains("test-uid"));
    }

    #[test]
    fn test_throttle_events() {
        let mut registry = MetricsRegistry::new();

        let pod = PodInfo {
            uid: "test-uid".to_string(),
            qos: QoSClass::Guaranteed,
            cgroup_path: PathBuf::from("/test"),
            name: None,
            namespace: None,
        };

        registry.add_pod(pod);

        let event = ThrottleEvent {
            timestamp_ms: 1000,
            comm: "test".to_string(),
            pid: 123,
            throttled_usec: 5000,
        };

        registry.add_throttle_event("test-uid", event);

        let output = registry.render_prometheus();
        assert!(output.contains("k8s_container_bpf_throttle_events_total"));
        assert!(output.contains("1")); // one event
    }
}
