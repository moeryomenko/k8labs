#!/usr/bin/env bats
# test-eevdf-wiring.bats — Tests for --eevdf flag in run-experiment.sh
#
# These tests encode the target behavior of wiring EEVDF scheduler metric
# collection — eevdf-observe.sh JSON snapshots and cgroup-pid-watch.sh per-task
# time series — into the experiment runner behind a new --eevdf flag.
# They are written test-first: the flag-parsing and artifact-naming tests FAIL
# (red phase) against the current runner (which rejects --eevdf as an unknown
# option), while the backward-compat and graceful-degradation tests are
# regression guards that already pass and must stay green after the flag
# lands.
#
# No running cluster is required — every assertion targets --dry-run
# stdout/stderr, exit codes, or the tooling's catchable-failure contract.
#
# Pod names pinned by the fixture manifests:
#   single-pod  throttling-baseline.yaml -> stress-ng
#   multi-pod   co-located.yaml          -> latency-sensitive, batch-burner
#
# Covered behaviors:
#   --eevdf flag parses and the dry-run plan advertises EEVDF collection
#   without --eevdf the default path never claims EEVDF collection
#   per-cell EEVDF artifact names appear in the dry-run plan (per pod)
#   --eevdf works for single-pod AND multi-pod configs with per-pod naming
#   graceful degradation: eevdf-observe.sh fails catchably without a cluster
#   unknown/misspelled --eevdf forms are rejected
#
# Run from project root:
#   bats research/experiments/tests/test-eevdf-wiring.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "EEVDF collection steps" research/experiments/tests/test-eevdf-wiring.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export EXPERIMENTS_DIR="$PROJECT_ROOT/research/experiments"
    export RUN_EXPERIMENT_SH="$EXPERIMENTS_DIR/run-experiment.sh"
    export COMMON_SH="$EXPERIMENTS_DIR/common.sh"
    export BASELINE_CONFIG="$EXPERIMENTS_DIR/configs/throttling-baseline.yaml"
    export CO_LOCATED_CONFIG="$EXPERIMENTS_DIR/configs/co-located.yaml"
    export EEVDF_BIN_DIR="$PROJECT_ROOT/research/bin"
    export EEVDF_OBSERVE_SH="$EEVDF_BIN_DIR/eevdf-observe.sh"

    # Sanity checks on runner and pre-existing configs
    [ -f "$RUN_EXPERIMENT_SH" ] || { echo "FATAL: runner not found at $RUN_EXPERIMENT_SH" >&2; exit 1; }
    [ -f "$BASELINE_CONFIG" ] || { echo "FATAL: throttling-baseline.yaml not found" >&2; exit 1; }
    [ -f "$CO_LOCATED_CONFIG" ] || { echo "FATAL: co-located.yaml not found" >&2; exit 1; }
}

# =============================================================================
# --eevdf flag parses; --dry-run --eevdf succeeds and the
# output mentions EEVDF collection steps (per pod).
# =============================================================================

@test "--eevdf flag is accepted with --dry-run" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    # RED PHASE: this exits 1 (Unknown option: --eevdf) until the flag lands
    [ "$status" -eq 0 ]
}

@test "--dry-run --eevdf output mentions EEVDF collection steps" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # The plan must advertise the EEVDF collection step
    # (e.g. "eevdf-observe" or "EEVDF")
    printf '%s\n' "$output" | grep -qiE 'eevdf(-observe)?'
}

@test "--help output mentions --eevdf flag" {
    run bash "$RUN_EXPERIMENT_SH" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"--eevdf"* ]]
}

# =============================================================================
# Backward compatibility — without --eevdf the dry-run
# output does NOT claim EEVDF collection. Regression guards: they pass today
# and must stay green after the flag lands.
# =============================================================================

@test "single-pod dry-run without --eevdf succeeds and never mentions EEVDF" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
    # Zero EEVDF claims in the default path
    local mentions
    mentions="$(printf '%s' "$output" | grep -ci 'eevdf' 2>/dev/null || true)"
    [ "$mentions" -eq 0 ]
}

