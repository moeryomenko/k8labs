#!/usr/bin/env bats
# test-lease-discovery.bats — Tests for research/cpu-sched/bin/lease-common.sh
# (worker IP discovery from the authoritative systemd-networkd DHCP JSON lease,
# plus the WORKER_MACS derivation from a tfvars file)
#
# WORKER_MACS semantics under test: when the variable is unset or empty,
# sourcing lease-common.sh derives the list from a tfvars file by running
# scripts/nodes.py --worker-macs (via the root venv), honoring the
# WORKER_MACS_TFVARS override for the tfvars path; on any derivation failure it
# falls back to the historical two-MAC default. An exported non-empty
# WORKER_MACS always wins and no derivation runs. The derivation tests are red
# against the current tree (the hardcoded default in lease-common.sh ignores
# WORKER_MACS_TFVARS) — the implementing task makes them green.
#
# The suite is cluster-free — fixtures live in tests/fixtures/ and
# $BATS_TEST_TMPDIR, no live lease files are read (every call passes
# --lease-file or sets SYSTEMD_LEASES/DNSMASQ_LEASES), and no SSH is involved.
#
# Context (verified 2026-08-03):
#   - The authoritative DHCP lease is the systemd-networkd JSON file
#     /var/lib/systemd/network/dhcp-server-lease/k8sbr0 with schema
#     {"Leases": [{"HardwareAddress": [6 ints], "AddressString": "IP", ...}]}.
#   - The dnsmasq lease /var/lib/misc/dnsmasq/k8sbr0.leases is STALE (Jul 31);
#     dnsmasq is DNS-only now. A second worker w2 (MAC c6:e5:50:1c:ec:03)
#     lands ONLY in the systemd JSON, so dnsmasq-only resolution would miss it.
#   - ansible/inventory/inventory.py read_systemd_leases() is the reference
#     parsing approach: lowercase ":"-joined MAC keys from HardwareAddress,
#     skip leases without exactly 6 hardware-address bytes or an empty IP.
#
# Covered behaviors:
#   read_leases_systemd parses the systemd JSON
#   read_leases_dnsmasq parses the legacy format
#   get_worker_ips resolves both worker MACs in WORKER_MACS order
#   fallback to dnsmasq when the systemd file is missing
#   unknown MACs are skipped; at least one hit is success
#   caller can source lease-common.sh and use it
#   WORKER_MACS derivation from a tfvars fixture (WORKER_MACS_TFVARS override)
#   exported WORKER_MACS overrides derivation
#   fallback to the two-MAC default when derivation fails
#   the six research files source lease-common.sh and carry no per-file
#   WORKER_MACS default
#
# Run from project root:
#   bats research/cpu-sched/experiments/tests/test-lease-discovery.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "read_leases_systemd maps" research/cpu-sched/experiments/tests/test-lease-discovery.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../.." && pwd -P)"
    export LEASE_COMMON_SH="$PROJECT_ROOT/research/cpu-sched/bin/lease-common.sh"

    # Hermetic environment: never inherit real lease paths or MAC defaults.
    unset WORKER_MACS SYSTEMD_LEASES DNSMASQ_LEASES LEASE_FILE WORKER_MACS_TFVARS

    # Fixture paths (committed fixtures under tests/fixtures/, per-test tmpdir
    # fixtures auto-cleaned by bats)
    export FIXTURE_DIR="$BATS_TEST_DIRNAME/fixtures"
    export FIXTURE_SYSTEMD="$BATS_TEST_TMPDIR/systemd-k8sbr0.json"
    export FIXTURE_DNSMASQ="$BATS_TEST_TMPDIR/dnsmasq-k8sbr0.leases"
    export ABSENT_FILE="$BATS_TEST_TMPDIR/absent-lease"

    # Derivation fixtures: a three-worker tfvars (w1/w2/w3, MACs ec:02/03/04)
    # plus its matching systemd lease JSON (w1=.26, w2=.27, w3=.29), a
    # zero-worker tfvars, and per-test missing/malformed tfvars paths.
    export FIXTURE_TFVARS_3WORKER="$FIXTURE_DIR/tfvars-three-worker.tfvars"
    export FIXTURE_SYSTEMD_3WORKER="$FIXTURE_DIR/systemd-k8sbr0-three-worker.json"
    export FIXTURE_TFVARS_NO_WORKERS="$FIXTURE_DIR/tfvars-no-workers.tfvars"
    export FIXTURE_TFVARS_MISSING="$BATS_TEST_TMPDIR/absent.tfvars"
    export FIXTURE_TFVARS_MALFORMED="$BATS_TEST_TMPDIR/malformed.tfvars"
    cat > "$FIXTURE_TFVARS_MALFORMED" <<'EOF'
