#!/usr/bin/env bats
# test-cgroup-hierarchy.bats — Tests for research/bin/cgroup-hierarchy-snapshot.sh
#
# These tests encode the target behavior of TASK-009 (a new script that dumps
# the node cgroup v2 hierarchy: kubepods.slice cpu.weight, per-QoS-slice
# cpu.weight, and per-pod-slice cpu.weight/cpu.max, read over SSH). They are
# written test-first: every test FAILS (red phase) today because the script
# does not exist yet.
#
# No live cluster and no SSH are required. SSH is stubbed by a fake `ssh`
# binary placed at the front of PATH (created in setup()): it serves a
# virtual /sys/fs/cgroup tree from $BATS_TEST_TMPDIR, logs every invocation,
# and refuses any non-read-only command. TASK-009's script must reach the
# node via an `ssh` command (the repo convention, cgroup-common.sh ssh_node)
# so the fake ssh intercepts it.
#
# Requirements covered (full mapping in TEST-DESIGN.md):
#   REQ-1 -> VC-CH-01 (CH-01, CH-02, CH-03)
#   REQ-2 -> VC-CH-02 (CH-04, CH-16, CH-17, CH-18)
#   REQ-3 -> VC-CH-03 (CH-05..CH-11)
#   REQ-4 -> VC-CH-04 (CH-12, CH-13, CH-14)
#   REQ-5 -> VC-CH-05 (CH-15)
#   REQ-6 -> VC-CH-06 (CH-20)
#
# FIX-3 additions (TRUE Guaranteed pod support):
#   REQ-1(FIX-3) -> VC-CH-G-01 (CH-G-01, CH-G-02) — direct guaranteed pod slice
#                   emitted as a slice entry with ONE self-representing pod
#                   entry (name = slice name, cpu_weight = slice cpu.weight,
#                   cpu_max = slice cpu.max), never a weight-losing entry.
#   REQ-1 compat  -> VC-CH-G-02 (CH-G-03, CH-G-04) — burstable/besteffort
#                   output byte-compatible with and without the guaranteed
#                   slice present.
#   REQ-1 read-only -> VC-CH-G-03 (CH-G-05) — additive change stays read-only.
#
# Run from project root:
#   bats research/experiments/tests/test-cgroup-hierarchy.bats
#
# Run a specific test:
#   bats --filter "CH-05" research/experiments/tests/test-cgroup-hierarchy.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export SCRIPT="$PROJECT_ROOT/research/bin/cgroup-hierarchy-snapshot.sh"
    export NODE="192.0.2.10"                       # TEST-NET-1, unroutable by design

    # Per-test artifacts
    export FAKE_BIN="$BATS_TEST_TMPDIR/fakebin"
    export FAKE_CGROUP_ROOT="$BATS_TEST_TMPDIR/fake-cgroup"
    export FAKE_SSH_LOG="$BATS_TEST_TMPDIR/ssh-calls.log"
    export STDOUT_FILE="$BATS_TEST_TMPDIR/out.json"
    export STDERR_FILE="$BATS_TEST_TMPDIR/err.log"
    : > "$FAKE_SSH_LOG"

    # --- Golden cgroup tree served by the fake ssh -------------------------
    # Deterministic fixture. Values are chosen to be distinct so JSON value
    # assertions cannot pass for the wrong cgroup.
    mkdir -p "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podabc123.slice"
    mkdir -p "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-poddef456.slice"
    mkdir -p "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod789abc.slice"

    write_cg() { printf '%s\n' "$2" > "$1"; }
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/cpu.weight" "100"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/cpu.max" "max 100000"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/cpu.weight" "46"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/cpu.max" "max 100000"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podabc123.slice/cpu.weight" "38"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podabc123.slice/cpu.max" "50000 100000"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-poddef456.slice/cpu.weight" "42"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-poddef456.slice/cpu.max" "100000 100000"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-besteffort.slice/cpu.weight" "2"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod789abc.slice/cpu.weight" "2"
    write_cg "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod789abc.slice/cpu.max" "max 100000"
    # Make the virtual node read-only: any write the future script attempts
    # would fail here (VC-CH-05 enforcement).
    chmod -R a-w "$FAKE_CGROUP_ROOT"

    # --- Fake ssh ----------------------------------------------------------
    # Contract (see TEST-DESIGN.md): invoked as
    #   ssh [-o <opt>]... "root@<ip>" <remote-command>
    # It serves any read-only command that touches /sys/fs/cgroup by
    # transparently rewriting that prefix to $FAKE_CGROUP_ROOT and executing
    # locally. Commands outside the read-only allowlist, or containing write
    # intents, are logged as REFUSED and fail. Every invocation is logged to
    # $FAKE_SSH_LOG (one INVOKE line + one CMD line). FAKE_SSH_MODE=refuse
    # makes ssh fail like "connection refused" (VC-CH-04).
    mkdir -p "$FAKE_BIN"
    cat > "$FAKE_BIN/ssh" <<'FAKESSH'