@test "multi-pod dry-run without --eevdf succeeds and never mentions EEVDF" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Detected co-located experiment configuration"* ]]
    [[ "$output" == *"DRY RUN MODE"* ]]
    [[ "$output" == *"Matrix cells:"* ]]
    [[ "$output" == *"Prerequisites check passed"* ]]
    local mentions
    mentions="$(printf '%s' "$output" | grep -ci 'eevdf' 2>/dev/null || true)"
    [ "$mentions" -eq 0 ]
}

# =============================================================================
# Per-cell EEVDF artifacts are named in the dry-run plan —
# eevdf-<pod>.json snapshots and/or eevdf-<pod>-pids.csv time series in the
# cell dir. Per-pod naming is the invariant; which artifact types are printed
# is an either/or per the "e.g." wording.
# =============================================================================

@test "single-pod dry-run names the per-cell EEVDF artifact for stress-ng" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # Snapshot eevdf-stress-ng.json and/or time series
    # eevdf-stress-ng-pids.csv must be advertised for the cell
    printf '%s\n' "$output" | grep -qE 'eevdf-stress-ng(-pids)?\.(json|csv)'
}

@test "multi-pod dry-run names a per-cell EEVDF artifact for each pod" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # Both co-located pods must get their own artifact name
    printf '%s\n' "$output" | grep -qE 'eevdf-latency-sensitive(-pids)?\.(json|csv)'
    printf '%s\n' "$output" | grep -qE 'eevdf-batch-burner(-pids)?\.(json|csv)'
}

@test "dry-run with --eevdf references EEVDF artifacts in cell metadata" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # The plan records where the artifacts land (cell metadata.json)
    printf '%s\n' "$output" | grep -qi 'metadata'
    printf '%s\n' "$output" | grep -qi 'eevdf'
}

# =============================================================================
# --eevdf works for a single-pod config AND a multi-pod
# config, with per-pod EEVDF naming in both cases.
# =============================================================================

@test "--eevdf per-pod collection is advertised for the single pod (stress-ng)" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # The single pod is the subject of an EEVDF collection step
    printf '%s\n' "$output" | grep -qi 'eevdf'
    printf '%s\n' "$output" | grep -q 'stress-ng'
}

@test "--eevdf per-pod collection is advertised for BOTH co-located pods" {
    run bash "$RUN_EXPERIMENT_SH" "$CO_LOCATED_CONFIG" --dry-run --eevdf

    [ "$status" -eq 0 ]
    # One EEVDF stream per pod, all pods covered
    local streams=0
    for pod in latency-sensitive batch-burner; do
        if printf '%s\n' "$output" | grep "$pod" | grep -qiE 'eevdf'; then
            streams=$((streams + 1))
        fi
    done
    [ "$streams" -eq 2 ]
}

# =============================================================================
# Graceful degradation — EEVDF collection failures are
# non-fatal (cell continues, warning logged).
#
# Without a live cluster the runner-side warn-and-continue branch cannot be
# driven to failure, so this section pins the two strongest achievable
# contracts:
#   (green): the tool's failure mode is a catchable return code, not
#            a hang or a hard process abort — the runner can guard it
#            with `|| log WARNING`.
#   (red):   a guarded code path exists — common.sh (which the runner
#            sources) exposes at least one eevdf availability/guard
#            function. Location pinned to common.sh by this contract.
# =============================================================================

@test "eevdf-observe.sh fails non-fatally (catchable exit code, no hang) without a cluster" {
    # Force an unreachable cluster so the tool's failure path is deterministic
    # regardless of the host environment (mise may set KUBECONFIG).
    run timeout 30 env KUBECONFIG=/nonexistent-kubeconfig \
        bash "$EEVDF_OBSERVE_SH" nonexistent-pod

    # Failure must be a catchable non-zero exit (124 = timeout/hang -> fail)
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ]
    # The tool reports the failure instead of dying silently
    [[ "$output" == *"rror"* || "$output" == *"Missing"* || "$output" == *"annot reach"* ]]
}

@test "common.sh exposes an EEVDF guard function (guarded code path exists)" {
    # RED PHASE: common.sh currently defines no eevdf-related function. It must
    # add an availability guard (mirroring check_tracebox_available in
    # perfetto-common.sh / check_sched_debug_available in eevdf-common.sh).
    run bash -c "
        source '$COMMON_SH'
        declare -F | grep -ci eevdf || true
    "
    [ "$status" -eq 0 ]
    [ "$output" -ge 1 ]
}

