#!/usr/bin/env python3
"""
Phase 5 Task 5.2: Latency-Sensitive Workload Testing

Demonstrates the impact of CPU throttling on HTTP request latency (P50/P95/P99).

This is a critical gap in the current research - Phase 4 only measured throughput
(bogo ops/sec), not latency percentiles which matter more for user-facing services.

Approach:
1. Deploy nginx with 3 different CPU limit configurations
2. Generate constant HTTP load (1000 req/s for 2 minutes)
3. Measure P50/P95/P99 latency with wrk
4. Correlate latency spikes with CPU throttling metrics

Expected findings:
- No throttle: P99 < 10ms
- Light throttle (4.7%): P99 ~ 25ms
- Heavy throttle (69.8%): P99 > 500ms (50x worse!)
"""

import subprocess
import json
import time
from datetime import datetime, UTC
from pathlib import Path
import re

EVIDENCE_DIR = Path("evidence")

# Test configurations matching Phase 4 throttling levels
CONFIGS = [
    {
        "name": "nginx-no-throttle",
        "cpu_request": "2000m",
        "cpu_limit": "4000m",
        "expected_throttling": 0,
        "description": "Baseline - no throttling",
    },
    {
        "name": "nginx-light-throttle",
        "cpu_request": "1000m",
        "cpu_limit": "3000m",
        "expected_throttling": 4.7,
        "description": "Light throttling - 4.7% from Phase 4",
    },
    {
        "name": "nginx-heavy-throttle",
        "cpu_request": "100m",
        "cpu_limit": "500m",
        "expected_throttling": 69.8,
        "description": "Heavy throttling - 69.8% from Phase 4",
    },
]

WORKLOAD_DIR = Path("../experiments/latency-sensitive")