#!/usr/bin/env bash
set -Eeuo pipefail
: "${FAKE_CGROUP_ROOT:?FAKE_CGROUP_ROOT required}"
: "${FAKE_SSH_LOG:?FAKE_SSH_LOG required}"
MODE="${FAKE_SSH_MODE:-serve}"

printf 'INVOKE %s\n' "$*" >> "$FAKE_SSH_LOG"

if [[ "$MODE" == "refuse" ]]; then
    printf 'ssh: connect to host %s port 22: Connection refused\n' \
        "${FAKE_SSH_HOST:-unknown}" >&2
    exit 255
fi

# Parse: options (-o value / -oValue / -x...), then root@host, then command.
host=""
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o)   shift 2 ;;
        -o*)  shift ;;
        -*|'') shift ;;
        *)
            if [[ "$1" == *@* ]]; then
                host="$1"
                shift
                args=("$@")
                break
            fi
            shift
            ;;
    esac
done
[[ -n "$host" ]] || { printf 'REFUSED no host\n' >> "$FAKE_SSH_LOG"; exit 2; }

# Rewrite virtual cgroup paths to the fixture root.
for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == *"/sys/fs/cgroup"* ]]; then
        args[$i]="${args[$i]//\/sys\/fs\/cgroup/$FAKE_CGROUP_ROOT}"
    fi
done
cmdstr="${args[*]}"
# Trim leading whitespace so the first-word check is stable.
cmdstr="${cmdstr#"${cmdstr%%[![:space:]]*}"}"
printf 'CMD %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"

# Read-only enforcement (VC-CH-05): no redirects except stderr/stdout
# suppression (>/dev/null, >1, >2, >&1, >&2), no mutating tools.
if printf '%s' "$cmdstr" | grep -qE '>[^/12&]' \
   || printf '%s' "$cmdstr" | grep -qE '(^|[^A-Za-z])(tee|touch|mkdir|rm|mv|cp|dd|truncate|chmod|chown|install|mknod)([^A-Za-z]|$)' \
   || printf '%s' "$cmdstr" | grep -qE '/(etc|usr|var|home|root|opt|boot|s?bin)/'; then
    printf 'REFUSED %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"
    printf 'fake-ssh: refusing non-read-only command\n' >&2
    exit 1
fi
first="${cmdstr%% *}"
case "$first" in
    cat|ls|find|test|grep|'[') ;;
    *)
        printf 'REFUSED %s\n' "$cmdstr" >> "$FAKE_SSH_LOG"
        printf 'fake-ssh: refusing unsupported command: %s\n' "$first" >&2
        exit 1
        ;;
esac

bash -c "$cmdstr"
FAKESSH
    chmod +x "$FAKE_BIN/ssh"
}

