#!/usr/bin/env bash
#
# check.sh — the OTel campaign's gate.
#
# Verifies, and fails loudly on any miss:
#   1. every session in sessions.json answers a Pyroscope query with exactly its
#      own labels and time range, and returns samples;
#   2. Alloy is healthy and its profile counters moved the right way — the
#      counter advanced, the failing-session counter did not;
#   3. the profile volume is a host-owned filesystem with the 10 GiB budget,
#      Pyroscope retains 336h, and no session took stored bytes past the 9 GiB
#      stop threshold.
#
# Run it as `make -C research linux-sched-otel-check` after the campaign.

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LINUX_SCHED_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
# shellcheck source=../experiments/lib.sh
source "${LINUX_SCHED_DIR}/experiments/lib.sh"

PROFILE_VOLUME="${PROFILE_VOLUME:?set PROFILE_VOLUME to the host-owned profile directory}"
PYROSCOPE_URL="${PYROSCOPE_URL:-http://127.0.0.1:4040}"
PYROSCOPE_CONTAINER="${PYROSCOPE_CONTAINER:-linux-sched-pyroscope}"
PROFILE_TYPE="${OTEL_PROFILE_TYPE:-process_cpu:cpu:nanoseconds:cpu:nanoseconds}"
RETENTION="${OTEL_RETENTION:-336h}"
# The image is exactly 10 GiB; the filesystem on it reports less because ext4
# spends inode tables, journal and superblock. The gate checks usable capacity
# against that documented allowance and the stop threshold against real bytes.
BUDGET_BYTES="${OTEL_BUDGET_BYTES:-10200547328}"   # 9.5 GiB usable floor
STOP_BYTES="${OTEL_STOP_BYTES:-9663676416}"        # 9 GiB
# Session windows are recorded on this host, but profiles are timestamped by the
# *node's* clock, and the two are only loosely synchronised — the lab guests ran
# ~100 s behind. Querying the session's exact window then looks after its own
# profiles and finds nothing. Pad both ends: this is a clock-tolerance bound, not
# a claim about when the session ran.
SKEW_MARGIN_S="${OTEL_SKEW_MARGIN_S:-600}"
EXPECTED_SESSIONS="${OTEL_EXPECTED_SESSIONS:-15}"

# Same versioned root the measured campaign uses: a protocol revision must not
# silently read another protocol's frozen rate or write beside its records.
LINUX_SCHED_DATA_DIR="${LINUX_SCHED_DATA_DIR:-linux-sched}"
DATA_ROOT="${K8LABS_RESEARCH_DATA_ROOT:+${K8LABS_RESEARCH_DATA_ROOT}/${LINUX_SCHED_DATA_DIR}}"
DATA_ROOT="${DATA_ROOT:-$(project_root)/build/research/${LINUX_SCHED_DATA_DIR}}"
RECORD="${DATA_ROOT}/sessions.json"

failures=0
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$(( failures + 1 )); }
pass() { printf 'ok:   %s\n' "$*"; }

[[ -s "$RECORD" ]] || { echo "FATAL: no sessions.json at ${RECORD}; run the campaign first" >&2; exit 1; }

# 1. Every session answers an exact-label, exact-time-range query.
total_sessions="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$RECORD")"
if (( total_sessions != EXPECTED_SESSIONS )); then
    fail "sessions.json holds ${total_sessions} session(s), expected ${EXPECTED_SESSIONS}"
else
    pass "sessions.json holds ${total_sessions} sessions"
fi

while IFS=$'\t' read -r workload scheduler run_id start end run_id_check; do
    [[ -n "$workload" ]] || continue
    # Widen by the skew margin before querying.
    start="$(date -u -d "${start} - ${SKEW_MARGIN_S} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '%s' "$start")"
    end="$(date -u -d "${end} + ${SKEW_MARGIN_S} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '%s' "$end")"
    query="{service_name=\"linux-sched-profile\",workload=\"${workload}\",scheduler=\"${scheduler}\",run_id=\"${run_id}\"}"
    out="$(docker exec "$PYROSCOPE_CONTAINER" /usr/bin/profilecli query top \
        --url "$PYROSCOPE_URL" --profile-type "$PROFILE_TYPE" --query "$query" \
        --from "$start" --to "$end" --label-names scheduler --output json 2>&1)" || {
        fail "${workload}/${scheduler}: query failed: $(printf '%s' "$out" | tail -1)"
        continue
    }
    total="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except Exception:
    print(0); raise SystemExit