def run_command(cmd, check=True):
    """Run shell command and return output."""
    result = subprocess.run(
        cmd, shell=True, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def generate_nginx_manifest(config):
    """Generate nginx deployment with specified CPU resources."""
    manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {config["name"]}
  labels:
    app: nginx-latency-test
    test-type: {config["name"]}
spec:
  containers:
  - name: nginx
    image: nginx:alpine
    ports:
    - containerPort: 80
    resources:
      requests:
        cpu: "{config["cpu_request"]}"
      limits:
        cpu: "{config["cpu_limit"]}"
    # Nginx config for consistent behavior
    volumeMounts:
    - name: nginx-config
      mountPath: /etc/nginx/nginx.conf
      subPath: nginx.conf
  volumes:
  - name: nginx-config
    configMap:
      name: nginx-latency-config
---
apiVersion: v1
kind: Service
metadata:
  name: {config["name"]}-svc
spec:
  selector:
    test-type: {config["name"]}
  ports:
  - protocol: TCP
    port: 80
    targetPort: 80
  type: ClusterIP
"""
    return manifest


def generate_nginx_config():
    """Generate nginx config optimized for latency testing."""
    return """
events {
    worker_connections 1024;
}

http {
    access_log off;
    error_log /dev/null;
    
    server {
        listen 80;
        
        location / {
            return 200 "OK\\n";
            add_header Content-Type text/plain;
        }
        
        location /health {
            return 200 "healthy\\n";
            add_header Content-Type text/plain;
        }
    }
}
"""


def generate_wrk_script():
    """Generate Lua script for wrk to extract percentiles."""
    return """
-- wrk Lua script for detailed latency reporting
done = function(summary, latency, requests)
  io.write("---WRK_RESULTS_START---\\n")
  io.write(string.format("duration_s,%f\\n", summary.duration / 1000000))
  io.write(string.format("requests,%d\\n", summary.requests))
  io.write(string.format("bytes,%d\\n", summary.bytes))
  io.write(string.format("errors_connect,%d\\n", summary.errors.connect))
  io.write(string.format("errors_read,%d\\n", summary.errors.read))
  io.write(string.format("errors_write,%d\\n", summary.errors.write))
  io.write(string.format("errors_timeout,%d\\n", summary.errors.timeout))
  
  io.write(string.format("latency_min_us,%f\\n", latency.min))
  io.write(string.format("latency_max_us,%f\\n", latency.max))
  io.write(string.format("latency_mean_us,%f\\n", latency.mean))
  io.write(string.format("latency_stdev_us,%f\\n", latency.stdev))
  io.write(string.format("latency_p50_us,%f\\n", latency:percentile(50)))
  io.write(string.format("latency_p75_us,%f\\n", latency:percentile(75)))
  io.write(string.format("latency_p90_us,%f\\n", latency:percentile(90)))
  io.write(string.format("latency_p95_us,%f\\n", latency:percentile(95)))
  io.write(string.format("latency_p99_us,%f\\n", latency:percentile(99)))
  io.write(string.format("latency_p999_us,%f\\n", latency:percentile(99.9)))
  io.write("---WRK_RESULTS_END---\\n")
end
"""


def setup_nginx_config():
    """Create nginx ConfigMap."""
    print("Setting up nginx configuration...")

    nginx_conf = generate_nginx_config()

    # Create ConfigMap
    run_command(
        "kubectl delete configmap nginx-latency-config 2>/dev/null || true", check=False
    )
    cmd = f"kubectl create configmap nginx-latency-config --from-literal=nginx.conf='{nginx_conf}'"
    run_command(cmd)
    print("  ✓ ConfigMap created")


def deploy_workload(config):
    """Deploy nginx with specified configuration."""
    print(f"\n{'=' * 80}")
    print(f"Deploying: {config['name']}")
    print(f"  CPU Request: {config['cpu_request']}")
    print(f"  CPU Limit: {config['cpu_limit']}")
    print(f"  Expected Throttling: {config['expected_throttling']}%")
    print(f"{'=' * 80}")

    # Generate and apply manifest
    manifest = generate_nginx_manifest(config)
    manifest_file = WORKLOAD_DIR / f"{config['name']}.yaml"
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_file, "w") as f:
        f.write(manifest)

    run_command(f"kubectl apply -f {manifest_file}")

    # Wait for pod to be ready
    print("  Waiting for pod to be ready...")
    for i in range(60):
        status = run_command(
            f"kubectl get pod {config['name']} -o jsonpath='{{.status.phase}}'",
            check=False,
        )
        if status == "Running":
            # Check if container is ready
            ready = run_command(
                f"kubectl get pod {config['name']} -o jsonpath='{{.status.containerStatuses[0].ready}}'",
                check=False,
            )
            if ready == "true":
                print(f"  ✓ Pod is ready")
                break
        time.sleep(2)
    else:
        print(f"  ✗ Pod did not become ready within 120 seconds")
        return False

    # Test connectivity
    print("  Testing nginx connectivity...")
    svc_ip = run_command(
        f"kubectl get svc {config['name']}-svc -o jsonpath='{{.spec.clusterIP}}'"
    )
    test_cmd = f"kubectl run -it --rm curl-test --image=curlimages/curl --restart=Never -- curl -s http://{svc_ip}/health"
    result = run_command(test_cmd, check=False)
    if "healthy" in result:
        print(f"  ✓ Nginx is responding")
        return True
    else:
        print(f"  ✗ Nginx health check failed")
        return False


def run_load_test(config, duration_seconds=120, rate=1000):
    """Run wrk load test against nginx."""
    print(f"\nRunning load test...")
    print(f"  Duration: {duration_seconds}s")
    print(f"  Rate: {rate} req/s")

    svc_ip = run_command(
        f"kubectl get svc {config['name']}-svc -o jsonpath='{{.spec.clusterIP}}'"
    )
    url = f"http://{svc_ip}/"

    # Create wrk Lua script
    wrk_script = generate_wrk_script()
    script_file = WORKLOAD_DIR / "wrk-report.lua"
    with open(script_file, "w") as f:
        f.write(wrk_script)

    # Deploy wrk pod with the script
    wrk_manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: wrk-load-generator
spec:
  restartPolicy: Never
  containers:
  - name: wrk
    image: williamyeh/wrk:latest
    command: ["sh", "-c"]
    args:
    - |
      echo "Starting load test..."
      wrk -t4 -c100 -d{duration_seconds}s -R{rate} --latency -s /scripts/wrk-report.lua {url}
    volumeMounts:
    - name: wrk-scripts
      mountPath: /scripts
  volumes:
  - name: wrk-scripts
    configMap:
      name: wrk-scripts
"""

    # Create ConfigMap for wrk script
    run_command("kubectl delete configmap wrk-scripts 2>/dev/null || true", check=False)
    run_command(
        f"kubectl create configmap wrk-scripts --from-file=wrk-report.lua={script_file}"
    )

    # Deploy wrk pod
    wrk_manifest_file = WORKLOAD_DIR / "wrk-pod.yaml"
    with open(wrk_manifest_file, "w") as f:
        f.write(wrk_manifest)

    run_command(
        "kubectl delete pod wrk-load-generator 2>/dev/null || true", check=False
    )
    time.sleep(2)
    run_command(f"kubectl apply -f {wrk_manifest_file}")

    # Wait for wrk to start
    print("  Waiting for wrk to start...")
    time.sleep(5)

    # Monitor progress
    print(f"  Load test running... (0/{duration_seconds}s)")
    for i in range(0, duration_seconds, 10):
        time.sleep(10)
        elapsed = min(i + 10, duration_seconds)
        print(f"  Load test running... ({elapsed}/{duration_seconds}s)")

    # Wait for wrk to complete
    print("  Waiting for wrk to complete...")
    for i in range(30):
        status = run_command(
            "kubectl get pod wrk-load-generator -o jsonpath='{.status.phase}'",
            check=False,
        )
        if status == "Succeeded":
            break
        time.sleep(2)

    # Get wrk output
    logs = run_command("kubectl logs wrk-load-generator")

    # Cleanup
    run_command("kubectl delete pod wrk-load-generator", check=False)

    return parse_wrk_output(logs)


def parse_wrk_output(logs):
    """Parse wrk output to extract metrics."""
    results = {}

    # Extract structured results between markers
    match = re.search(
        r"---WRK_RESULTS_START---(.+?)---WRK_RESULTS_END---", logs, re.DOTALL
    )
    if match:
        lines = match.group(1).strip().split("\n")
        for line in lines:
            if "," in line:
                key, value = line.split(",", 1)
                try:
                    results[key] = float(value)
                except ValueError:
                    results[key] = value

    # Convert microseconds to milliseconds for readability
    for key in [
        "latency_min_us",
        "latency_max_us",
        "latency_mean_us",
        "latency_stdev_us",
        "latency_p50_us",
        "latency_p75_us",
        "latency_p90_us",
        "latency_p95_us",
        "latency_p99_us",
        "latency_p999_us",
    ]:
        if key in results:
            results[key.replace("_us", "_ms")] = results[key] / 1000

    return results


def get_cgroup_stats(pod_name):
    """Get cgroup CPU stats from the pod."""
    cmd = f"kubectl exec {pod_name} -- cat /sys/fs/cgroup/cpu.stat"
    output = run_command(cmd, check=False)

    stats = {}
    if output:
        for line in output.split("\n"):
            if " " in line:
                key, value = line.split(" ", 1)
                stats[key] = int(value)

    return stats


def measure_throttling(pod_name, duration_seconds):
    """Measure CPU throttling over a duration."""
    print(f"  Measuring CPU throttling...")

    stats_before = get_cgroup_stats(pod_name)
    time.sleep(duration_seconds)
    stats_after = get_cgroup_stats(pod_name)

    usage_delta = stats_after.get("usage_usec", 0) - stats_before.get("usage_usec", 0)
    throttled_delta = stats_after.get("throttled_usec", 0) - stats_before.get(
        "throttled_usec", 0
    )

    throttling_pct = (
        (throttled_delta / (usage_delta + throttled_delta) * 100)
        if (usage_delta + throttled_delta) > 0
        else 0
    )

    return {
        "usage_usec": usage_delta,
        "throttled_usec": throttled_delta,
        "throttling_percentage": throttling_pct,
        "nr_throttled": stats_after.get("nr_throttled", 0)
        - stats_before.get("nr_throttled", 0),
    }


def cleanup_workload(config):
    """Remove deployed workload."""
    print(f"\nCleaning up {config['name']}...")
    run_command(f"kubectl delete pod {config['name']}", check=False)
    run_command(f"kubectl delete svc {config['name']}-svc", check=False)


def main():
    print("=" * 80)
    print("Phase 5 Task 5.2: Latency-Sensitive Workload Testing")
    print("=" * 80)

    EVIDENCE_DIR.mkdir(exist_ok=True)
    WORKLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Setup
    setup_nginx_config()

    all_results = []

    # Run tests for each configuration
    for config in CONFIGS:
        try:
            # Deploy
            if not deploy_workload(config):
                print(f"Skipping load test due to deployment failure")
                continue

            # Warmup
            print("  Warming up (10s)...")
            time.sleep(10)

            # Run load test
            wrk_results = run_load_test(config, duration_seconds=120, rate=1000)

            # Measure throttling
            throttling = measure_throttling(config["name"], duration_seconds=30)

            # Combine results
            result = {
                "config": config,
                "latency": wrk_results,
                "throttling": throttling,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            all_results.append(result)

            # Print summary
            print(f"\n{'=' * 80}")
            print(f"Results: {config['name']}")
            print(f"{'=' * 80}")
            print(f"Latency:")
            print(f"  P50: {wrk_results.get('latency_p50_ms', 0):.2f}ms")
            print(f"  P95: {wrk_results.get('latency_p95_ms', 0):.2f}ms")
            print(f"  P99: {wrk_results.get('latency_p99_ms', 0):.2f}ms")
            print(f"Throttling:")
            print(f"  Percentage: {throttling['throttling_percentage']:.1f}%")
            print(f"  Events: {throttling['nr_throttled']}")

        finally:
            cleanup_workload(config)
            print(f"\nWaiting 10s before next test...")
            time.sleep(10)

    # Save results
    results_file = (
        EVIDENCE_DIR
        / f"latency-impact-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'=' * 80}")
    print("All Tests Complete")
    print(f"{'=' * 80}")
    print(f"Results saved to: {results_file}")

    # Print comparison table
    print(f"\n{'=' * 80}")
    print("Latency Comparison")
    print(f"{'=' * 80}")
    print(
        f"{'Configuration':<25} {'P50':>10} {'P95':>10} {'P99':>10} {'Throttling':>12}"
    )
    print("-" * 80)
    for result in all_results:
        name = result["config"]["name"]
        lat = result["latency"]
        throttle = result["throttling"]["throttling_percentage"]
        print(
            f"{name:<25} {lat.get('latency_p50_ms', 0):>9.2f}ms {lat.get('latency_p95_ms', 0):>9.2f}ms {lat.get('latency_p99_ms', 0):>9.2f}ms {throttle:>11.1f}%"
        )


if __name__ == "__main__":
    main()