teardown() {
    # Restore write permission on the read-only fixture so bats can clean up
    # $BATS_TEST_TMPDIR (chmod -R a-w in setup would otherwise block removal).
    chmod -R u+w "$FAKE_CGROUP_ROOT" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# snapshot_captured — Run the script splitting stdout/stderr into files.
# Sets SNAP_RC to the script's exit code. Use for JSON tests: `run` merges
# stdout+stderr, which would poison `jq` parsing; stdout must stay pure JSON.
# ---------------------------------------------------------------------------
snapshot_captured() {
    "$SCRIPT" "$@" >"$STDOUT_FILE" 2>"$STDERR_FILE"
    SNAP_RC=$?
    return "$SNAP_RC"
}

assert_captured_ok() {
    if [[ "$SNAP_RC" -ne 0 ]]; then
        printf 'script failed (rc=%s); stderr:\n' "$SNAP_RC" >&2
        cat "$STDERR_FILE" >&2
        return 1
    fi
    return 0
}

# =============================================================================
# VC-CH-01 (REQ-1): Script exists, is executable, and --help/-h print usage
# and exit 0.
# =============================================================================

@test "CH-01: script exists at research/bin/cgroup-hierarchy-snapshot.sh and is executable" {
    [ -f "$SCRIPT" ]
    [ -x "$SCRIPT" ]
}

@test "CH-02: --help prints usage and exits 0" {
    run "$SCRIPT" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"sage"* ]]
}

@test "CH-03: -h prints usage and exits 0" {
    run "$SCRIPT" -h

    [ "$status" -eq 0 ]
    [[ "$output" == *"sage"* ]]
}

# =============================================================================
# VC-CH-02 (REQ-2): A node is required (--node <ip> or positional <ip>).
# Missing/invalid node arguments fail with a non-zero exit and an error
# message; the positional form is accepted.
# =============================================================================

@test "CH-04: missing node argument exits non-zero with an error message" {
    run "$SCRIPT"

    [ "$status" -ne 0 ]
    [[ "$output" == *"sage"* ]] || [[ "$output" == *"node"* ]] || [[ "$output" == *"rror"* ]]
}

@test "CH-16: --node without a value exits non-zero" {
    run "$SCRIPT" --node

    [ "$status" -ne 0 ]
    [[ "$output" == *"node"* ]] || [[ "$output" == *"equire"* ]] || [[ "$output" == *"sage"* ]]
}

@test "CH-17: unknown option is rejected" {
    run "$SCRIPT" --bogus-flag

    [ "$status" -ne 0 ]
    [[ "$output" == *"nknown"* ]] || [[ "$output" == *"nrecognized"* ]] || [[ "$output" == *"nvalid"* ]] || [[ "$output" == *"sage"* ]]
}

@test "CH-18: positional <ip> is accepted and produces the same snapshot" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured "$NODE"
    assert_captured_ok

    jq -e . "$STDOUT_FILE" >/dev/null
    [ "$(jq -r '.node' "$STDOUT_FILE")" = "$NODE" ]
}

# =============================================================================
# VC-CH-03 (REQ-3): Output is valid JSON with the deterministic schema
# { node, timestamp, kubepods_slice_weight, slices[ {name, cpu_weight,
# pods[{name, cpu_weight, cpu_max}]} ] }.
# =============================================================================

@test "CH-05: --node snapshot succeeds; output is valid JSON with node and kubepods_slice_weight" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    # stdout must be pure JSON (diagnostics belong on stderr)
    jq -e . "$STDOUT_FILE" >/dev/null
    [ "$(jq -r '.node' "$STDOUT_FILE")" = "$NODE" ]
    [ "$(jq -r '.kubepods_slice_weight' "$STDOUT_FILE")" = "100" ]
}

@test "CH-06: slices array contains exactly the two QoS slices of the golden tree" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices | length' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '[.slices[].name] | sort | join(",")' "$STDOUT_FILE")" = "kubepods-besteffort.slice,kubepods-burstable.slice" ]
}

@test "CH-07: burstable slice has name, cpu_weight 46, and 2 pod entries" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .cpu_weight' "$STDOUT_FILE")" = "46" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods | length' "$STDOUT_FILE")" = "2" ]
}