# =============================================================================
# Unknown option handling — --eevdf=foo and misspelled
# flags still error. Regression guards: they pass today (verified) and must
# stay green (the --eevdf case must stay exact/boolean).
# =============================================================================

@test "--eevdf=foo is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdf=foo

    [ "$status" -ne 0 ]
    [[ "$output" == *"--eevdf"* ]]
    [[ "$output" == *"Unknown"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}

@test "misspelled --eevdfd is rejected" {
    run bash "$RUN_EXPERIMENT_SH" "$BASELINE_CONFIG" --dry-run --eevdfd

    [ "$status" -ne 0 ]
    [[ "$output" == *"--eevdfd"* ]]
    [[ "$output" == *"Unknown"* || "$output" == *"nvalid"* || "$output" == *"rror"* ]]
}

# =============================================================================
# resolve_container_pids
# must enumerate the CONTAINER CGROUP MEMBERSHIP, not /proc/<init>/task/
#
# Empirical gate failure (real cluster): stress-ng --cpu 2 forks
# worker PROCESSES (not threads), so /proc/<init>/task/ on the node returns
# ONLY the init tid. The previous implementation enumerated exactly that
# listing, so dist-analyze.py attributed 100% of the pod's slices to `system`
# (unmatched tid -> system). The container's cgroup scope is the
# correct source: cgroup.procs lists the thread group leaders in the cgroup
# (init + forked worker processes) and cgroup.threads lists every TID
# (leaders + worker threads). A correct implementation returns the UNION of
# cgroup.procs + cgroup.threads TIDs, deduplicated.
#
# Harness (no cluster, no real ssh — same fake-ssh pattern as
# test-cgroup-hierarchy.bats):
#   - A fake `ssh` binary on PATH serves a virtual node whose
#     /proc/<pid>/task/ dirs reflect REAL kernel behavior: forked worker
#     processes are NOT listed in the init process's task/ dir (they have
#     their own /proc/<pid>/ dirs), so a task/-based enumeration cannot see
#     them (this is the exact empirical failure).
#   - The virtual node serves the container cgroup scope (cgroup.procs +
#     cgroup.threads files) plus /proc/<pid>/cgroup so an implementation can
#     derive the scope path from the init pid (get_cgroup_path) or via
#     get_container_cgroup_path; logs every invocation; refuses non-read-only
#     commands.
#   - Fake `kubectl` / `tofu` binaries + a DHCP-lease fixture resolve the
#     host-side chain (get_pod_node_ip -> kubectl -o wide, tofu nodes output,
#     get_node_ip lease lookup), so resolve_container_pids runs through its
#     REAL dependency chain, including the real get_container_pid and the real
#     cgroup-path functions (regression guards).
#
# Pinned contract:
#   - Return: one whitespace-separated list of numeric TIDs on stdout — the
#     UNION of cgroup.procs + cgroup.threads entries read from the container
#     cgroup scope, deduplicated. The /proc/<init>/task/ listing is NOT the
#     source: it lacks forked worker processes.
#   - Multi-container pod: union of every container's membership, deduplicated.
#   - Graceful degradation: when a container's cgroup membership files are
#     empty or unreadable, the container still contributes its init PID and
#     the call succeeds (exit 0).
#   - Unchanged: get_container_pid, get_cgroup_path, pod_name_to_cgroup_path,
#     get_container_cgroup_path, and the all-fail / per-container-skip
#     failure contracts.
#
# RED phase (current code enumerates /proc/<init>/task/):
#   the new enumeration tests FAIL (5); the pre-existing
#   membership/regression tests are green guards the fix must keep green.
#
# Run: bats --filter "cgroup membership" research/experiments/tests/test-eevdf-wiring.bats
# =============================================================================

# ---------------------------------------------------------------------------
# setup_fakes — Build the offline fake node + host-side command fakes.
# Call at the top of every test in this section. Self-contained per test (Bats runs
# each @test in a fresh subprocess, so PATH/exports never leak).
# ---------------------------------------------------------------------------
setup_fakes() {
    export EEVDF_COMMON_SH="$PROJECT_ROOT/research/bin/eevdf-common.sh"
    export NODE_IP="192.0.2.10"                 # TEST-NET-1, unroutable by design
    export FAKE_BIN="$BATS_TEST_TMPDIR/fakebin"
    export FAKE_NODE_ROOT="$BATS_TEST_TMPDIR/vnode"
    export FAKE_CRICTL_ROOT="$FAKE_NODE_ROOT/crictl"
    export FAKE_KUBECTL_FIXTURE="$BATS_TEST_TMPDIR/kubectl-fixture.sh"
    export FAKE_SSH_LOG="$BATS_TEST_TMPDIR/ssh-calls.log"
    export STDOUT_FILE="$BATS_TEST_TMPDIR/out.txt"
    export STDERR_FILE="$BATS_TEST_TMPDIR/err.txt"
    export KUBECONFIG="$BATS_TEST_TMPDIR/kubeconfig"
    export SYSTEMD_LEASES="$BATS_TEST_TMPDIR/leases.json"
    export DNSMASQ_LEASES="$BATS_TEST_TMPDIR/leases-dnsmasq"
    : > "$FAKE_SSH_LOG"
    : > "$STDOUT_FILE"
    : > "$STDERR_FILE"

    # --- host-side fakes: kubectl / tofu (get_pod_node_ip chain) ------------
    mkdir -p "$FAKE_BIN"
    cat > "$FAKE_KUBECTL_FIXTURE" <<'KFIX'
declare -A K_POD_NODE=( [stress-ng]=w1 [web]=w1 [web-overlap]=w1 [degraded]=w1 [single]=w1 [partial]=w1 [ghost]=w1 [allbad]=w1 )
declare -A K_POD_UID=( [stress-ng]=uid-stress [web]=uid-web [web-overlap]=uid-web-ovl [degraded]=uid-degraded [single]=uid-single [partial]=uid-partial [ghost]=uid-ghost [allbad]=uid-allbad )
declare -A K_POD_CONTAINERS=( [stress-ng]="stress-ng" [web]="app sidecar" [web-overlap]="app2 sidecar2" [degraded]="broken" [single]="solo" [partial]="ok bad" [ghost]="" [allbad]="bad1 bad2" )
declare -A K_CID=( [stress-ng]=cid-stress [app]=cid-app [sidecar]=cid-sidecar [app2]=cid-app2 [sidecar2]=cid-sidecar2 [broken]=cid-broken [solo]=cid-solo [ok]=cid-ok [bad]=cid-bad [bad1]=cid-bad1 [bad2]=cid-bad2 )
KFIX

    cat > "$FAKE_BIN/kubectl" <<'FAKEKUBECTL'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${FAKE_KUBECTL_FIXTURE:?FAKE_KUBECTL_FIXTURE required}"
source "$FAKE_KUBECTL_FIXTURE"
pod=""
o_mode=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kubeconfig) shift 2 ;;
        --kubeconfig=*) shift ;;
        get|pod) shift ;;
        --no-headers) shift ;;
        -o) o_mode="$2"; shift 2 ;;
        *) [[ -z "$pod" ]] && pod="$1"; shift ;;
    esac
