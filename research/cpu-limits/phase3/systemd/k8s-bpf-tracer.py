#!/usr/bin/env python3
"""
Kubernetes Container BPF Tracer Daemon

A systemd service that continuously monitors Kubernetes containers for CPU throttle
events using BPF, collects cgroup metrics, and exposes them via Prometheus metrics.

Runs on each Kubernetes node as a systemd service (outside container runtime).

Architecture:
- Watches for new pods via cgroup filesystem notifications
- Attaches BPF throttle event tracer to running containers
- Collects cgroup cpu.stat metrics periodically
- Exposes metrics via HTTP endpoint for Prometheus scraping

Features:
- No privileged container required (runs as host systemd service)
- Survives kubelet restarts
- Low overhead (BPF kernel tracing)
- Per-container metrics with pod/container labels
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set
from threading import Thread, Lock
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# Configuration
CGROUP_ROOT = Path("/sys/fs/cgroup")
KUBELET_CGROUP = CGROUP_ROOT / "kubepods.slice"
STATE_DIR = Path("/var/lib/k8s-bpf-tracer")
METRICS_PORT = 9101
COLLECTION_INTERVAL = 10  # seconds

# BPF script for throttle events
THROTTLE_BPF_SCRIPT = """
tracepoint:sched:sched_cfs_quota_throttled {
  printf("%lld,%s,%d,%lld\\n",
    nsecs / 1000000,  // timestamp_ms
    args->comm,        // process name
    args->pid,         // process ID
    args->throttled_usec  // throttled time in microseconds
  );
}
"""

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(STATE_DIR / "tracer.log"),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class PodInfo:
    """Kubernetes pod information extracted from cgroup path."""

    uid: str
    qos: str  # besteffort, burstable, guaranteed
    cgroup_path: Path
    name: Optional[str] = None
    namespace: Optional[str] = None

    @property
    def metric_labels(self) -> str:
        """Prometheus metric labels."""
        return f'pod_uid="{self.uid}",qos="{self.qos}"'


@dataclass
class CgroupMetrics:
    """CPU cgroup metrics."""

    usage_usec: int
    user_usec: int
    system_usec: int
    nr_periods: int
    nr_throttled: int
    throttled_usec: int
    nr_bursts: int = 0
    burst_usec: int = 0

    @classmethod
    def from_cpu_stat(cls, cpu_stat_content: str) -> "CgroupMetrics":
        """Parse cpu.stat file."""
        metrics = {}
        for line in cpu_stat_content.strip().split("\n"):
            if " " in line:
                key, value = line.split(None, 1)
                metrics[key] = int(value)

        return cls(
            usage_usec=metrics.get("usage_usec", 0),
            user_usec=metrics.get("user_usec", 0),
            system_usec=metrics.get("system_usec", 0),
            nr_periods=metrics.get("nr_periods", 0),
            nr_throttled=metrics.get("nr_throttled", 0),
            throttled_usec=metrics.get("throttled_usec", 0),
            nr_bursts=metrics.get("nr_bursts", 0),
            burst_usec=metrics.get("burst_usec", 0),
        )


@dataclass
class ThrottleEvent:
    """Single throttle event from BPF."""

    timestamp_ms: int
    comm: str
    pid: int
    throttled_usec: int


class MetricsCollector:
    """Collects and aggregates metrics from multiple pods."""

    def __init__(self):
        self.pods: Dict[str, PodInfo] = {}  # uid -> PodInfo
        self.metrics: Dict[str, CgroupMetrics] = {}  # uid -> latest metrics
        self.throttle_events: Dict[str, List[ThrottleEvent]] = {}  # uid -> events
        self.lock = Lock()

    def discover_pods(self):
        """Scan cgroup filesystem for Kubernetes pods."""
        discovered = set()

        if not KUBELET_CGROUP.exists():
            logger.warning(f"Kubelet cgroup not found: {KUBELET_CGROUP}")
            return

        # Scan for pod cgroups
        # Pattern: kubepods-<qos>.slice/kubepods-<qos>-pod<uid>.slice
        for qos in ["besteffort", "burstable", "guaranteed"]:
            qos_slice = KUBELET_CGROUP / f"kubepods-{qos}.slice"
            if not qos_slice.exists():
                continue

            for pod_dir in qos_slice.iterdir():
                if not pod_dir.is_dir():
                    continue

                # Extract pod UID from directory name
                # Format: kubepods-burstable-pod<uid>.slice
                match = re.match(
                    rf"kubepods-{qos}-pod([a-f0-9-]+)\.slice", pod_dir.name
                )
                if not match:
                    continue

                uid = match.group(1)
                discovered.add(uid)

                # Add new pod
                with self.lock:
                    if uid not in self.pods:
                        pod_info = PodInfo(uid=uid, qos=qos, cgroup_path=pod_dir)
                        self.pods[uid] = pod_info
                        self.throttle_events[uid] = []
                        logger.info(f"Discovered pod: {uid} (QoS: {qos})")

        # Remove terminated pods
        with self.lock:
            terminated = set(self.pods.keys()) - discovered
            for uid in terminated:
                logger.info(f"Pod terminated: {uid}")
                del self.pods[uid]
                if uid in self.metrics:
                    del self.metrics[uid]
                if uid in self.throttle_events:
                    del self.throttle_events[uid]

    def collect_cgroup_metrics(self):
        """Collect cgroup metrics for all pods."""
        with self.lock:
            pods = list(self.pods.values())

        for pod in pods:
            cpu_stat_file = pod.cgroup_path / "cpu.stat"
            if not cpu_stat_file.exists():
                continue

            try:
                content = cpu_stat_file.read_text()
                metrics = CgroupMetrics.from_cpu_stat(content)

                with self.lock:
                    self.metrics[pod.uid] = metrics

            except Exception as e:
                logger.error(f"Failed to read cpu.stat for {pod.uid}: {e}")

    def add_throttle_event(self, pid: int, event: ThrottleEvent):
        """Add a BPF throttle event (matched to pod by PID)."""
        # Find which pod this PID belongs to
        with self.lock:
            for pod in self.pods.values():
                procs_file = pod.cgroup_path / "cgroup.procs"
                if not procs_file.exists():
                    continue

                try:
                    pids = [
                        int(p) for p in procs_file.read_text().strip().split("\n") if p
                    ]
                    if pid in pids:
                        self.throttle_events[pod.uid].append(event)
                        return
                except Exception:
                    pass

    def get_prometheus_metrics(self) -> str:
        """Generate Prometheus metrics text format."""
        lines = [
            "# HELP k8s_container_cpu_usage_usec Total CPU usage in microseconds",
            "# TYPE k8s_container_cpu_usage_usec counter",
        ]

        with self.lock:
            for uid, pod in self.pods.items():
                metrics = self.metrics.get(uid)
                if not metrics:
                    continue

                labels = pod.metric_labels

                # CPU usage
                lines.append(
                    f"k8s_container_cpu_usage_usec{{{labels}}} {metrics.usage_usec}"
                )

                # Throttle metrics
                lines.extend(
                    [
                        f"# HELP k8s_container_throttled_periods_total Number of throttled periods",
                        f"# TYPE k8s_container_throttled_periods_total counter",
                        f"k8s_container_throttled_periods_total{{{labels}}} {metrics.nr_throttled}",
                        f"# HELP k8s_container_throttled_usec_total Total throttled time in microseconds",
                        f"# TYPE k8s_container_throttled_usec_total counter",
                        f"k8s_container_throttled_usec_total{{{labels}}} {metrics.throttled_usec}",
                    ]
                )

                # BPF event count
                event_count = len(self.throttle_events.get(uid, []))
                lines.extend(
                    [
                        f"# HELP k8s_container_bpf_throttle_events_total BPF-traced throttle events",
                        f"# TYPE k8s_container_bpf_throttle_events_total counter",
                        f"k8s_container_bpf_throttle_events_total{{{labels}}} {event_count}",
                    ]
                )

        return "\n".join(lines) + "\n"


class BPFTracer:
    """Manages BPF tracing subprocess."""

    def __init__(self, collector: MetricsCollector):
        self.collector = collector
        self.proc: Optional[subprocess.Popen] = None
        self.running = False

    def start(self):
        """Start BPF tracing subprocess."""
        if self.running:
            return

        logger.info("Starting BPF tracer...")

        try:
            # Write BPF script to temporary file
            script_file = STATE_DIR / "throttle.bt"
            script_file.write_text(THROTTLE_BPF_SCRIPT)

            # Start bpftrace
            self.proc = subprocess.Popen(
                ["bpftrace", str(script_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            self.running = True

            # Start reader thread
            Thread(target=self._read_events, daemon=True).start()

            logger.info("BPF tracer started")

        except Exception as e:
            logger.error(f"Failed to start BPF tracer: {e}")
            self.running = False

    def stop(self):
        """Stop BPF tracing."""
        if not self.running or not self.proc:
            return

        logger.info("Stopping BPF tracer...")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

        self.running = False
        logger.info("BPF tracer stopped")

    def _read_events(self):
        """Read throttle events from bpftrace stdout."""
        if not self.proc or not self.proc.stdout:
            return

        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                # Parse CSV output: timestamp_ms,comm,pid,throttled_usec
                parts = line.split(",")
                if len(parts) != 4:
                    continue

                event = ThrottleEvent(
                    timestamp_ms=int(parts[0]),
                    comm=parts[1],
                    pid=int(parts[2]),
                    throttled_usec=int(parts[3]),
                )

                self.collector.add_throttle_event(event.pid, event)

            except Exception as e:
                logger.debug(f"Failed to parse BPF event: {line} ({e})")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server."""

    daemon_threads = True


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint."""

    collector: MetricsCollector  # Set by server

    def log_message(self, format, *args):
        """Suppress access logs."""
        pass

    def do_GET(self):
        if self.path == "/metrics":
            metrics = self.collector.get_prometheus_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(metrics.encode("utf-8"))

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")

        else:
            self.send_response(404)
            self.end_headers()


def start_metrics_server(collector: MetricsCollector, port: int):
    """Start HTTP server for Prometheus metrics."""
    MetricsHandler.collector = collector

    server = ThreadedHTTPServer(("0.0.0.0", port), MetricsHandler)
    logger.info(f"Metrics server listening on port {port}")

    Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Kubernetes Container BPF Tracer")
    parser.add_argument(
        "--port", type=int, default=METRICS_PORT, help="Metrics HTTP port"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=COLLECTION_INTERVAL,
        help="Collection interval (seconds)",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=STATE_DIR, help="State directory"
    )
    parser.add_argument(
        "--no-bpf",
        action="store_true",
        help="Disable BPF tracing (cgroup metrics only)",
    )

    args = parser.parse_args()

    # Create state directory
    args.state_dir.mkdir(parents=True, exist_ok=True)
    global STATE_DIR
    STATE_DIR = args.state_dir

    logger.info("Starting Kubernetes Container BPF Tracer")
    logger.info(f"  State directory: {args.state_dir}")
    logger.info(f"  Metrics port: {args.port}")
    logger.info(f"  Collection interval: {args.interval}s")
    logger.info(f"  BPF tracing: {'disabled' if args.no_bpf else 'enabled'}")

    # Check bpftrace availability
    if not args.no_bpf:
        try:
            subprocess.run(["bpftrace", "--version"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.error(
                "bpftrace not found or not working. Install with: sudo dnf install bpftrace"
            )
            logger.info("Continuing without BPF tracing (cgroup metrics only)")
            args.no_bpf = True

    # Initialize collector
    collector = MetricsCollector()

    # Start BPF tracer
    tracer = None
    if not args.no_bpf:
        tracer = BPFTracer(collector)
        tracer.start()

    # Start metrics server
    server = start_metrics_server(collector, args.port)

    # Main loop
    try:
        while True:
            # Discover pods
            collector.discover_pods()

            # Collect cgroup metrics
            collector.collect_cgroup_metrics()

            # Sleep
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Shutting down...")

    finally:
        if tracer:
            tracer.stop()
        server.shutdown()
        logger.info("Stopped")


if __name__ == "__main__":
    main()