@test "CH-08: besteffort slice has name, cpu_weight 2, and 1 pod entry" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .cpu_weight' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .pods | length' "$STDOUT_FILE")" = "1" ]
}

@test "CH-09: per-pod slice entries carry the fixture cpu_weight and cpu_max values" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-podabc123.slice") | .cpu_weight' "$STDOUT_FILE")" = "38" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-podabc123.slice") | .cpu_max' "$STDOUT_FILE")" = "50000 100000" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-poddef456.slice") | .cpu_weight' "$STDOUT_FILE")" = "42" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-poddef456.slice") | .cpu_max' "$STDOUT_FILE")" = "100000 100000" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .pods[] | select(.name == "kubepods-besteffort-pod789abc.slice") | .cpu_max' "$STDOUT_FILE")" = "max 100000" ]
}

@test "CH-10: every slice has name/cpu_weight/pods and every pod has name/cpu_weight/cpu_max" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    jq -e '.slices | type == "array"' "$STDOUT_FILE" >/dev/null
    jq -e '.slices | length > 0' "$STDOUT_FILE" >/dev/null
    jq -e '.slices | all(.[]; (.name|type)=="string" and ((.cpu_weight|type)=="string" or (.cpu_weight|type)=="number") and (.pods|type)=="array")' "$STDOUT_FILE" >/dev/null
    jq -e '.slices | all(.[]; .pods | all(.[]; (.name|type)=="string" and ((.cpu_weight|type)=="string" or (.cpu_weight|type)=="number") and (.cpu_max|type)=="string"))' "$STDOUT_FILE" >/dev/null
    jq -e '.node | type == "string"' "$STDOUT_FILE" >/dev/null
    jq -e '.kubepods_slice_weight | type == "string" or type == "number"' "$STDOUT_FILE" >/dev/null
}

@test "CH-11: timestamp field is present and non-empty" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    jq -e '(.timestamp | type == "string" or type == "number")' "$STDOUT_FILE" >/dev/null
    jq -e '.timestamp | tostring | length > 0' "$STDOUT_FILE" >/dev/null
}

# =============================================================================
# VC-CH-04 (REQ-4): Data is read over SSH to root@<node>. SSH failure yields
# a non-zero exit with a clear message.
# =============================================================================

@test "CH-12: snapshot reaches ssh as root@<node> and issues remote commands" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    grep -q '^CMD ' "$FAKE_SSH_LOG"
    grep -q "root@$NODE" "$FAKE_SSH_LOG"
}

@test "CH-13: ssh failure exits non-zero with a clear message" {
    export PATH="$FAKE_BIN:$PATH"
    export FAKE_SSH_MODE=refuse
    export FAKE_SSH_HOST="$NODE"

    run "$SCRIPT" --node "$NODE"

    [ "$status" -ne 0 ]
    [[ "$output" == *"ssh"* ]] || [[ "$output" == *"SSH"* ]] || [[ "$output" == *"connect"* ]] || [[ "$output" == *"fail"* ]] || [[ "$output" == *"node"* ]]
}

@test "CH-14: ssh unavailable on PATH exits non-zero (edge)" {
    mkdir -p "$BATS_TEST_TMPDIR/no-ssh"
    touch "$BATS_TEST_TMPDIR/no-ssh/ssh"
    chmod 644 "$BATS_TEST_TMPDIR/no-ssh/ssh"   # present but not executable

    run env PATH="$BATS_TEST_TMPDIR/no-ssh:$PATH" "$SCRIPT" --node "$NODE"

    [ "$status" -ne 0 ]
    [[ "$output" == *"ssh"* ]] || [[ "$output" == *"equired"* ]] || [[ "$output" == *"not found"* ]] || [[ "$output" == *"enied"* ]] || [[ "$output" == *"rror"* ]]
}

# =============================================================================
# VC-CH-05 (REQ-5): The snapshot is read-only — the fake node sees only
# read-only ssh commands and its fixture tree is byte-for-byte unchanged.
# =============================================================================

