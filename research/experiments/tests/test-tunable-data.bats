#!/usr/bin/env bats
# test-tunable-data.bats — Tests for tunable-sweep.sh / tunable-defaults.sh
# against the documented JSON data files (research/data/tunable-sets.json,
# research/data/tunable-baseline.json).
#
# These tests assert the tunable scripts work with the new JSON data files:
#   tunable-sweep.sh list --file <sets.json> exits 0, prints set names
#   tunable-sweep.sh apply <key> --file <sets.json> --dry-run exits 0,
#          prints tunable=value lines, and performs NO ssh
#   tunable-sweep.sh apply unknown-key --file <sets.json> exits non-zero
#          and prints "not found"
#   tunable-sweep.sh restore --file <baseline.json> [--dry-run]:
#          missing baseline -> non-zero "Baseline file not found";
#          present baseline + --dry-run -> exits 0 and prints would-restore
#   tunable-defaults.sh list is safe with or without debugfs mounted
#   jq empty <file> succeeds for both JSON fixtures
#   schema contract: exactly the documented set keys; values map only
#          to {base_slice_ns, migration_cost_ns, nr_migrate} within
#          TUNABLE_RANGES (base_slice_ns 500000-50000000,
#          migration_cost_ns 0-5000000, nr_migrate 0-10000)
#
# Test strategy:
#   - Hermetic fixture files are written to $BATS_TEST_TMPDIR and passed via
#     --file, so no cluster and no real data files are needed for the core
#     assertions.
#   - Worker IP discovery (get_worker_ips from lease-common.sh) is pointed at
#     a fixture systemd lease via $SYSTEMD_LEASES / $WORKER_MACS.
#   - ssh and tofu are stubbed on PATH so the scripts never touch the real
#     network or cluster; the ssh stub records invocations so tests can prove
#     dry-run apply performs no ssh.
#   - The last tests assert the REAL files exist at
#     research/data/tunable-sets.json and research/data/tunable-baseline.json
#     and satisfy the schema. They FAIL in the red phase (data files not yet
#     created) and PASS once the implementer creates them.
#
# Run from project root:
#   bats research/experiments/tests/test-tunable-data.bats
#
# Run a specific test (filter by any substring of the test description):
#   bats --filter "apply default" research/experiments/tests/test-tunable-data.bats

