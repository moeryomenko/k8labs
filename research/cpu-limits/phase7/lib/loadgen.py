#!/usr/bin/env python3
"""
Fixed-rate HTTP load generator.

wrk2 on this cluster fails with a Lua panic before issuing any request, and
the host has no wrk either. This script is the replacement: it opens a fixed
number of keep-alive connections, dispatches requests at a fixed rate, and
records a latency sample for every request.

Usage:
    loadgen.py --url http://10.244.5.22/ --rate 500 --duration 20 \
               --connections 20 --out /tmp/result.json

Output (JSON on stdout and in --out):
    achieved_rps, dispatched, completed, errors,
    latency_ms.p50/p90/p99/p999/max/mean, status_counts
"""

import argparse
import json
import queue
import statistics
import sys
import threading
import time
import http.client
import urllib.parse


class LoadGen:
    def __init__(self, url, rate, duration, connections, timeout=5.0, keepalive=True):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.rate = rate
        self.duration = duration
        self.connections = connections
        self.timeout = timeout
        self.keepalive = keepalive

        self.stop = threading.Event()
        self.latencies = []
        self.status_counts = {}
        self.errors = []
        self.lock = threading.Lock()

    def worker(self, work_q):
        conn = None
        while True:
            try:
                due = work_q.get(timeout=0.5)
            except queue.Empty:
                if self.stop.is_set():
                    return
                continue
            if due is None:
                return

            now = time.perf_counter()
            if due > now:
                time.sleep(due - now)

            start = time.perf_counter()
            status = None
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(
                        self.host, self.port, timeout=self.timeout
                    )
                conn.request("GET", self.path)
                resp = conn.getresponse()
                resp.read()
                status = resp.status
                if not self.keepalive:
                    # Close per request so the server pays connection setup.
                    conn.close()
                    conn = None
            except Exception as exc:
                with self.lock:
                    self.errors.append(type(exc).__name__)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                continue

            elapsed_ms = (time.perf_counter() - start) * 1000
            with self.lock:
                self.latencies.append(elapsed_ms)
                self.status_counts[str(status)] = (
                    self.status_counts.get(str(status), 0) + 1
                )

        if conn is not None:
            conn.close()

    def run(self):
        work_q = queue.Queue(maxsize=self.rate * 2)
        threads = [
            threading.Thread(target=self.worker, args=(work_q,), daemon=True)
            for _ in range(self.connections)
        ]
        for t in threads:
            t.start()

        start = time.perf_counter()
        interval = 1.0 / self.rate
        dispatched = 0
        next_due = start
        while True:
            now = time.perf_counter()
            if now - start >= self.duration:
                break
            if next_due <= now:
                try:
                    work_q.put_nowait(next_due)
                    dispatched += 1
                except queue.Full:
                    pass
                next_due += interval
            else:
                time.sleep(min(next_due - now, 0.01))

        elapsed = time.perf_counter() - start
        for _ in threads:
            work_q.put(None)
        for t in threads:
            t.join(timeout=self.timeout + 1)

        return self.summarize(elapsed, dispatched)

    def summarize(self, elapsed, dispatched):
        lat = sorted(self.latencies)
        completed = len(lat)

        def pct(p):
            if not lat:
                return None
            idx = min(int(round(p / 100 * (len(lat) - 1))), len(lat) - 1)
            return round(lat[idx], 3)

        return {
            "url": f"http://{self.host}:{self.port}{self.path}",
            "target_rate_rps": self.rate,
            "connections": self.connections,
            "keepalive": self.keepalive,
            "duration_sec": round(elapsed, 3),
            "dispatched": dispatched,
            "completed": completed,
            "errors": len(self.errors),
            "error_kinds": sorted(set(self.errors)),
            "achieved_rps": round(completed / elapsed, 2) if elapsed else None,
            "status_counts": self.status_counts,
            "latency_ms": {
                "mean": round(statistics.fmean(lat), 3) if lat else None,
                "p50": pct(50),
                "p90": pct(90),
                "p99": pct(99),
                "p99.9": pct(99.9),
                "max": round(lat[-1], 3) if lat else None,
            },
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--rate", type=int, required=True)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--connections", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument(
        "--no-keepalive",
        action="store_true",
        help="close the connection after every request",
    )
    ap.add_argument("--out")
    args = ap.parse_args()

    gen = LoadGen(args.url, args.rate, args.duration, args.connections, args.timeout)
    result = gen.run()

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