@test "CH-15: snapshot issues only read-only remote commands and leaves the node untouched" {
    export PATH="$FAKE_BIN:$PATH"

    local before after
    before="$(find "$FAKE_CGROUP_ROOT" -type f -exec sha256sum {} + | sort | sha256sum)"

    snapshot_captured --node "$NODE"
    assert_captured_ok

    after="$(find "$FAKE_CGROUP_ROOT" -type f -exec sha256sum {} + | sort | sha256sum)"
    [ "$before" = "$after" ]
    ! grep -q '^REFUSED' "$FAKE_SSH_LOG"
    ! grep -qE '^CMD .*>[^/12&]' "$FAKE_SSH_LOG"
    ! grep -qE '^CMD .*(^|[^A-Za-z])(tee|touch|mkdir|rm|mv|cp|dd|truncate|chmod|chown|install|mknod)([^A-Za-z]|$)' "$FAKE_SSH_LOG"
}

# =============================================================================
# VC-CH-06 (REQ-6): The JSON is jq-parseable; .kubepods_slice_weight and the
# slices array length are accessible via jq.
# =============================================================================

@test "CH-20: output parses with jq and exposes .kubepods_slice_weight and .slices length" {
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    jq -e '.kubepods_slice_weight and .slices' "$STDOUT_FILE" >/dev/null
    [ "$(jq -r '.kubepods_slice_weight' "$STDOUT_FILE")" = "100" ]
    [ "$(jq -r '.slices | length' "$STDOUT_FILE")" = "2" ]
}

# =============================================================================
# FIX-3: TRUE Guaranteed pod support (VC-CH-G)
#
# With the systemd cgroup driver a TRUE Guaranteed pod (memory
# requests==limits) has NO kubepods-guaranteed.slice wrapper: its pod slice
# kubepods-pod<uid>.slice sits DIRECTLY under kubepods.slice. The snapshot
# must emit such a slice as a top-level slice entry whose pods[] holds ONE
# self-representing pod entry mirroring the slice itself (name = slice name,
# cpu_weight = slice cpu.weight, cpu_max = slice cpu.max) — never a
# weight-losing empty entry. The change is additive: fixtures without a
# direct pod slice must stay byte-compatible.
#
# The fake-ssh tree is created read-only in setup(); CH-G tests extend it
# with the direct pod slices before running the snapshot.
# =============================================================================

# ---------------------------------------------------------------------------
# add_guaranteed_slice_fixture — Extend the golden tree with TRUE Guaranteed
# pod slices (systemd cgroup driver layout): kubepods-podABC.slice (weight 59,
# cpu.max 50000 100000) and kubepods-podDEF.slice (weight 10, cpu.max
# "max 100000") directly under kubepods.slice. Self-contained (does not rely
# on the setup() helper) so it works from any test body.
# ---------------------------------------------------------------------------
add_guaranteed_slice_fixture() {
    chmod -R u+w "$FAKE_CGROUP_ROOT"
    mkdir -p "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podABC.slice"
    printf '59\n' > "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podABC.slice/cpu.weight"
    printf '50000 100000\n' > "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podABC.slice/cpu.max"
    mkdir -p "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podDEF.slice"
    printf '10\n' > "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podDEF.slice/cpu.weight"
    printf 'max 100000\n' > "$FAKE_CGROUP_ROOT/kubepods.slice/kubepods-podDEF.slice/cpu.max"
    chmod -R a-w "$FAKE_CGROUP_ROOT"
}

@test "CH-G-01: direct guaranteed pod slice is emitted with ONE self-representing pod entry (VC-CH-G-01)" {
    export PATH="$FAKE_BIN:$PATH"
    add_guaranteed_slice_fixture
    snapshot_captured --node "$NODE"
    assert_captured_ok

    # Top-level slice entry for the direct pod slice (no guaranteed wrapper)
    [ "$(jq -r '.slices | length' "$STDOUT_FILE")" = "4" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podABC.slice") | .cpu_weight' "$STDOUT_FILE")" = "59" ]

    # Exactly one self-representing pod entry mirroring the slice itself:
    # name == slice name, cpu_weight == slice cpu.weight, cpu_max == slice cpu.max.
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podABC.slice") | .pods | length' "$STDOUT_FILE")" = "1" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podABC.slice") | .pods[0].name' "$STDOUT_FILE")" = "kubepods-podABC.slice" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podABC.slice") | .pods[0].cpu_weight' "$STDOUT_FILE")" = "59" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podABC.slice") | .pods[0].cpu_max' "$STDOUT_FILE")" = "50000 100000" ]
}