setup() {
    export PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../.." && pwd -P)"
    export RESEARCH_DIR="$PROJECT_ROOT/research"
    export TUNABLE_SWEEP_SH="$RESEARCH_DIR/scripts/tunable-sweep.sh"
    export TUNABLE_DEFAULTS_SH="$RESEARCH_DIR/scripts/tunable-defaults.sh"
    export REAL_DATA_DIR="$RESEARCH_DIR/data"
    export REAL_SETS_FILE="$REAL_DATA_DIR/tunable-sets.json"
    export REAL_BASELINE_FILE="$REAL_DATA_DIR/tunable-baseline.json"

    # ---- Hermetic fixtures (fresh per test) ----
    export FIXTURE_SETS="$BATS_TEST_TMPDIR/tunable-sets.json"
    export FIXTURE_BASELINE="$BATS_TEST_TMPDIR/tunable-baseline.json"
    export FIXTURE_LEASE="$BATS_TEST_TMPDIR/lease.json"

    # Documented schema: 9 sets, kernel-7.1 tunables only, defaults
    # base_slice_ns=1400000, migration_cost_ns=500000, nr_migrate=32.
    # Every value is inside TUNABLE_RANGES.
    cat > "$FIXTURE_SETS" <<'JSON'
{
  "default": {
    "base_slice_ns": "1400000",
    "migration_cost_ns": "500000",
    "nr_migrate": "32"
  },
  "base-slice-low": {
    "base_slice_ns": "700000",
    "migration_cost_ns": "500000",
    "nr_migrate": "32"
  },
  "base-slice-high": {
    "base_slice_ns": "5000000",
    "migration_cost_ns": "500000",
    "nr_migrate": "32"
  },
  "migration-cost-zero": {
    "base_slice_ns": "1400000",
    "migration_cost_ns": "0",
    "nr_migrate": "32"
  },
  "migration-cost-high": {
    "base_slice_ns": "1400000",
    "migration_cost_ns": "3000000",
    "nr_migrate": "32"
  },
  "nr-migrate-low": {
    "base_slice_ns": "1400000",
    "migration_cost_ns": "500000",
    "nr_migrate": "8"
  },
  "nr-migrate-high": {
    "base_slice_ns": "1400000",
    "migration_cost_ns": "500000",
    "nr_migrate": "256"
  },
  "all-low": {
    "base_slice_ns": "700000",
    "migration_cost_ns": "0",
    "nr_migrate": "8"
  },
  "all-high": {
    "base_slice_ns": "5000000",
    "migration_cost_ns": "3000000",
    "nr_migrate": "256"
  }
}
JSON

    # Documented baseline schema: timestamp/captured_by/workers keyed by
    # hostname (and IP fallback) with string tunable values.
    cat > "$FIXTURE_BASELINE" <<'JSON'
{
  "timestamp": "2026-01-01T00:00:00Z",
  "captured_by": "tunable-defaults.sh",
  "workers": {
    "worker1": {
      "base_slice_ns": "1400000",
      "migration_cost_ns": "500000",
      "nr_migrate": "32"
    },
    "192.0.2.1": {
      "base_slice_ns": "1400000",
      "migration_cost_ns": "500000",
      "nr_migrate": "32"
    }
  }
}
JSON

    # systemd-networkd DHCP server lease fixture: maps the worker MAC to a
    # TEST-NET-1 IP so get_worker_ips resolves hermetically without the cluster.
    cat > "$FIXTURE_LEASE" <<'JSON'
{
  "Leases": [
    {
      "HardwareAddress": [198, 229, 80, 28, 236, 2],
      "AddressString": "192.0.2.1"
    }
  ]
}
JSON

    # Point lease discovery at the fixture (never the real host lease).
    export SYSTEMD_LEASES="$FIXTURE_LEASE"
    export DNSMASQ_LEASES="$BATS_TEST_TMPDIR/no-dnsmasq.leases"
    export WORKER_MACS="c6:e5:50:1c:ec:02"

    # ---- Command stubs so the scripts never touch the real network/cluster ----
    export STUB_BIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$STUB_BIN"
    export SSH_MARKER="$BATS_TEST_TMPDIR/ssh-calls.log"

    cat > "$STUB_BIN/ssh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SSH_MARKER:-/dev/null}"
if [[ "$*" == *"hostname"* ]]; then
    printf 'worker1\n'
    exit 0
fi
exit 1
STUB

    # check_deps requires ssh AND (tofu OR terraform) on PATH; neither is ever
    # invoked by apply/restore/list (IP discovery reads local lease files), so
    # a no-op stub keeps the tests environment-independent.
    cat > "$STUB_BIN/tofu" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

    chmod +x "$STUB_BIN/ssh" "$STUB_BIN/tofu"
    export PATH="$STUB_BIN:$PATH"
}

# =============================================================================
# list prints the documented set names
# =============================================================================

@test "list --file prints all 9 documented set names and exits 0" {
    run bash "$TUNABLE_SWEEP_SH" list --file "$FIXTURE_SETS"

    [ "$status" -eq 0 ]
    [[ "$output" == *"- default:"* ]]
    [[ "$output" == *"- base-slice-low:"* ]]
    [[ "$output" == *"- base-slice-high:"* ]]
    [[ "$output" == *"- migration-cost-zero:"* ]]
    [[ "$output" == *"- migration-cost-high:"* ]]
    [[ "$output" == *"- nr-migrate-low:"* ]]
    [[ "$output" == *"- nr-migrate-high:"* ]]
    [[ "$output" == *"- all-low:"* ]]
    [[ "$output" == *"- all-high:"* ]]
}