control_plane = {
  name = "cp1"
  cpu  = 2
EOF

    # --- systemd-networkd DHCP lease fixture (authoritative format) ---------
    # Mirrors /var/lib/systemd/network/dhcp-server-lease/k8sbr0 (verified
    # 2026-08-03): top-level BootID/Address/PrefixLength plus a Leases[] array;
    # each lease carries HardwareAddress as a byte list and AddressString as
    # the IP. w2 (c6:e5:50:1c:ec:03, .27) is synthetic — it is the lease that
    # never lands in the stale dnsmasq file, which is exactly the gap
    # lease-common.sh closes. Byte math: 198=0xc6 229=0xe5 80=0x50 28=0x1c
    # 236=0xec. Unknown keys (ClientId, Expiration*) must be ignored.
    cat > "$FIXTURE_SYSTEMD" <<'EOF'
{
  "BootID": "d7f5041316de4f1f992a8a2822218a71",
  "Address": [192, 168, 124, 1],
  "PrefixLength": 24,
  "Leases": [
    {
      "ClientId": [1, 198, 229, 80, 28, 236, 2],
      "AddressString": "192.168.124.26",
      "Address": [192, 168, 124, 26],
      "Hostname": "w1",
      "HardwareAddressType": 1,
      "HardwareAddressLength": 6,
      "HardwareAddress": [198, 229, 80, 28, 236, 2],
      "ExpirationRealtimeUSec": 1785791504658855
    },
    {
      "ClientId": [1, 198, 229, 80, 28, 236, 3],
      "AddressString": "192.168.124.27",
      "Address": [192, 168, 124, 27],
      "Hostname": "w2",
      "HardwareAddressType": 1,
      "HardwareAddressLength": 6,
      "HardwareAddress": [198, 229, 80, 28, 236, 3],
      "ExpirationRealtimeUSec": 1785791504658855
    },
    {
      "ClientId": [1, 198, 229, 80, 28, 236, 1],
      "AddressString": "192.168.124.28",
      "Address": [192, 168, 124, 28],
      "Hostname": "cp1",
      "HardwareAddressType": 1,
      "HardwareAddressLength": 6,
      "HardwareAddress": [198, 229, 80, 28, 236, 1],
      "ExpirationRealtimeUSec": 1785791504658855
    }
  ]
}
EOF

    # --- dnsmasq lease fixture (legacy fallback format) ---------------------
    # dnsmasq line format: <expiry> <mac> <ip> <hostname> <client-id>.
    cat > "$FIXTURE_DNSMASQ" <<'EOF'
199859416800 c6:e5:50:1c:ec:01 192.168.124.28 cp1 01c6e5501cec01
199858771981 c6:e5:50:1c:ec:02 192.168.124.26 w1 01c6e5501cec02
199858771982 c6:e5:50:1c:ec:03 192.168.124.27 w2 01c6e5501cec03
EOF

    # The six files that must source lease-common.sh and carry no per-file
    # WORKER_MACS default (the default/derivation logic lives in
    # lease-common.sh only).
    FILES_6=(
        "$PROJECT_ROOT/research/cpu-sched/bin/cgroup-common.sh"
        "$PROJECT_ROOT/research/cpu-sched/experiments/common.sh"
        "$PROJECT_ROOT/research/cpu-sched/scripts/tunable-sweep.sh"
        "$PROJECT_ROOT/research/cpu-sched/scripts/tunable-defaults.sh"
        "$PROJECT_ROOT/research/cpu-sched/scripts/switch-cpu-manager.sh"
        "$PROJECT_ROOT/research/cpu-sched/scripts/verify-cpu-manager.sh"
    )
}