done
[[ -n "$o_mode" ]] || { echo "kubectl: missing -o mode" >&2; exit 1; }
if [[ "$o_mode" == "wide" ]]; then
    printf '%s 1/1 Running 0 17s 10.0.10.5 %s <none> <none>\n' "${K_POD_NODE[$pod]:-w1}" "${K_POD_NODE[$pod]:-w1}"
    exit 0
fi
if [[ "$o_mode" != jsonpath=* ]]; then
    echo "kubectl: unsupported -o mode: $o_mode" >&2; exit 1
fi
jp="${o_mode#jsonpath=}"
if [[ "$jp" == *"spec.containers"* ]]; then
    printf '%s\n' "${K_POD_CONTAINERS[$pod]:-}"
elif [[ "$jp" == *"metadata.uid"* ]]; then
    printf '%s\n' "${K_POD_UID[$pod]:-}"
elif [[ "$jp" == *"containerStatuses"* ]]; then
    name="${jp#*name==\"}"
    name="${name%%\"*}"
    printf '%s\n' "cri-o://${K_CID[$name]:-}"
else
    echo "kubectl: unknown jsonpath: $jp" >&2; exit 1
fi
FAKEKUBECTL
    chmod +x "$FAKE_BIN/kubectl"

    cat > "$FAKE_BIN/tofu" <<'FAKETOFU'