@test "CH-G-02: unlimited guaranteed slice mirrors cpu.max 'max 100000' (VC-CH-G-01 edge)" {
    export PATH="$FAKE_BIN:$PATH"
    add_guaranteed_slice_fixture
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices[] | select(.name == "kubepods-podDEF.slice") | .pods | length' "$STDOUT_FILE")" = "1" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podDEF.slice") | .pods[0].name' "$STDOUT_FILE")" = "kubepods-podDEF.slice" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podDEF.slice") | .pods[0].cpu_weight' "$STDOUT_FILE")" = "10" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-podDEF.slice") | .pods[0].cpu_max' "$STDOUT_FILE")" = "max 100000" ]
}

@test "CH-G-03: burstable/besteffort slices stay byte-compatible when a guaranteed slice is present (VC-CH-G-02)" {
    export PATH="$FAKE_BIN:$PATH"
    add_guaranteed_slice_fixture
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.kubepods_slice_weight' "$STDOUT_FILE")" = "100" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .cpu_weight' "$STDOUT_FILE")" = "46" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods | length' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-podabc123.slice") | .cpu_weight' "$STDOUT_FILE")" = "38" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods[] | select(.name == "kubepods-burstable-podabc123.slice") | .cpu_max' "$STDOUT_FILE")" = "50000 100000" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .cpu_weight' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .pods | length' "$STDOUT_FILE")" = "1" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .pods[0].cpu_weight' "$STDOUT_FILE")" = "2" ]
}

@test "CH-G-04: tree without a guaranteed slice still emits exactly the two QoS slices (VC-CH-G-02 byte-compat)" {
    # Regression pin for the additive FIX-3 change: with NO kubepods-pod*.slice
    # directly under kubepods.slice, the snapshot output keeps the pre-fix
    # shape (2 slices, burstable 2 pods, besteffort 1 pod).
    export PATH="$FAKE_BIN:$PATH"
    snapshot_captured --node "$NODE"
    assert_captured_ok

    [ "$(jq -r '.slices | length' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '[.slices[].name] | sort | join(",")' "$STDOUT_FILE")" = "kubepods-besteffort.slice,kubepods-burstable.slice" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-burstable.slice") | .pods | length' "$STDOUT_FILE")" = "2" ]
    [ "$(jq -r '.slices[] | select(.name == "kubepods-besteffort.slice") | .pods | length' "$STDOUT_FILE")" = "1" ]
}

@test "CH-G-05: guaranteed-slice snapshot stays read-only and leaves the node untouched (VC-CH-G-03)" {
    export PATH="$FAKE_BIN:$PATH"
    add_guaranteed_slice_fixture

    local before after
    before="$(find "$FAKE_CGROUP_ROOT" -type f -exec sha256sum {} + | sort | sha256sum)"

    snapshot_captured --node "$NODE"
    assert_captured_ok

    after="$(find "$FAKE_CGROUP_ROOT" -type f -exec sha256sum {} + | sort | sha256sum)"
    [ "$before" = "$after" ]
    ! grep -q '^REFUSED' "$FAKE_SSH_LOG"
    ! grep -qE '^CMD .*>[^/12&]' "$FAKE_SSH_LOG"
    ! grep -qE '^CMD .*(^|[^A-Za-z])(tee|touch|mkdir|rm|mv|cp|dd|truncate|chmod|chown|install|mknod)([^A-Za-z]|$)' "$FAKE_SSH_LOG"
}