# =============================================================================
# apply --dry-run prints tunable=value lines, no ssh
# =============================================================================

@test "apply default --file fixture --dry-run exits 0 and prints tunable=value lines" {
    run bash "$TUNABLE_SWEEP_SH" apply default --file "$FIXTURE_SETS" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"[DRY-RUN]"* ]]
    [[ "$output" == *"base_slice_ns=1400000"* ]]
    [[ "$output" == *"migration_cost_ns=500000"* ]]
    [[ "$output" == *"nr_migrate=32"* ]]
}

@test "apply --dry-run performs no ssh (stub records zero calls)" {
    run bash "$TUNABLE_SWEEP_SH" apply default --file "$FIXTURE_SETS" --dry-run

    [ "$status" -eq 0 ]
    [ ! -f "$SSH_MARKER" ]
}

@test "every documented set applies cleanly under --dry-run" {
    local -a sets=(default base-slice-low base-slice-high migration-cost-zero migration-cost-high nr-migrate-low nr-migrate-high all-low all-high)
    local set
    for set in "${sets[@]}"; do
        run bash "$TUNABLE_SWEEP_SH" apply "$set" --file "$FIXTURE_SETS" --dry-run
        echo "set=$set status=$status" >&2
        [ "$status" -eq 0 ]
    done
}

@test "apply of an empty set fails with a clear error" {
    local empty="$BATS_TEST_TMPDIR/empty-set.json"
    printf '{ "default": {} }\n' > "$empty"

    run bash "$TUNABLE_SWEEP_SH" apply default --file "$empty" --dry-run

    [ "$status" -ne 0 ]
    [[ "$output" == *"empty"* ]]
}

# =============================================================================
# unknown key rejected
# =============================================================================

@test "apply unknown-key --file exits non-zero and prints not found" {
    run bash "$TUNABLE_SWEEP_SH" apply unknown-key --file "$FIXTURE_SETS"

    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]]
}

# =============================================================================
# restore behavior
# =============================================================================

@test "restore with missing baseline exits non-zero and prints Baseline file not found" {
    run bash "$TUNABLE_SWEEP_SH" restore --file "$BATS_TEST_TMPDIR/does-not-exist.json" --dry-run

    [ "$status" -ne 0 ]
    [[ "$output" == *"Baseline file not found"* ]]
}

@test "restore with present baseline --dry-run exits 0 and prints would-restore" {
    run bash "$TUNABLE_SWEEP_SH" restore --file "$FIXTURE_BASELINE" --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"Would restore"* ]]
    [[ "$output" == *"base_slice_ns=1400000"* ]]
}

# =============================================================================
# tunable-defaults.sh list is safe with/without debugfs
# =============================================================================

@test "tunable-defaults.sh list is safe with or without debugfs" {
    run bash "$TUNABLE_DEFAULTS_SH" list

    if [[ -d /sys/kernel/debug/sched ]]; then
        [ "$status" -eq 0 ]
        [[ "$output" == *"base_slice_ns"* ]]
    else
        [ "$status" -ne 0 ]
        [[ "$output" == *"not found"* ]]
    fi
}

# =============================================================================
# JSON fixtures are valid
# =============================================================================

@test "jq empty succeeds on the tunable-sets fixture" {
    run jq empty < "$FIXTURE_SETS"
    [ "$status" -eq 0 ]
}

@test "jq empty succeeds on the tunable-baseline fixture" {
    run jq empty < "$FIXTURE_BASELINE"
    [ "$status" -eq 0 ]
}

