#!/usr/bin/env bash
# Load generator for api-server — generates HTTP traffic with configurable
# rate, endpoint mix, and concurrency.
#
# Usage: load-generator.sh <target-url> [OPTIONS]
#
# Options:
#   --rate N        Target requests per second (default: 50)
#   --duration N    Duration in seconds (default: 60)
#   --endpoints S   Comma-separated endpoint weights (default: "users:30,orders:30,search:20,reports:20")
#   --output FILE   Output file for latency CSV (default: stdout summary only)
#   --workers N     Number of parallel workers (default: 5)
#
# Output:
#   stderr: real-time progress (requests completed, errors, current latency)
#   stdout: final summary statistics (p50/p95/p99 latency, total, errors)
#   CSV file (with --output): per-request data (timestamp, endpoint, latency_ms, status)
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
RATE=50
DURATION=60
ENDPOINTS="users:30,orders:30,search:20,reports:20"
OUTPUT_FILE=""
WORKERS=5

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
show_help() {
	cat <<'EOF'
Usage: load-generator.sh <target-url> [OPTIONS]

Generate HTTP load against the api-server.

Positional:
  target-url  Base URL of the api-server (e.g. http://localhost:8080)

Options:
  --rate N        Target requests per second (default: 50)
  --duration N    Duration in seconds (default: 60)
  --endpoints S   Comma-separated endpoint weights (default: "users:30,orders:30,search:20,reports:20")
  --output FILE   Output file for latency CSV (default: stdout summary only)
  --workers N     Number of parallel workers (default: 5)
  --help          Show this help and exit

Examples:
  load-generator.sh http://localhost:8080
  load-generator.sh http://10.0.0.1:8080 --rate 100 --duration 30 --output results.csv
  load-generator.sh http://localhost:8080 --endpoints "users:50,search:50" --workers 10
EOF
	exit 0
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
TARGET_URL=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--help) show_help ;;
		--rate) RATE="$2"; shift 2 ;;
		--duration) DURATION="$2"; shift 2 ;;
		--endpoints) ENDPOINTS="$2"; shift 2 ;;
		--output) OUTPUT_FILE="$2"; shift 2 ;;
		--workers) WORKERS="$2"; shift 2 ;;
		--*) echo "error: unknown option $1" >&2; exit 1 ;;
		*) TARGET_URL="$1"; shift ;;
	esac
done

if [[ -z "$TARGET_URL" ]]; then
	echo "error: target-url is required" >&2
	echo "usage: load-generator.sh <target-url> [OPTIONS]" >&2
	exit 1
fi

# Strip trailing slash.
TARGET_URL="${TARGET_URL%/}"

# ---------------------------------------------------------------------------
# Parse endpoint weights
# ---------------------------------------------------------------------------
declare -a ENDPOINT_NAMES=()
declare -a ENDPOINT_WEIGHTS=()
declare -a ENDPOINT_PATHS=()
declare -a ENDPOINT_METHODS=()
declare -a ENDPOINT_BODIES=()

TOTAL_WEIGHT=0
IFS=',' read -ra ENTRIES <<< "$ENDPOINTS"
for entry in "${ENTRIES[@]}"; do
	name="${entry%%:*}"
	weight="${entry#*:}"
	ENDPOINT_NAMES+=("$name")
	ENDPOINT_WEIGHTS+=("$weight")
	TOTAL_WEIGHT=$((TOTAL_WEIGHT + weight))

	case "$name" in
		users)
			ENDPOINT_PATHS+=("/api/v1/users")
			ENDPOINT_METHODS+=("GET")
			ENDPOINT_BODIES+=("")
			;;
		orders)
			ENDPOINT_PATHS+=("/api/v1/orders")
			ENDPOINT_METHODS+=("GET")
			ENDPOINT_BODIES+=("")
			;;
		search)
			ENDPOINT_PATHS+=("/api/v1/search?q=kubernetes")
			ENDPOINT_METHODS+=("GET")
			ENDPOINT_BODIES+=("")
			;;
		reports)
			ENDPOINT_PATHS+=("/api/v1/reports")
			ENDPOINT_METHODS+=("POST")
			ENDPOINT_BODIES+=('{"period":"daily","dimension":"revenue"}')
			;;
		*)
			echo "error: unknown endpoint '$name' (supported: users, orders, search, reports)" >&2
			exit 1
			;;
	esac
done

if [[ "$TOTAL_WEIGHT" -eq 0 ]]; then
	echo "error: total endpoint weight is zero" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Cumulative weights for random selection