series = payload.get("series") or []
print(max((s.get("total") or 0) for s in series) if series else 0)
' 2>/dev/null || echo 0)"
    if [[ "${total:-0}" =~ ^[0-9]+$ ]] && (( total > 0 )); then
        pass "${workload}/${scheduler}: query returned ${total} samples"
    else
        fail "${workload}/${scheduler}: query returned no nonzero samples"
    fi
done < <(python3 - "$RECORD" <<'PY'
import json, sys
for session in json.load(open(sys.argv[1])):
    print("\t".join([
        session["workload"], session["scheduler"], session["run_id"],
        session["start_utc"], session["end_utc"], session["run_id"],
    ]))
PY
)

# 2. Alloy health and profile counters, read live.
pod="$(kubectl_wl -n linux-sched get pods -l app.kubernetes.io/name=linux-sched-otel-alloy \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$pod" ]]; then
    fail "no Alloy pod is running"
else
    port="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
    kubectl_wl -n linux-sched port-forward "pod/${pod}" "${port}:12345" >/dev/null 2>&1 &
    forward=$!
    deadline=$(( SECONDS + 60 ))
    healthy=""
    while (( SECONDS < deadline )); do
        healthy="$(curl -sS -m 3 "http://127.0.0.1:${port}/-/healthy" 2>/dev/null || true)"
        [[ -n "$healthy" ]] && break
        sleep 1
    done
    if [[ "$healthy" == *"All Alloy components are healthy."* ]]; then
        pass "Alloy reports all components healthy"
    else
        fail "Alloy health: ${healthy:-<no response>}"
    fi
    metrics="$(curl -sS -m 5 "http://127.0.0.1:${port}/metrics" 2>/dev/null || true)"
    kill "$forward" 2>/dev/null || true
    wait "$forward" 2>/dev/null || true
    sent="$(printf '%s' "$metrics" | awk '/^pyroscope_ebpf_pprofs_total/ {print $2; exit}')"
    failed="$(printf '%s' "$metrics" | awk '/^pyroscope_ebpf_profiling_sessions_failing_total/ {print $2; exit}')"
    if [[ "${sent:-0}" =~ ^[0-9]+$ ]] && (( sent > 0 )); then
        pass "profiler has collected ${sent} profile(s)"
    else
        fail "profiler has collected no profiles (pyroscope_ebpf_pprofs_total=${sent:-0})"
    fi
    if [[ "${failed:-0}" == "0" ]]; then
        pass "no failing profiling sessions"
    else
        fail "profiling sessions failed: ${failed}"
    fi
fi

# 3. Volume, retention and the stop threshold.
if [[ -d "$PROFILE_VOLUME" ]]; then
    avail="$(df -B1 --output=avail "$PROFILE_VOLUME" | tail -1 | tr -d '[:space:]')"
    used="$(du -sb "$PROFILE_VOLUME" 2>/dev/null | awk '{print $1}')"
    if [[ "${avail:-0}" =~ ^[0-9]+$ ]] && (( avail >= BUDGET_BYTES )); then
        pass "profile volume has ${avail} B usable (10 GiB image, 9.5 GiB floor)"
    else
        fail "profile volume has ${avail:-0} B usable, below the ${BUDGET_BYTES} B floor"
    fi
    if (( used <= STOP_BYTES )); then
        pass "stored profiles are ${used} B, within the 9 GiB stop threshold"
    else
        fail "stored profiles are ${used} B, past the 9 GiB stop threshold"
    fi
else
    fail "no profile volume at ${PROFILE_VOLUME}"
fi

if docker inspect "$PYROSCOPE_CONTAINER" --format '{{range .Config.Cmd}}{{.}} {{end}}' 2>/dev/null \
    | grep -q -- "-retention-period=${RETENTION}"; then
    pass "Pyroscope retains ${RETENTION}"
else
    fail "Pyroscope is not running with -retention-period=${RETENTION}"
fi

over="$(python3 - "$RECORD" "$STOP_BYTES" <<'PY'
import json, sys
stop = int(sys.argv[2])
over = [s["run_id"] for s in json.load(open(sys.argv[1])) if int(s.get("profile_bytes_after") or 0) > stop]
print(len(over))
PY
)"
if [[ "$over" == "0" ]]; then
    pass "no session recorded stored bytes past the stop threshold"
else
    fail "${over} session(s) recorded stored bytes past the stop threshold"
fi

if (( failures > 0 )); then
    printf '\n%d check(s) failed\n' "$failures" >&2
    exit 1
fi
printf '\nall OTel campaign checks passed\n'