@test "apply with malformed JSON fixture fails gracefully" {
    local bad="$BATS_TEST_TMPDIR/malformed.json"
    printf '{ not valid json\n' > "$bad"

    run bash "$TUNABLE_SWEEP_SH" apply default --file "$bad" --dry-run

    [ "$status" -ne 0 ]
    [[ "$output" == *"parse"* ]] || [[ "$output" == *"invalid"* ]]
}

# =============================================================================
# schema contract on the fixture
# =============================================================================

@test "fixture tunable-sets.json has exactly the 9 documented keys" {
    run jq -r 'keys[]' "$FIXTURE_SETS"

    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 9 ]
    local joined
    joined="$(printf '%s\n' "${lines[@]}" | tr '\n' ' ')"
    [ "$joined" = "all-high all-low base-slice-high base-slice-low default migration-cost-high migration-cost-zero nr-migrate-high nr-migrate-low " ]
}

@test "fixture set values map only to kernel-7.1 tunables" {
    run jq -e 'all(.[] | keys[]; . == "base_slice_ns" or . == "migration_cost_ns" or . == "nr_migrate")' "$FIXTURE_SETS"
    [ "$status" -eq 0 ]
}

@test "fixture set values are within TUNABLE_RANGES" {
    run jq -e '
        all(.[];
            ((.base_slice_ns      | tonumber) >= 500000)
        and ((.base_slice_ns      | tonumber) <= 50000000)
        and ((.migration_cost_ns  | tonumber) >= 0)
        and ((.migration_cost_ns  | tonumber) <= 5000000)
        and ((.nr_migrate         | tonumber) >= 0)
        and ((.nr_migrate         | tonumber) <= 10000)
        )' "$FIXTURE_SETS"
    [ "$status" -eq 0 ]
}

@test "apply with out-of-range value fails range validation" {
    local bad="$BATS_TEST_TMPDIR/out-of-range.json"
    printf '{ "default": { "base_slice_ns": "1" } }\n' > "$bad"

    run bash "$TUNABLE_SWEEP_SH" apply default --file "$bad" --dry-run

    [ "$status" -ne 0 ]
    [[ "$output" == *"below minimum"* ]]
}

# =============================================================================
# Real data files — red phase until the implementer creates them
# =============================================================================

@test "real tunable-sets.json exists at research/data/" {
    if [[ ! -f "$REAL_SETS_FILE" ]]; then
        echo "MISSING: $REAL_SETS_FILE (implementer must create it)" >&2
        return 1
    fi
    [ -s "$REAL_SETS_FILE" ]
}

@test "real tunable-baseline.json exists at research/data/" {
    if [[ ! -f "$REAL_BASELINE_FILE" ]]; then
        echo "MISSING: $REAL_BASELINE_FILE (implementer must create it)" >&2
        return 1
    fi
    [ -s "$REAL_BASELINE_FILE" ]
}

@test "real tunable-sets.json satisfies the full schema when present" {
    if [[ ! -f "$REAL_SETS_FILE" ]]; then
        echo "MISSING: $REAL_SETS_FILE (implementer must create it before this test passes)" >&2
        return 1
    fi

    run jq empty < "$REAL_SETS_FILE"
    [ "$status" -eq 0 ]

    run jq -r 'keys[]' "$REAL_SETS_FILE"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 9 ]

    run jq -e 'all(.[] | keys[]; . == "base_slice_ns" or . == "migration_cost_ns" or . == "nr_migrate")' "$REAL_SETS_FILE"
    [ "$status" -eq 0 ]

    run jq -e '
        all(.[];
            ((.base_slice_ns      | tonumber) >= 500000)
        and ((.base_slice_ns      | tonumber) <= 50000000)
        and ((.migration_cost_ns  | tonumber) >= 0)
        and ((.migration_cost_ns  | tonumber) <= 5000000)
        and ((.nr_migrate         | tonumber) >= 0)
        and ((.nr_migrate         | tonumber) <= 10000)
        )' "$REAL_SETS_FILE"
    [ "$status" -eq 0 ]
}