# ---------------------------------------------------------------------------
declare -a CUM_WEIGHTS=()
cum=0
for w in "${ENDPOINT_WEIGHTS[@]}"; do
	cum=$((cum + w))
	CUM_WEIGHTS+=("$cum")
done

pick_endpoint() {
	local r=$((RANDOM % TOTAL_WEIGHT))
	for ((i = 0; i < ${#CUM_WEIGHTS[@]}; i++)); do
		if ((r < CUM_WEIGHTS[i])); then
			echo "$i"
			return
		fi
	done
	echo $(( ${#CUM_WEIGHTS[@]} - 1 ))
}

# ---------------------------------------------------------------------------
# CSV header
# ---------------------------------------------------------------------------
if [[ -n "$OUTPUT_FILE" ]]; then
	echo "timestamp,endpoint,latency_ms,status" > "$OUTPUT_FILE"
fi

# ---------------------------------------------------------------------------
# Shared counters (file-based for inter-process safety)
# ---------------------------------------------------------------------------
TMPDIR="$(mktemp -d /tmp/loadgen.XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

echo 0 > "$TMPDIR/req_count"
echo 0 > "$TMPDIR/err_count"
echo 0 > "$TMPDIR/latency_sum"

# ---------------------------------------------------------------------------
# Worker function
# ---------------------------------------------------------------------------
worker_loop() {
	local idx="$1"
	local per_worker_sleep

	# Calculate sleep between requests to maintain rate.
	if [[ "$RATE" -gt 0 && "$WORKERS" -gt 0 ]]; then
		per_worker_sleep="$(echo "scale=6; 1.0 / ($RATE / $WORKERS)" | bc 2>/dev/null || echo "0.1")"
	else
		per_worker_sleep="0.1"
	fi

	# Avoid division by zero / unreasonably large sleeps.
	if (( $(echo "$per_worker_sleep > 10" | bc -l 2>/dev/null || echo 0) )); then
		per_worker_sleep="10"
	fi

	local end_time=$(( $(date +%s) + DURATION ))

	while [[ $(date +%s) -lt "$end_time" ]]; do
		local start_ms
		start_ms=$(date +%s%3N)

		# Pick random endpoint based on weights.
		local ep_idx
		ep_idx=$(pick_endpoint)
		local path="${ENDPOINT_PATHS[$ep_idx]}"
		local method="${ENDPOINT_METHODS[$ep_idx]}"
		local body="${ENDPOINT_BODIES[$ep_idx]}"
		local name="${ENDPOINT_NAMES[$ep_idx]}"

		# Build curl command.
		local curl_args=(-s -o /dev/null -w '%{http_code}' --max-time 10)
		if [[ "$method" == "POST" && -n "$body" ]]; then
			curl_args+=(-X POST -H 'Content-Type: application/json' -d "$body")
		else
			curl_args+=(-X GET)
		fi
		curl_args+=("${TARGET_URL}${path}")

		# Execute.
		local status_code
		status_code="$(curl "${curl_args[@]}" 2>/dev/null)" || true

		local end_ms
		end_ms=$(date +%s%3N)
		local latency_ms=$(( end_ms - start_ms ))

		# Update counters.
		echo "$(($(cat "$TMPDIR/req_count") + 1))" > "$TMPDIR/req_count"
		if [[ "$status_code" -lt 200 || "$status_code" -ge 400 ]]; then
			echo "$(($(cat "$TMPDIR/err_count") + 1))" > "$TMPDIR/err_count"
		fi
		echo "$(($(cat "$TMPDIR/latency_sum") + latency_ms))" > "$TMPDIR/latency_sum"

		# Write CSV row.
		if [[ -n "$OUTPUT_FILE" ]]; then
			local ts
			ts="$(date --iso-8601=seconds)"
			echo "${ts},${name},${latency_ms},${status_code}" >> "$OUTPUT_FILE"
		fi

		# Rate-limit sleep.
		sleep "$per_worker_sleep"
	done
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "starting load test:" >&2
echo "  target:   ${TARGET_URL}" >&2
echo "  rate:     ${RATE} req/s" >&2
echo "  duration: ${DURATION}s" >&2
echo "  workers:  ${WORKERS}" >&2
echo "  endpoints: ${ENDPOINTS}" >&2
if [[ -n "$OUTPUT_FILE" ]]; then
	echo "  output:   ${OUTPUT_FILE}" >&2
fi
echo >&2

START_TIME=$(date +%s)

# Launch workers in background.
declare -a WORKER_PIDS=()
for ((w = 0; w < WORKERS; w++)); do
	worker_loop "$w" &
	WORKER_PIDS+=($!)
done

# Status reporting loop (every second).
while true; do
	sleep 1
	now=$(date +%s)
	elapsed=$((now - START_TIME))

	if [[ "$elapsed" -ge "$DURATION" ]]; then
		break
	fi

	req_count=$(cat "$TMPDIR/req_count" 2>/dev/null || echo 0)
	err_count=$(cat "$TMPDIR/err_count" 2>/dev/null || echo 0)
	latency_sum=$(cat "$TMPDIR/latency_sum" 2>/dev/null || echo 0)

	if [[ "$req_count" -gt 0 ]]; then
		avg_latency=$((latency_sum / req_count))
	else
		avg_latency=0
	fi

	printf "\r  %4ds elapsed | req: %-6d | err: %-4d | avg lat: %-4d ms" \
		"$elapsed" "$req_count" "$err_count" "$avg_latency" >&2
done

echo >&2
echo >&2

# Wait for workers to finish.
for pid in "${WORKER_PIDS[@]}"; do
	wait "$pid" 2>/dev/null || true
done

END_TIME=$(date +%s)
TOTAL_DURATION=$((END_TIME - START_TIME))

# Collect final counters.
TOTAL_REQS=$(cat "$TMPDIR/req_count" 2>/dev/null || echo 0)
TOTAL_ERRS=$(cat "$TMPDIR/err_count" 2>/dev/null || echo 0)

# Compute percentiles from CSV if output file was specified.
p50=0
p95=0
p99=0

if [[ -n "$OUTPUT_FILE" && -f "$OUTPUT_FILE" ]]; then
	# Extract latency column, skip header, sort numerically.
	LATENCIES=$(tail -n +2 "$OUTPUT_FILE" | cut -d',' -f3 | sort -n)
	COUNT=$(echo "$LATENCIES" | wc -l)
	if [[ "$COUNT" -gt 0 ]]; then
		# bc may be absent on nodes; fall back to line 1 (the minimum) so the
		# summary still prints instead of aborting after the CSV was written.
		p50=$(echo "$LATENCIES" | sed -n "$(printf '%.0f' "$(echo "$COUNT * 0.50" | bc -l 2>/dev/null | cut -d. -f1 || echo 1)")p" 2>/dev/null || echo 0)
		# If sed doesn't capture it, try head/tail.
		if [[ -z "$p50" || "$p50" -eq 0 ]]; then
			half=$((COUNT / 2))
			[[ "$half" -lt 1 ]] && half=1
			p50=$(echo "$LATENCIES" | head -n "$half" | tail -n 1)
		fi
		p95_line=$(printf '%.0f' "$(echo "$COUNT * 0.95" | bc -l 2>/dev/null | cut -d. -f1 || echo 1)")
		[[ "$p95_line" -lt 1 ]] && p95_line=1
		p95=$(echo "$LATENCIES" | sed -n "${p95_line}p" 2>/dev/null || echo 0)
		p99_line=$(printf '%.0f' "$(echo "$COUNT * 0.99" | bc -l 2>/dev/null | cut -d. -f1 || echo 1)")
		[[ "$p99_line" -lt 1 ]] && p99_line=1
		p99=$(echo "$LATENCIES" | sed -n "${p99_line}p" 2>/dev/null || echo 0)
	fi
fi

# Compute actual rate. bc may be absent on some nodes (e.g. the experiment
# VMs); every other bc call has a fallback, so this one degrades to 0 too
# rather than aborting the run after the CSV was already written.
if [[ "$TOTAL_DURATION" -gt 0 ]]; then
	ACTUAL_RATE=$(echo "scale=1; $TOTAL_REQS / $TOTAL_DURATION" | bc 2>/dev/null || echo "0")
else
	ACTUAL_RATE="0"
fi

# Summary output.
cat <<EOF
=== Load Test Summary ===
  target:       ${TARGET_URL}
  duration:     ${TOTAL_DURATION}s
  workers:      ${WORKERS}

  total requests: ${TOTAL_REQS}
  total errors:   ${TOTAL_ERRS}
  actual rate:    ${ACTUAL_RATE} req/s

  latency:
    p50:  ${p50} ms
    p95:  ${p95} ms
    p99:  ${p99} ms
=========================
EOF

# Exit with error if error rate > 50%.
if [[ "$TOTAL_REQS" -gt 0 ]]; then
	err_pct=$((TOTAL_ERRS * 100 / TOTAL_REQS))
	if [[ "$err_pct" -gt 50 ]]; then
		exit 1
	fi
fi
exit 0