# Load the shared lease helper. RED until the engineer creates the file.
load_lease_common() {
    [ -f "$LEASE_COMMON_SH" ] || {
        echo "FATAL: $LEASE_COMMON_SH not found — lease-common.sh not implemented yet (expected red)" >&2
        return 1
    }
    source "$LEASE_COMMON_SH"
}

# =============================================================================
# read_leases_systemd parses the systemd JSON.
# =============================================================================

@test "read_leases_systemd maps HardwareAddress byte lists to lowercased zero-padded MAC keys" {
    load_lease_common

    run read_leases_systemd "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 3 ]
    # Exact lines pin the key format: lowercase, colon-separated, zero-padded
    # (byte 0x02 renders as "02", not "2"; 0x03 as "03").
    [ "${lines[0]}" = "c6:e5:50:1c:ec:02 192.168.124.26" ]
    [ "${lines[1]}" = "c6:e5:50:1c:ec:03 192.168.124.27" ]
    [ "${lines[2]}" = "c6:e5:50:1c:ec:01 192.168.124.28" ]
}

@test "read_leases_systemd skips leases with wrong HardwareAddress length or empty AddressString" {
    load_lease_common

    local bad="$BATS_TEST_TMPDIR/systemd-bad.json"
    cat > "$bad" <<'EOF'
{
  "Leases": [
    {"HardwareAddress": [198, 229, 80, 28, 236], "AddressString": "192.168.124.99"},
    {"HardwareAddress": [198, 229, 80, 28, 236, 2], "AddressString": ""},
    {"HardwareAddress": [198, 229, 80, 28, 236, 3], "AddressString": "192.168.124.27"}
  ]
}
EOF

    run read_leases_systemd "$bad"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ "${lines[0]}" = "c6:e5:50:1c:ec:03 192.168.124.27" ]
}

@test "read_leases_systemd on missing or malformed file returns empty with exit 0" {
    load_lease_common

    run read_leases_systemd "$ABSENT_FILE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    local malformed="$BATS_TEST_TMPDIR/systemd-malformed.json"
    printf '{ "Leases": [ ' > "$malformed"
    run read_leases_systemd "$malformed"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# =============================================================================
# read_leases_dnsmasq parses the legacy format.
# =============================================================================

@test "read_leases_dnsmasq maps <expiry mac ip hostname client-id> lease lines to MAC and IP pairs" {
    load_lease_common

    run read_leases_dnsmasq "$FIXTURE_DNSMASQ"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 3 ]
    [ "${lines[0]}" = "c6:e5:50:1c:ec:01 192.168.124.28" ]
    [ "${lines[1]}" = "c6:e5:50:1c:ec:02 192.168.124.26" ]
    [ "${lines[2]}" = "c6:e5:50:1c:ec:03 192.168.124.27" ]
}

@test "read_leases_dnsmasq normalizes MAC keys to lowercase" {
    load_lease_common

    local upper="$BATS_TEST_TMPDIR/dnsmasq-upper.leases"
    cat > "$upper" <<'EOF'
1800000000 C6:E5:50:1C:EC:02 192.168.124.26 w1 01c6e5501cec02
EOF

    run read_leases_dnsmasq "$upper"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ "${lines[0]}" = "c6:e5:50:1c:ec:02 192.168.124.26" ]
}

