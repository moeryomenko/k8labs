#!/bin/sh
# shellcheck shell=sh
# verify-docs.sh — verify README.md and AGENTS.md match the image-baked
# sysext/confext node-provisioning pipeline.
#
# Checks (one PASS/FAIL line each):
#   1. no rsync/synchronize node-deployment claim (stale)
#   2. no "dropped into /var/lib/extensions/" claim without Packer-baked
#      context (stale)
#   3. no Ansible bootstrap instructions: `make bootstrap`, `ansible-playbook`,
#      or Ansible described as the node-config mechanism (stale)
#   4. no `make certs` / `make deploy-extensions` references (stale)
#   5. `make configure` (phase-B target) is mentioned (present)
#   6. sysext/confext architecture is described (present)
#   7. AGENTS.md "Version pins" section exists and lists a Cilium version pin
#      and conmon as a baked prerequisite (present)
#
# The stale-claim checks (1-4) tolerate lines that are migration
# history or that negate the stale tool's role (e.g. "rsync is not baked",
# "no Ansible anywhere in the pipeline"), matching the spec's pass condition:
# "grep -ri ansible README.md AGENTS.md returns only migration history or
# nothing."
#
# Exits 0 only if ALL checks pass. The repo root is resolved from the script
# path (<root>/scripts/verify/verify-docs.sh), so the script can be invoked
# from any working directory.

set -eu

for _cmd in awk grep; do
    command -v "${_cmd}" >/dev/null 2>&1 || {
        printf 'FAIL: required tool not found: %s\n' "${_cmd}" >&2
        exit 1
    }
done

# Resolve the repo root from this script's location.
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(dirname -- "${SCRIPT_DIR}")
ROOT=$(dirname -- "${ROOT}")

README="${ROOT}/README.md"
AGENTS="${ROOT}/AGENTS.md"

fail_count=0

# Lines matching HISTORY_ALLOWED are treated as migration history or as
# negating the stale tool's role and are permitted by the stale-claim checks.
# The "without" marker is tool-specific ("without Ansible", "without rsync")
# so unrelated prose like "without re-baking the VM image" is not exempted.
HISTORY_ALLOWED='[Hh]istor|[Pp]revious|[Ff]ormer|[Rr]eplac|[Rr]emov|[Ll]egacy|[Ss]upersed|[Rr]etired|[Mm]igration|[Ss]tale|[Nn]o [Ll]onger|[Nn]ot [Bb]aked|[Nn]ot [Uu]sed|[Ww]ithout [Aa]nsible|[Ww]ithout [Rr]sync|[Ww]ithout [Bb]ootstrap|[Nn]o [Rr]sync|[Nn]o [Aa]nsible'

result() {
    _st=$1
    _label=$2
    _detail=${3:-}
    if [ "${_st}" = "PASS" ]; then
        printf 'PASS: %s\n' "${_label}"
    else
        printf 'FAIL: %s\n' "${_label}"
        fail_count=$((fail_count + 1))
    fi
    if [ -n "${_detail}" ]; then
        printf '%s\n' "${_detail}"
    fi
}

# append_chunk CHUNK
# Appends CHUNK to the global accumulator `out`, inserting a newline
# separator when `out` is already non-empty (command substitution strips
# trailing newlines, so raw concatenation would glue chunks together).
append_chunk() {
    if [ -n "${1}" ]; then
        if [ -n "${out}" ]; then
            out="${out}
${1}"
        else
            out="${1}"
        fi
    fi
}

# collect_offending FILE NEEDLE ALLOWED
# Prints FILE:LINE: <content> for every line of FILE matching NEEDLE
# (extended regex) but not ALLOWED (extended regex; empty disables the
# filter).
collect_offending() {
    _file=$1
    _needle=$2
    _allowed=$3
    awk -v n="${_needle}" -v a="${_allowed}" '
        $0 ~ n && (a == "" || $0 !~ a) {
            printf "%s:%d: %s\n", FILENAME, FNR, $0
        }
    ' "${_file}"
}

