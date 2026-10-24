#!/usr/bin/env bats
# Offline contracts for CPU-limits Taskfile commands.

setup() {
    SERIES_DIR="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    TASKFILE="${SERIES_DIR}/Taskfile.yml"
}

@test "validate accepts the frozen design" {
    run task --taskfile "${TASKFILE}" cpu-limits-validate
    [ "$status" -eq 0 ]
    [[ "$output" == *"VALID: 36 cells"* ]]
}

@test "plan is deterministic and has 36 cells" {
    run task --taskfile "${TASKFILE}" cpu-limits-plan
    [ "$status" -eq 0 ]
    first="$output"

    run task --taskfile "${TASKFILE}" cpu-limits-plan
    [ "$status" -eq 0 ]
    [ "$output" = "$first" ]
    [ "$(printf '%s\n' "$first" | grep -c '^block-')" -eq 36 ]
}

@test "an unknown workload is rejected" {
    local kubeconfig
    kubeconfig="$(mktemp)"

    run task --taskfile "${TASKFILE}" cpu-limits-smoke \
        CPU_LIMITS_NODE=worker-a \
        CPU_LIMITS_COLLECTOR_URL=http://collector.invalid/metrics \
        CPU_LIMITS_COLLECTOR_POD=collector-a \
        CPU_LIMITS_KUBECONFIG="$kubeconfig" \
        CPU_LIMITS_WORKLOAD=unknown
    rm -f "$kubeconfig"

    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown workload"* ]]
}

@test "confirm refuses without frozen policies and sample size" {
    local data_root
    data_root="$(mktemp -d)"

    run env K8LABS_RESEARCH_DATA_ROOT="$data_root" \
        task --taskfile "${TASKFILE}" cpu-limits-confirm \
        CPU_LIMITS_NODE=worker-a \
        CPU_LIMITS_COLLECTOR_URL=http://collector.invalid/metrics \
        CPU_LIMITS_COLLECTOR_POD=collector-a
    rm -rf "$data_root"

    [ "$status" -ne 0 ]
    [[ "$output" == *"policies.json"* ]]
    [[ "$output" == *"sample-size.json"* ]]
}

@test "no task uses git add all" {
    run grep -F 'git add -A' "$TASKFILE"
    [ "$status" -ne 0 ]
}