@test "read_leases_dnsmasq skips blank/short lines; missing file returns empty" {
    load_lease_common

    local messy="$BATS_TEST_TMPDIR/dnsmasq-messy.leases"
    cat > "$messy" <<'EOF'

1800000000 c6:e5:50:1c:ec:02 192.168.124.26
c6:e5:50:1c:ec:99
1800000000 c6:e5:50:1c:ec:03 192.168.124.27 w2 *
EOF

    run read_leases_dnsmasq "$messy"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 2 ]
    [ "${lines[0]}" = "c6:e5:50:1c:ec:02 192.168.124.26" ]
    [ "${lines[1]}" = "c6:e5:50:1c:ec:03 192.168.124.27" ]

    run read_leases_dnsmasq "$ABSENT_FILE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# =============================================================================
# get_worker_ips resolves BOTH worker MACs,
# deterministically (WORKER_MACS order), from the systemd lease.
# =============================================================================

@test "get_worker_ips returns both worker IPs from the systemd fixture in WORKER_MACS order" {
    load_lease_common
    export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

@test "get_worker_ips falls back to the two-MAC default when the tfvars file is missing" {
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_MISSING"
    load_lease_common

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

@test "get_worker_ips honors SYSTEMD_LEASES env override without --lease-file" {
    load_lease_common
    export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
    export SYSTEMD_LEASES="$FIXTURE_SYSTEMD"

    run get_worker_ips

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

# =============================================================================
# fallback to dnsmasq when the systemd file is missing;
# non-zero with a clear error when both are missing.
# =============================================================================

@test "get_worker_ips falls back to the dnsmasq lease when the systemd lease file is missing" {
    load_lease_common
    export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
    export DNSMASQ_LEASES="$FIXTURE_DNSMASQ"

    run get_worker_ips --lease-file "$ABSENT_FILE"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

@test "get_worker_ips fails with a clear error when systemd and dnsmasq leases are both missing" {
    load_lease_common
    export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
    export DNSMASQ_LEASES="$BATS_TEST_TMPDIR/absent-dnsmasq"

    run get_worker_ips --lease-file "$ABSENT_FILE"

    [ "$status" -ne 0 ]
    [[ "$output" =~ [Ll]ease ]]
}

# =============================================================================
# unknown MACs are skipped; at least one hit is success.
# =============================================================================

@test "get_worker_ips skips unknown MACs and succeeds with the known ones" {
    load_lease_common
    export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:99 c6:e5:50:1c:ec:03"

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

@test "get_node_ip fails with a clear error for an unknown MAC" {
    load_lease_common

    run get_node_ip "c6:e5:50:1c:ec:99" --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -ne 0 ]
    [[ "$output" == *"c6:e5:50:1c:ec:99"* ]]
}

@test "get_node_ip resolves a known MAC and normalizes an uppercase query to lowercase" {
    load_lease_common

    run get_node_ip "c6:e5:50:1c:ec:02" --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26" ]

    run get_node_ip "C6:E5:50:1C:EC:02" --lease-file "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26" ]
}

# =============================================================================
# a caller can source lease-common.sh and use it.
# =============================================================================

@test "caller script sources lease-common.sh and calls get_worker_ips --lease-file" {
    local caller="$BATS_TEST_TMPDIR/caller.sh"
    cat > "$caller" <<'EOF'
#!/usr/bin/env bash
source "$1"
export WORKER_MACS="c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03"
get_worker_ips --lease-file "$2"
EOF
    chmod +x "$caller"

    run bash "$caller" "$LEASE_COMMON_SH" "$FIXTURE_SYSTEMD"

    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

# =============================================================================
# WORKER_MACS derivation from a tfvars file (WORKER_MACS_TFVARS override).
#
# With WORKER_MACS unset or empty, sourcing lease-common.sh derives the list
# by running scripts/nodes.py --worker-macs against the tfvars file pointed to
# by $WORKER_MACS_TFVARS (default: <project-root>/build/deploy.tfvars). An
# exported non-empty WORKER_MACS wins and no derivation runs. Derivation
# failure (missing/malformed tfvars, parser error) falls back to the
# historical two-MAC default; a successful parse that yields no workers sets
# WORKER_MACS to the empty string (resolution then fails loudly). These tests
# are red against the current hardcoded default in lease-common.sh, which
# ignores WORKER_MACS_TFVARS.
# =============================================================================

@test "derives worker MACs from the three-worker tfvars fixture in tfvars order" {
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_3WORKER"
    load_lease_common

    [ "$WORKER_MACS" = "c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03 c6:e5:50:1c:ec:04" ]

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD_3WORKER"
    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27 192.168.124.29" ]
}

@test "exported WORKER_MACS overrides the tfvars-derived list" {
    export WORKER_MACS="c6:e5:50:1c:ec:04 c6:e5:50:1c:ec:02"
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_3WORKER"
    load_lease_common

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD_3WORKER"
    [ "$status" -eq 0 ]
    # Env order, not tfvars order: derivation would give .26 .27 .29.
    [ "$output" = "192.168.124.29 192.168.124.26" ]
}

@test "an exported-but-empty WORKER_MACS still derives from the tfvars fixture" {
    export WORKER_MACS=""
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_3WORKER"
    load_lease_common

    [ "$WORKER_MACS" = "c6:e5:50:1c:ec:02 c6:e5:50:1c:ec:03 c6:e5:50:1c:ec:04" ]

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD_3WORKER"
    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27 192.168.124.29" ]
}

@test "falls back to the two-MAC default when the tfvars file is malformed" {
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_MALFORMED"
    load_lease_common

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD"
    [ "$status" -eq 0 ]
    [ "$output" = "192.168.124.26 192.168.124.27" ]
}

@test "derives an empty MAC list from a tfvars with no workers, then fails resolution with a clear error" {
    export WORKER_MACS_TFVARS="$FIXTURE_TFVARS_NO_WORKERS"
    load_lease_common

    [ "$WORKER_MACS" = "" ]

    run get_worker_ips --lease-file "$FIXTURE_SYSTEMD_3WORKER"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no worker IPs found"* ]]
}

# =============================================================================
# the six research files source lease-common.sh and
# carry no per-file WORKER_MACS default.
#
# Source/import assertion: a non-comment line that sources lease-common.sh
# (`source ...lease-common.sh` or `. .../lease-common.sh`). The weaker
# "file defines get_worker_ips" variant is NOT used — every one of the six
# files already defines get_worker_ips today, so that assertion would be green
# in the red phase and prove nothing. No-default assertion: the file contains
# no WORKER_MACS assignment (`WORKER_MACS=` or `WORKER_MACS:=`); the only
# default/derivation logic lives in lease-common.sh.
# =============================================================================

@test "cgroup-common.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[0]}"
}

@test "experiments/common.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[1]}"
}

@test "tunable-sweep.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[2]}"
}

@test "tunable-defaults.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[3]}"
}

@test "switch-cpu-manager.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[4]}"
}

@test "verify-cpu-manager.sh sources lease-common.sh" {
    grep -qE '^[^#]*(source|\.)[[:space:]]+[^[:space:]]*lease-common\.sh' "${FILES_6[5]}"
}

@test "cgroup-common.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[0]}"
}

@test "experiments/common.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[1]}"
}

@test "tunable-sweep.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[2]}"
}

@test "tunable-defaults.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[3]}"
}

@test "switch-cpu-manager.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[4]}"
}

@test "verify-cpu-manager.sh carries no per-file WORKER_MACS default" {
    ! grep -qE 'WORKER_MACS[[:space:]:]*=' "${FILES_6[5]}"
}