check_1() {
    out=""
    _chunk=$(collect_offending "${README}" '[Rr][Ss][Yy][Nn][Cc]|[Ss][Yy][Nn][Cc][Hh]roniz' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" '[Rr][Ss][Yy][Nn][Cc]|[Ss][Yy][Nn][Cc][Hh]roniz' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    if [ -z "${out}" ]; then
        result PASS "check 1 — no rsync/synchronize node-deployment claim"
    else
        result FAIL "check 1 — rsync/synchronize node-deployment claim found:" "${out}"
    fi
}

check_2() {
    # The old README wording delivers extensions by "dropped into
    # /var/lib/extensions/". Allow a same-line Packer/bake context that ties
    # the delivery to image baking (e.g. "baked into ... by Packer"); bare
    # "re-baking the VM image" does not qualify.
    _needle='[Dd]ropped? into.*var/lib/extensions|var/lib/extensions.*[Dd]ropped? into'
    _allowed="${HISTORY_ALLOWED}|[Pp]acker|[Bb]ak(e|ed|ing) (at|into)|[Ii]mage[- ]?bak"
    out=""
    _chunk=$(collect_offending "${README}" "${_needle}" "${_allowed}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" "${_needle}" "${_allowed}")
    append_chunk "${_chunk}"
    if [ -z "${out}" ]; then
        result PASS "check 2 — no /var/lib/extensions/ drop claim without image-baked context"
    else
        result FAIL "check 2 — 'dropped into /var/lib/extensions/' without image-baked context found:" "${out}"
    fi
}

check_3() {
    out=""
    _chunk=$(collect_offending "${README}" '[Mm]ake bootstrap' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" '[Mm]ake bootstrap' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${README}" '[Aa]nsible-playbook' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" '[Aa]nsible-playbook' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${README}" '[Aa]nsible' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" '[Aa]nsible' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    if [ -z "${out}" ]; then
        result PASS "check 3 — no Ansible bootstrap instructions (make bootstrap / ansible-playbook / Ansible as node-config mechanism)"
    else
        result FAIL "check 3 — Ansible bootstrap instructions found:" "${out}"
    fi
}

check_4() {
    out=""
    _chunk=$(collect_offending "${README}" '[Mm]ake certs|[Mm]ake deploy-extensions' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    _chunk=$(collect_offending "${AGENTS}" '[Mm]ake certs|[Mm]ake deploy-extensions' "${HISTORY_ALLOWED}")
    append_chunk "${_chunk}"
    if [ -z "${out}" ]; then
        result PASS "check 4 — no make certs / make deploy-extensions references"
    else
        result FAIL "check 4 — make certs / make deploy-extensions references found:" "${out}"
    fi
}

check_5() {
    if grep -qiF 'make configure' "${README}" "${AGENTS}"; then
        _detail=$(grep -inF 'make configure' "${README}" "${AGENTS}")
        result PASS "check 5 — docs mention make configure (phase-B target)" "${_detail}"
    else
        result FAIL "check 5 — make configure not mentioned in README.md/AGENTS.md"
    fi
}

check_6() {
    if grep -qiE 'systemd-sysext|systemd-confext|system extension' "${README}" "${AGENTS}"; then
        _detail=$(grep -inE 'systemd-sysext|systemd-confext|system extension' "${README}" "${AGENTS}")
        result PASS "check 6 — sysext/confext architecture described (systemd-sysext / systemd-confext / system extension)" "${_detail}"
    else
        result FAIL "check 6 — sysext/confext architecture not described"
    fi
}

check_7() {
    if ! grep -qiE '^## .*[Vv]ersion [Pp]ins' "${AGENTS}"; then
        result FAIL "check 7 — AGENTS.md has no 'Version pins' section (needed for Cilium pin and conmon baked prerequisite)"
        return
    fi
    _pins=$(awk '
        /^## / {
            if (inpins) exit
            if ($0 ~ /[Vv]ersion [Pp]ins/) { inpins = 1; print; next }
        }
        inpins { print }
    ' "${AGENTS}")
    _b=ok
    _c=ok
    _missing_pins=""
    if printf '%s\n' "${_pins}" | grep -qi 'cilium'; then
        :
    else
        _b=fail
        _missing_pins="  missing: Cilium version pin in the Version pins section"
    fi
    if printf '%s\n' "${_pins}" | grep -qiE 'conmon.*[Bb]ak|[Bb]ak.*conmon'; then
        :
    else
        _c=fail
        _missing_pins="${_missing_pins}
  missing: conmon listed as a baked prerequisite in the Version pins section"
    fi
    if [ "${_b}" = "ok" ] && [ "${_c}" = "ok" ]; then
        result PASS "check 7 — AGENTS.md Version pins section lists Cilium pin and conmon baked prerequisite" "${_pins}"
    else
        result FAIL "check 7 — AGENTS.md Version pins section incomplete:" "${_missing_pins}"
    fi
}

# Pre-flight: both documentation files must exist.
_missing=""
for _f in "${README}" "${AGENTS}"; do
    if [ ! -f "${_f}" ]; then
        _missing="${_missing}
  ${_f}"
    fi
done
if [ -n "${_missing}" ]; then
    printf 'FAIL: documentation file(s) missing:%s\n' "${_missing}"
    for _n in 1 2 3 4 5 6 7; do
        printf 'FAIL: check %s — cannot verify (documentation file missing)\n' "${_n}"
    done
    exit 1
fi

check_1
check_2
check_3
check_4
check_5
check_6
check_7

if [ "${fail_count}" -eq 0 ]; then
    printf 'RESULT: all 7 checks passed\n'
    exit 0
fi
printf 'RESULT: %s of 7 checks failed\n' "${fail_count}"
exit 1