#!/usr/bin/env bash
echo '[{"name":"w1","mac":"c6:e5:50:1c:ec:02"},{"name":"w2","mac":"c6:e5:50:1c:ec:03"}]'
FAKETOFU
    chmod +x "$FAKE_BIN/tofu"

    cat > "$SYSTEMD_LEASES" <<'LEASES'
{"Leases":[{"HardwareAddress":[198,229,80,28,236,2],"AddressString":"192.0.2.10"}]}
LEASES
    : > "$DNSMASQ_LEASES"

    # --- virtual node: crictl init pid per container id ----------------------
    # cid-bad / cid-bad1 / cid-bad2 intentionally have NO pid file, so
    # get_container_pid fails for those containers (THR-10 / THR-11).
    mkdir -p "$FAKE_CRICTL_ROOT"
    mk_crictl() { mkdir -p "$FAKE_CRICTL_ROOT/$1"; printf '%s\n' "$2" > "$FAKE_CRICTL_ROOT/$1/pid"; }
    mk_crictl cid-stress 1000
    mk_crictl cid-app 2000
    mk_crictl cid-sidecar 3000
    mk_crictl cid-app2 2100
    mk_crictl cid-sidecar2 3100
    mk_crictl cid-broken 4000
    mk_crictl cid-solo 5000
    mk_crictl cid-ok 6000

    # --- virtual node: /proc/<pid>/task/ (REAL kernel semantics) --------------
    # Each init pid directory holds the init tid + its OWN THREADS only.
    # Forked worker PROCESSES (stress-ng --cpu N) are separate /proc/<pid>/
    # dirs and do NOT appear in the init's task/ listing — the empirical
    # failure of the task/-based enumeration.
    # 4000 (degraded/broken) intentionally has NO /proc/4000/task.
    mk_proc() { local pid="$1"; shift; local t; for t in "$pid" "$@"; do mkdir -p "$FAKE_NODE_ROOT/proc/$pid/task/$t"; done; }
    mk_proc 1000                    # stress-ng: workers 1001/1002 are FORKED processes (absent here)
    mk_proc 2000 2001               # web/app: init + 1 real thread of the same process
    mk_proc 3000                    # web/sidecar: single-threaded
    mk_proc 2100                    # web-overlap/app2: worker 2200 forked (absent here)
    mk_proc 3100                    # web-overlap/sidecar2: forked workers absent
    mk_proc 5000                    # single/solo: single-threaded
    mk_proc 6000                    # partial/ok: worker 6001 forked (absent here)

    # --- virtual node: container cgroup scope (THE enumeration source) -------
    # cgroup.procs lists the thread group leaders in the cgroup (init pid +
    # forked worker PROCESSES); cgroup.threads lists every TID (leaders +
    # worker threads, and any additional threads). /proc/<pid>/cgroup lets an
    # implementation derive the scope path from the init pid (get_cgroup_path).
    # Fabricated divergences are documented: web-overlap sidecar2 shares tid
    # 2200 with app2 (dedup pin, impossible on a real node) and carries
    # procs-only tid 3101 (union-of-both-files pin).
    mk_cg_scope() {
        local uid="$1" cid="$2" pid="$3" procs="$4" threads="$5"
        local d="$FAKE_NODE_ROOT/sys/fs/cgroup/kubepods.slice/kubepods-pod${uid}.slice/crio-${cid}.scope"
        mkdir -p "$d" "$FAKE_NODE_ROOT/proc/$pid"
        printf '0::/kubepods.slice/kubepods-pod%s.slice/crio-%s.scope\n' "$uid" "$cid" > "$FAKE_NODE_ROOT/proc/$pid/cgroup"
        if [[ -n "$procs" ]]; then printf '%s\n' "$procs" | tr ' ' '\n' > "$d/cgroup.procs"; fi
        if [[ -n "$threads" ]]; then printf '%s\n' "$threads" | tr ' ' '\n' > "$d/cgroup.threads"; fi
    }
    # stress-ng: init + 2 forked worker PROCESSES in procs; threads adds 1010
    mk_cg_scope uid-stress cid-stress 1000 "1000 1001 1002" "1000 1001 1002 1010"
    # web: app has a real thread (2001) of the same process; sidecar single
    mk_cg_scope uid-web cid-app 2000 "2000" "2000 2001"
    mk_cg_scope uid-web cid-sidecar 3000 "3000" "3000"
    # web-overlap: app2's forked worker 2200; sidecar2 shares 2200 (dedup pin)
    # and lists procs-only 3101 (union pin); sidecar2 threads = init only
    mk_cg_scope uid-web-ovl cid-app2 2100 "2100 2200" "2100 2200"
    mk_cg_scope uid-web-ovl cid-sidecar2 3100 "3100 2200 3101" "3100"
    mk_cg_scope uid-degraded cid-broken 4000 "" ""   # scope exists, files empty -> THR-05
    mk_cg_scope uid-single cid-solo 5000 "5000" "5000"
    mk_cg_scope uid-partial cid-ok 6000 "6000 6001" "6000 6001"

    # --- fake ssh: serves the virtual node -----------------------------------
    cat > "$FAKE_BIN/ssh" <<'FAKESSH'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${FAKE_NODE_ROOT:?FAKE_NODE_ROOT required}"
