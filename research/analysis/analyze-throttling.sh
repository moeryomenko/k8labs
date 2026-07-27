#!/usr/bin/env bash
# analyze-throttling.sh — Process experiment data and produce aggregates
# Usage: analyze-throttling.sh <experiment-data-dir> [--output-dir path]
set -Eeuo pipefail

PROG=$(basename "$0")

usage() {
  cat <<EOF
Usage: $PROG <experiment-data-dir> [--output-dir path]

Process experiment summary.csv files and produce aggregated results.

Arguments:
  <experiment-data-dir>   Directory containing experiment data (recursive search for summary.csv)
  --output-dir PATH       Output directory for results (default: ./analysis-output)

Output:
  aggregates.csv  — Per-config aggregates
  summary.json    — Full summary as JSON
EOF
  exit 0
}

# --- Logging ---
log_info()  { echo "[$(date -u +%FT%TZ)] INFO:  $*" >&2; }
log_error() { echo "[$(date -u +%FT%TZ)] ERROR: $*" >&2; }
die()       { log_error "$*"; exit 1; }

# --- Argument parsing ---
DATA_DIR=""
OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    *) DATA_DIR="$1"; shift ;;
  esac
done

[[ -z "$DATA_DIR" ]] && die "Missing <experiment-data-dir>. Use --help for usage."
[[ ! -d "$DATA_DIR" ]] && die "Directory not found: $DATA_DIR"

[[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="./analysis-output"
mkdir -p "$OUTPUT_DIR"

# --- Verify jq is available ---
command -v jq >/dev/null 2>&1 || die "Required tool 'jq' not found. Install with: apt install jq"

# --- Find summary.csv files ---
mapfile -t CSV_FILES < <(find "$DATA_DIR" -name 'summary.csv' -type f 2>/dev/null || true)

if [[ ${#CSV_FILES[@]} -eq 0 ]]; then
  echo "No experiment data found. Run experiments first with 'make experiment-*'"
  exit 0
fi

log_info "Found ${#CSV_FILES[@]} summary.csv files"

# --- Process each summary.csv ---
AGGREGATES="$OUTPUT_DIR/aggregates.csv"
SUMMARY="$OUTPUT_DIR/summary.json"

# Write aggregates header
echo "experiment,config_cell,replicates,mean_nr_periods,mean_nr_throttled,mean_throttled_usec,mean_usage_usec,mean_throttling_ratio,mean_throttled_time_ratio" > "$AGGREGATES"

JSON_ENTRIES=()

for csv in "${CSV_FILES[@]}"; do
  log_info "Processing: $csv"

  # Determine experiment name from directory structure
  EXPERIMENT=$(basename "$(dirname "$(dirname "$csv")")")

  # Read CSV header to understand columns
  HEADER=$(head -1 "$csv")

  # Count lines (excluding header)
  TOTAL_LINES=$(wc -l < "$csv")
  DATA_LINES=$((TOTAL_LINES - 1))
  [[ $DATA_LINES -lt 1 ]] && continue

  # Process data with awk (skip header)
  awk -F',' -v experiment="$EXPERIMENT" -v OFS=',' '
  NR == 1 {
    # Find column indices from header
    for (i=1; i<=NF; i++) {
      if ($i == "config_cell") cell_idx = i
      if ($i == "nr_periods") periods_idx = i
      if ($i == "nr_throttled") throttled_idx = i
      if ($i == "throttled_usec") usec_idx = i
      if ($i == "usage_usec") usage_idx = i
    }
    next
  }
  {
    cell = $cell_idx
    periods = $periods_idx + 0
    throttled = $throttled_idx + 0
    usec = $usec_idx + 0
    usage = $usage_idx + 0

    sum_periods[cell] += periods
    sum_throttled[cell] += throttled
    sum_usec[cell] += usec
    sum_usage[cell] += usage
    count[cell]++
  }
  END {
    for (cell in count) {
      c = count[cell]
      mean_periods = sum_periods[cell] / c
      mean_throttled = sum_throttled[cell] / c
      mean_usec = sum_usec[cell] / c
      mean_usage = sum_usage[cell] / c
      ratio = (mean_periods > 0) ? mean_throttled / mean_periods : 0
      time_ratio = (mean_periods > 0 && mean_periods * 100000 > 0) ? mean_usec / (mean_periods * 100000) : 0
      printf "%s,%s,%d,%.2f,%.2f,%.2f,%.2f,%.6f,%.6f\n",
        experiment, cell, c, mean_periods, mean_throttled, mean_usec, mean_usage, ratio, time_ratio
    }
  }' "$csv" >> "$AGGREGATES"

  # Build JSON summary entry
  while IFS= read -r line; do
    JSON_ENTRIES+=("$line")
  done < <(awk -F',' -v experiment="$EXPERIMENT" '
  NR == 1 {
    for (i=1; i<=NF; i++) {
      if ($i == "config_cell") cell_idx = i
      if ($i == "nr_periods") periods_idx = i
      if ($i == "nr_throttled") throttled_idx = i
      if ($i == "throttled_usec") usec_idx = i
      if ($i == "usage_usec") usage_idx = i
    }
    next
  }
  {
    printf "{\"experiment\":\"%s\",\"config_cell\":\"%s\",\"nr_periods\":%s,\"nr_throttled\":%s,\"throttled_usec\":%s,\"usage_usec\":%s}\n",
      experiment, $cell_idx, $periods_idx, $throttled_idx, $usec_idx, $usage_idx
  }' "$csv")
done

# Write JSON summary
echo "[" > "$SUMMARY"
for ((i=0; i<${#JSON_ENTRIES[@]}; i++)); do
  echo "${JSON_ENTRIES[$i]}" >> "$SUMMARY"
  if [[ $i -lt $((${#JSON_ENTRIES[@]} - 1)) ]]; then
    echo "," >> "$SUMMARY"
  fi
done
echo "]" >> "$SUMMARY"

log_info "Aggregates written to: $AGGREGATES"
log_info "Summary JSON written to: $SUMMARY"

# Print quick summary
echo ""
echo "=== Analysis Summary ==="
echo "Experiments processed: $(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)"
echo "Config cells: $(tail -n +2 "$AGGREGATES" | wc -l)"
echo "Output: $OUTPUT_DIR/"
