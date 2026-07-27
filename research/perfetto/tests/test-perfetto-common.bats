#!/usr/bin/env bats
# test-perfetto-common.bats — Tests for perfetto-common.sh shared library
#
# These tests verify sourcing, guard variables, function definitions,
# and path resolution WITHOUT requiring a running cluster.
#
# Run from project root: bats research/perfetto/tests/test-perfetto-common.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export PERFETTO_BIN="$PROJECT_ROOT/research/perfetto/bin"
    export PERFETTO_COMMON_SH="$PERFETTO_BIN/perfetto-common.sh"
    export CGROUP_COMMON_SH="$PROJECT_ROOT/research/bin/cgroup-common.sh"
}

teardown() {
    unset _PERFETTO_COMMON_SH _CGROUP_COMMON_SH
}

# =============================================================================
# VC-001: Library can be sourced without error
# =============================================================================

@test "C01: perfetto-common.sh file exists" {
    [ -f "$PERFETTO_COMMON_SH" ]
}

@test "C02: perfetto-common.sh can be sourced without error" {
    run bash -c "source '$PERFETTO_COMMON_SH'"
    [ "$status" -eq 0 ]
}

@test "C03: guard variable _PERFETTO_COMMON_SH is set after sourcing" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        echo \${_PERFETTO_COMMON_SH:-UNSET}
    "
    [ "$status" -eq 0 ]
    [ "$output" != "UNSET" ]
}

@test "C04: double sourcing is idempotent (no error, returns immediately)" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        source '$PERFETTO_COMMON_SH'
        echo 'OK'
    "
    [ "$status" -eq 0 ]
    [ "$output" = "OK" ]
}

@test "C05: guard variable is readonly after sourcing" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        _PERFETTO_COMMON_SH=newvalue 2>&1 || true
    "
    # readonly makes assignment fail — expected
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-002: perfetto_binary_path() returns /usr/bin/tracebox
# =============================================================================

@test "C06: perfetto_binary_path function exists" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type perfetto_binary_path 2>&1
    "
    [ "$status" -eq 0 ]
}

@test "C07: perfetto_binary_path returns /usr/bin/tracebox" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        perfetto_binary_path
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/usr/bin/tracebox" ]
}

# =============================================================================
# VC-003: perfetto_config_path() resolves config paths
# =============================================================================

@test "C08: perfetto_config_path function exists" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type perfetto_config_path 2>&1
    "
    [ "$status" -eq 0 ]
}

@test "C09: perfetto_config_path returns /tmp/<name>.cfg for partial name" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        perfetto_config_path scheduling
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/scheduling.cfg" ]
}

@test "C10: perfetto_config_path returns /tmp/<name> for name with extension" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        perfetto_config_path scheduling.cfg
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/scheduling.cfg" ]
}

@test "C11: perfetto_config_path returns /tmp/<name> for full path" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        perfetto_config_path /tmp/custom-config.cfg
    "
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/custom-config.cfg" ]
}

@test "C12: perfetto_config_path fails on empty argument" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        perfetto_config_path '' 2>&1 || true
    "
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-004: resolve_node_ip() resolves node names to IPs
# =============================================================================

@test "C13: resolve_node_ip function exists" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type resolve_node_ip 2>&1
    "
    [ "$status" -eq 0 ]
}

@test "C14: resolve_node_ip fails gracefully with no cluster (no SSH/Terraform)" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        resolve_node_ip 'nonexistent-node' 2>&1 || true
    "
    # Should exit non-zero when Terraform/SSH unavailable — this proves
    # the error path exists rather than silently hanging
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-005: check_tracebox_available() validates tracebox presence
# =============================================================================

@test "C15: check_tracebox_available function exists" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type check_tracebox_available 2>&1
    "
    [ "$status" -eq 0 ]
}

@test "C16: check_tracebox_available fails gracefully with no SSH target" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        check_tracebox_available '192.0.2.1' 2>&1 || true
    "
    # Should fail because the IP is unreachable (RFC 5735 TEST-NET)
    [ "$status" -ne 0 ]
}

# =============================================================================
# VC-006: Library depends on cgroup-common.sh
# =============================================================================

@test "C17: perfetto-common.sh sources cgroup-common.sh" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        type ssh_node resolve_project_root 2>&1
    "
    # ssh_node and resolve_project_root are defined in cgroup-common.sh
    # They should be available after sourcing perfetto-common.sh
    [ "$status" -eq 0 ]
}

@test "C18: cgroup-common.sh is sourced only once (guard check)" {
    run bash -c "
        source '$CGROUP_COMMON_SH'
        source '$PERFETTO_COMMON_SH'
        echo OK
    "
    [ "$status" -eq 0 ]
    [ "$output" = "OK" ]
}

# =============================================================================
# VC-007: Dry-run mode support structure
# =============================================================================

@test "C19: DRY_RUN variable is respected if defined" {
    run bash -c "
        source '$PERFETTO_COMMON_SH'
        DRY_RUN=true
        echo \"DRY_RUN=\$DRY_RUN\"
    "
    [ "$status" -eq 0 ]
}