: "${FAKE_SSH_LOG:?FAKE_SSH_LOG required}"
MODE="${FAKE_SSH_MODE:-serve}"
printf 'INVOKE %s\n' "$*" >> "$FAKE_SSH_LOG"
if [[ "$MODE" == "refuse" ]]; then
    printf 'ssh: connect to host %s port 22: Connection refused\n' "${FAKE_SSH_HOST:-unknown}" >&2
    exit 255
fi
host=""
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o)   shift 2 ;;
        -o*)  shift ;;
        -*|'') shift ;;
        *)
            if [[ "$1" == *@* ]]; then host="$1"; shift; args=("$@"); break; fi
            shift ;;
    esac
done
[[ -n "$host" ]] || { printf 'REFUSED no host\n' >> "$FAKE_SSH_LOG"; exit 2; }
cmdstr="${args[*]}"
cmdstr="${cmdstr//\/sys\/fs\/cgroup/$FAKE_NODE_ROOT/sys/fs/cgroup}"
cmdstr="${cmdstr//\/proc\//$FAKE_NODE_ROOT/proc/}"
cmdstr="${cmdstr//\/usr\/bin\/crictl/crictl}"
cmdstr="${cmdstr//\/usr\/bin\/python3/python3}"
printf 'CMD %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"
# crictl inspect is read-only by nature; served by the fake crictl on PATH
if [[ "$cmdstr" == *"crictl"* ]]; then
    bash -c "$cmdstr"
    exit $?
fi
# read-only enforcement (mirrors test-cgroup-hierarchy.bats)
if printf '%s' "$cmdstr" | grep -qE '>[^/12&]' \
   || printf '%s' "$cmdstr" | grep -qE '(^|[^A-Za-z])(tee|touch|mkdir|rm|mv|cp|dd|truncate|chmod|chown|install|mknod)([^A-Za-z]|$)' \
   || printf '%s' "$cmdstr" | grep -qE '/(etc|usr|var|home|root|opt|boot|s?bin)/'; then
    printf 'REFUSED %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"
    printf 'fake-ssh: refusing non-read-only command\n' >&2
    exit 1
fi
first="${cmdstr%% *}"
case "$first" in
    cat|ls|find|test|grep|'['|hostname) ;;
    *)
        printf 'REFUSED %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"
        printf 'fake-ssh: refusing unsupported command: %s\n' "$first" >&2
        exit 1 ;;
esac
bash -c "$cmdstr"
FAKESSH
    chmod +x "$FAKE_BIN/ssh"

    # --- fake crictl: serves the container init pid --------------------------
    cat > "$FAKE_BIN/crictl" <<'FAKECRICTL'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${FAKE_CRICTL_ROOT:?FAKE_CRICTL_ROOT required}"
[[ $# -ge 2 ]] || { echo "crictl: missing container id" >&2; exit 1; }
id="$2"
pid_file="$FAKE_CRICTL_ROOT/$id/pid"
if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    printf '{"status":{},"info":{"pid":%s,"runtimeSpec":{"linux":{}}}}\n' "$pid"
else
    printf 'crictl: container %s not found\n' "$id" >&2
    exit 1
fi
FAKECRICTL
    chmod +x "$FAKE_BIN/crictl"

    export PATH="$FAKE_BIN:$PATH"
}

# ---------------------------------------------------------------------------
# run_resolve <pod> — Invoke resolve_container_pids with stdout/stderr split.
# stdout (the pure tid list) lands in $STDOUT_FILE, stderr in $STDERR_FILE.
# Sets $status via bats `run`.
# ---------------------------------------------------------------------------
run_resolve() {
    local pod="$1"
    export POD="$pod"
    run bash -c '
        source "$EEVDF_COMMON_SH"
        resolve_container_pids "$POD" >"$STDOUT_FILE" 2>"$STDERR_FILE"
    '
}

# ---------------------------------------------------------------------------
# assert_tid_set <expected tids...> — The output contains EXACTLY the expected
# numeric tids (order-insensitive, whitespace-tolerant).
# ---------------------------------------------------------------------------
assert_tid_set() {
    local expected="$1"
    local actual exp act
    actual="$(cat "$STDOUT_FILE")"
    exp="$(printf '%s\n' $expected | sort -n | paste -sd' ' -)"
    act="$(printf '%s\n' $actual | sort -n | paste -sd' ' -)"
    [ "$exp" = "$act" ]
}

# ---------------------------------------------------------------------------
# assert_tid_count <n> — The output has exactly n whitespace-separated entries.
# ---------------------------------------------------------------------------
assert_tid_count() {
    local expected="$1"
    [ "$(wc -w < "$STDOUT_FILE")" -eq "$expected" ]
}

@test "single container returns the cgroup membership — init PID, forked worker PROCESSES and thread-only TIDs" {
    setup_fakes
    run_resolve stress-ng

    [ "$status" -eq 0 ]
    # cgroup.procs = {1000,1001,1002} + cgroup.threads = {1000,1001,1002,1010}
    # = union {1000,1001,1002,1010}. /proc/1000/task/ holds only {1000}, so a
    # task/-based enumeration returns just "1000" and FAILS this test (RED).
    assert_tid_set "1000 1001 1002 1010"
}

@test "multi-container pod returns the union of all containers' thread lists" {
    setup_fakes
    run_resolve web

    [ "$status" -eq 0 ]
    assert_tid_set "2000 2001 3000"
}

@test "cgroup membership yields exactly 4 entries — init PID is not duplicated, thread-only tid present" {
    setup_fakes
    run_resolve stress-ng

    [ "$status" -eq 0 ]
    # procs {1000,1001,1002} + threads {1000,1001,1002,1010} -> 4 unique tids
    assert_tid_count 4
    [ "$(tr ' ' '\n' < "$STDOUT_FILE" | grep -cx '1000')" -eq 1 ]
    [ "$(tr ' ' '\n' < "$STDOUT_FILE" | grep -cx '1010')" -eq 1 ]
}

@test "multi-container union is deduplicated (a shared tid appears once) and includes procs-only tids" {
    setup_fakes
    run_resolve web-overlap

    [ "$status" -eq 0 ]
    # app2 procs/threads {2100,2200} + sidecar2 procs {3100,2200,3101},
    # threads {3100} -> union {2100,2200,3100,3101}: 2200 once (dedup across
    # containers), 3101 only in sidecar2's cgroup.procs (union of both files).
    assert_tid_set "2100 2200 3100 3101"
    assert_tid_count 4
}

@test "empty/unreadable cgroup membership degrades gracefully — init PID still returned, no hard failure" {
    setup_fakes
    run_resolve degraded

    [ "$status" -eq 0 ]
    # broken's scope exists but cgroup.procs/cgroup.threads are empty/missing:
    # the container still contributes its init PID 4000 (fallback).
    assert_tid_set "4000"
    assert_tid_count 1
}

@test "single-threaded container output is unchanged (regression pin)" {
    setup_fakes
    run_resolve single

    [ "$status" -eq 0 ]
    assert_tid_set "5000"
    assert_tid_count 1
}

@test "get_container_pid unchanged — still returns the single init PID via crictl" {
    setup_fakes
    run bash -c '
        source "$EEVDF_COMMON_SH"
        get_container_pid "$NODE_IP" cid-stress
    '

    [ "$status" -eq 0 ]
    [ "$output" = "1000" ]
}

@test "cgroup path functions unchanged — get_container_cgroup_path resolves the container scope" {
    setup_fakes
    run bash -c '
        source "$EEVDF_COMMON_SH"
        get_container_cgroup_path stress-ng stress-ng
    '

    [ "$status" -eq 0 ]
    [[ "$output" == */crio-cid-stress.scope ]]
}

@test "pod with no containers still fails loudly (regression pin)" {
    setup_fakes
    run_resolve ghost

    [ "$status" -ne 0 ]
    grep -q "No PIDs found" "$STDERR_FILE"
}

@test "container whose PID cannot be resolved is skipped; the surviving container contributes its cgroup membership" {
    setup_fakes
    run_resolve partial

    [ "$status" -eq 0 ]
    # ok's cgroup membership = {6000,6001} (6001 is a forked worker PROCESS
    # absent from /proc/6000/task/, so the task/-based code returns {6000}).
    assert_tid_set "6000 6001"
}

@test "all containers failing PID resolution still fails loudly (regression pin)" {
    setup_fakes
    run_resolve allbad

    [ "$status" -ne 0 ]
    grep -q "No PIDs found" "$STDERR_FILE"
}

@test "thread enumeration issues only read-only remote commands (no new node privileges)" {
    setup_fakes
    run_resolve stress-ng

    [ "$status" -eq 0 ]
    ! grep -q '^REFUSED' "$FAKE_SSH_LOG"
}

@test "fixture realism — /proc/<init>/task/ lists only the init tid while cgroup membership lists the workers" {
    setup_fakes
    # The fixture encodes REAL kernel behavior for stress-ng --cpu 2: the
    # workers are forked PROCESSES, so the init's task/ listing holds only
    # the init tid (the empirical forked-worker gate failure). If this invariant
    # regresses, the RED tests would silently stop pinning the cgroup-
    # membership contract, so it is guarded here directly.
    local task_list procs threads
    task_list="$(ls "$FAKE_NODE_ROOT/proc/1000/task/")"
    [ "$task_list" = "1000" ]
    procs="$(cat "$FAKE_NODE_ROOT/sys/fs/cgroup/kubepods.slice/kubepods-poduid-stress.slice/crio-cid-stress.scope/cgroup.procs")"
    threads="$(cat "$FAKE_NODE_ROOT/sys/fs/cgroup/kubepods.slice/kubepods-poduid-stress.slice/crio-cid-stress.scope/cgroup.threads")"
    [ "$(wc -w <<< "$procs")" -eq 3 ]       # init + 2 worker processes
    [ "$(wc -w <<< "$threads")" -eq 4 ]     # plus one thread-only tid
    grep -qx '1001' <<< "$procs"
    grep -qx '1010' <<< "$threads"
    # Derivable scope path from the init pid (get_cgroup_path route)
    grep -q '^0::/kubepods.slice/kubepods-poduid-stress.slice/crio-cid-stress.scope$' "$FAKE_NODE_ROOT/proc/1000/cgroup"
}

@test "enumeration is the union of cgroup.procs and cgroup.threads (both directions)" {
    setup_fakes
    # Direction 1: a tid listed ONLY in cgroup.threads (1010) must be returned.
    run_resolve stress-ng
    [ "$status" -eq 0 ]
    grep -qx '1010' <(tr ' ' '\n' < "$STDOUT_FILE")
    # Direction 2: a tid listed ONLY in cgroup.procs (3101, web-overlap
    # sidecar2) must be returned — reading just one file is not enough.
    run_resolve web-overlap
    [ "$status" -eq 0 ]
    grep -qx '3101' <(tr ' ' '\n' < "$STDOUT_FILE")
}

@test "get_cgroup_path unchanged — derives the container scope from /proc/<pid>/cgroup" {
    setup_fakes
    run bash -c '
        source "$EEVDF_COMMON_SH"
        get_cgroup_path "$NODE_IP" 1000
    '

    [ "$status" -eq 0 ]
    [[ "$output" == */sys/fs/cgroup/kubepods.slice/kubepods-poduid-stress.slice/crio-cid-stress.scope ]]
}
