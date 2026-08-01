#!/bin/bash
# =============================================================================
# verify-network.sh — End-to-end verification for the VPN-safe cluster network
#
# Implements the TASK-001 verification contract:
#   .plans/cluster-network-vpn-safe/tasks/TASK-001/verification-contract.md
#
# Proves, by execution, that the cluster VM network (bridge k8sbr0
# 192.168.124.1/24, systemd-networkd DHCP, dnsmasq DNS, nftables NAT) can
# never disturb host connections — specifically the WireGuard full-tunnel
# VPN `wg1` — across `make network-up` / `make network-down` cycles, and
# that each of the four known defects is fixed without regressing host
# networking or VM connectivity.
#
# Phases:
#   Phase 0  prerequisites            (CHK-PRQ-01..03)
#   Phase A  baseline capture         (CHK-BSL-01..06)  VC-01
#   Phase B  damage assessment        (CHK-DMG-01..03)  VC-02, EC-01/02
#   Phase C  make network-down        (CHK-DWN-01..09)  VC-02/04/06/07
#   Phase D  make network-up          (CHK-UPP-01..10)  VC-02/03/05/06/07
#   Phase E  idempotency + durability (CHK-IDM-01..08)  VC-03, EC-03/04
#   Phase F  VM-level checks          (CHK-VM-00..04)   VC-08
#   Phase G  aggregate verdict        (CHK-SMY-01)
#
# USAGE:
#   sudo ./scripts/verify-network.sh          # run as root (recommended)
#   ./scripts/verify-network.sh               # run as user with passwordless sudo
#
# EXIT CODES:
#   0 — every applicable check PASSed (SKIPs allowed with documented reasons)
#   1 — at least one check FAILed
#   2 — harness failure (prerequisite missing, cannot proceed)
#
# Notes:
#   - The make cycle intentionally tears down and re-creates the lab network;
#     wg1 must survive the cycle (that is the point). The script restores the
#     lab to UP state before exiting, as it was found.
#   - dig is not installed on this host; DNS assertions use a python3 stdlib
#     raw UDP probe (corrected packet builder, per TASK-004 finding).
# =============================================================================

set -Eeuo pipefail
shopt -s inherit_errexit
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_ROOT"

# Snapshot files (exact names from the TASK-001 contract)
F_IPRULE_BEFORE="/tmp/verify-iprule.before"
F_NFTTABLES_BEFORE="/tmp/verify-nfttables.before"
F_SS53_BEFORE="/tmp/verify-ss53.before"
F_ROUTE_BEFORE="/tmp/verify-route.before"
F_WG1_BEFORE="/tmp/verify-wg1.before"
F_EGRESS_BEFORE="/tmp/verify-egress.before"
F_NFTTABLES_PREDOWN="/tmp/verify-nfttables.predown"
F_NFTTABLES_POSTDOWN="/tmp/verify-nfttables.postdown"
F_EGRESS_POSTDOWN="/tmp/verify-egress.postdown"
F_EGRESS_POSTUP="/tmp/verify-egress.postup"
LOG_DOWN="/tmp/verify-networkdown.log"
LOG_UP="/tmp/verify-networkup.log"
LOG_UP2="/tmp/verify-networkup2.log"

DNS_PROBE_PY='import socket, sys; name=b"".join(bytes([len(x)])+x.encode() for x in "example.com.".split(".")); p=b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"+name+b"\x00\x01\x00\x01"; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(p,(sys.argv[1],53)); d=s.recv(512); sys.exit(0 if d[:2]==b"\xab\xcd" and d[2:4]==b"\x81\x80" else 1)'

# ---------------------------------------------------------------------------
# Result accounting
# ---------------------------------------------------------------------------
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
FAIL_IDS=()
SKIP_NOTES=()

record() {
    local status="$1" id="$2" note="${3:-}"
    case "$status" in
        PASS)
            PASS_COUNT=$((PASS_COUNT + 1))
            ;;
        FAIL)
            FAIL_COUNT=$((FAIL_COUNT + 1))
            FAIL_IDS+=("$id")
            ;;
        SKIP)
            SKIP_COUNT=$((SKIP_COUNT + 1))
            SKIP_NOTES+=("$id: $note")
            ;;
        *)
            printf 'HARNESS ERROR: invalid status "%s"\n' "$status" >&2
            exit 2
            ;;
    esac
    printf '%-4s  %-11s  %s\n' "$status" "$id" "$note"
}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
WG_UP="unknown"                 # UP | DOWN  (set by CHK-DMG-01)
EGRESS_PROBE_OK=0               # 1 when baseline egress was captured
DROPIN_BEFORE=0                 # 1 when networkd drop-in existed at baseline
DOWN_CYCLE_STARTED=0            # 1 once the destructive make step has run
VM_GATE_OPEN=0                  # 1 when CHK-VM-00 opens the gate
export SSH_AGENT_SOCK=""        # discovered ssh-agent socket for VM checks (exported for ssh_vm subshell)
TOFU_BIN=""                     # discovered tofu binary for VM checks
NODES_JSON=""                   # tofu nodes output (JSON), for VM checks
VM_FIRST_IP=""                  # first node IP resolved from leases
NODES_MACS=()                   # MACs parsed from tofu nodes output

# ---------------------------------------------------------------------------
# Environment / dependency resolution
# ---------------------------------------------------------------------------
INVOKING_USER="${SUDO_USER:-$(id -un)}"

resolve_tofu() {
    local bin=""
    bin="$(command -v tofu 2>/dev/null || true)"
    if [[ -z "$bin" && -n "${SUDO_USER:-}" ]]; then
        bin="$(sudo -n -H -u "$SUDO_USER" bash -c 'command -v tofu' 2>/dev/null || true)"
    fi
    if [[ -z "$bin" ]]; then
        local uh
        uh="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
        local p
        for p in "$uh"/.local/share/mise/installs/opentofu/*/tofu; do
            if [[ -x "$p" ]]; then bin="$p"; break; fi
        done
    fi
    printf '%s' "$bin"
}

resolve_agent_socket() {
    local uh
    uh="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
    local s
    for s in "$uh"/.keychain/*.s; do
        if [[ -S "$s" ]]; then printf '%s' "$s"; return 0; fi
    done
    local line sock
    while IFS= read -r line; do
        sock="$(printf '%s\n' "$line" | grep -oE -- '-a [^ ]+' | awk '{print $2}' || true)"
        if [[ -n "$sock" && -S "$sock" ]]; then printf '%s' "$sock"; return 0; fi
    done < <(pgrep -af 'ssh-agent' 2>/dev/null || true)
    return 1
}

ssh_vm() {
    # ssh_vm <ip> <remote-command...>
    local ip="$1"
    shift
    env SSH_AUTH_SOCK="$SSH_AGENT_SOCK" \
        ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        "root@${ip}" "$@"
}

# `timeout` needs a real executable, so the ssh_vm function is exported and
# invoked through bash -c.
export -f ssh_vm

ssh_vm_timeout() {
    # ssh_vm_timeout <seconds> <ip> <remote-command...>
    local secs="$1"
    local ip="$2"
    shift 2
    timeout "$secs" bash -c 'ssh_vm "$@"' _ "$ip" "$@"
}

# ---------------------------------------------------------------------------
# Safety net: leave the system as found (lab UP at exit)
# ---------------------------------------------------------------------------
_on_exit() {
    local rc=$?
    if [[ "$DOWN_CYCLE_STARTED" == "1" ]] && ! ip route show 10.0.10.0/24 2>/dev/null | grep -q 'k8sbr0'; then
        printf '[RESTORE] lab network is DOWN at exit; running make network-up to restore as found\n' >&2
        (cd "$REPO_ROOT" && make network-up) >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap _on_exit EXIT

# ===========================================================================
# Phase 0 — Prerequisites
# ===========================================================================
chk_prq01() {
    if sudo -n true 2>/dev/null; then
        record PASS CHK-PRQ-01 "passwordless sudo available"
    else
        record FAIL CHK-PRQ-01 "sudo -n true failed — passwordless sudo required"
        printf 'FATAL: passwordless sudo is required to inspect nft/wg state and run make targets\n' >&2
        exit 2
    fi
}

chk_prq02() {
    local missing=()
    local c
    for c in ip nft ss systemctl getent curl python3 timeout grep awk sed; do
        if ! command -v "$c" >/dev/null 2>&1; then missing+=("$c"); fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        record PASS CHK-PRQ-02 "all required tools present (ip nft ss systemctl getent curl python3 timeout grep awk sed)"
    else
        record FAIL CHK-PRQ-02 "missing tools: ${missing[*]}"
    fi
}

chk_prq03() {
    if [[ -f network/nat.nft && -f network/k8sbr0.network \
          && -f network/dnsmasq-k8sbr0.conf && -f Makefile ]]; then
        record PASS CHK-PRQ-03 "repo source files present (nat.nft, k8sbr0.network, dnsmasq-k8sbr0.conf, Makefile)"
    else
        record FAIL CHK-PRQ-03 "one or more repo source files missing"
    fi
}

# ===========================================================================
# Phase A — Baseline capture (VC-01)
# ===========================================================================
chk_bsl01() {
    if ip rule show > "$F_IPRULE_BEFORE" 2>&1; then
        record PASS CHK-BSL-01 "ip rule snapshot written: $F_IPRULE_BEFORE"
    else
        record FAIL CHK-BSL-01 "cannot write ip rule snapshot"
    fi
}

chk_bsl02() {
    local out rc=0
    out="$(sudo -n nft list tables 2>&1)" || rc=$?
    printf '%s\n' "$out" > "$F_NFTTABLES_BEFORE"
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-BSL-02 "nft tables snapshot written: $F_NFTTABLES_BEFORE"
    else
        record FAIL CHK-BSL-02 "sudo nft list tables failed"
    fi
}

chk_bsl03() {
    local out
    out="$(sudo -n ss -lunp 2>&1 || true)"
    if printf '%s\n' "$out" | grep ':53 ' > "$F_SS53_BEFORE"; then
        record PASS CHK-BSL-03 "DNS listener snapshot written: $F_SS53_BEFORE"
    elif [[ -z "$out" ]]; then
        : > "$F_SS53_BEFORE"
        record PASS CHK-BSL-03 "DNS listener snapshot written (no :53 listeners found): $F_SS53_BEFORE"
    else
        record FAIL CHK-BSL-03 "ss -lunp failed"
    fi
}

chk_bsl04() {
    if ip route show > "$F_ROUTE_BEFORE" 2>&1; then
        record PASS CHK-BSL-04 "route snapshot written: $F_ROUTE_BEFORE"
    else
        record FAIL CHK-BSL-04 "cannot write route snapshot"
    fi
}

chk_bsl05() {
    local out
    if command -v wg >/dev/null 2>&1; then
        out="$(sudo -n wg show wg1 2>&1)" || true
        printf '%s\n' "$out" > "$F_WG1_BEFORE"
        record PASS CHK-BSL-05 "wg1 status snapshot written: $F_WG1_BEFORE"
    else
        record FAIL CHK-BSL-05 "wg binary missing (tool gap)"
    fi
}

chk_bsl06() {
    local rc=0
    set +e
    timeout 15 curl -sS --max-time 10 https://ifconfig.me > "$F_EGRESS_BEFORE" 2>/dev/null
    rc=$?
    set -e
    if [[ $rc -eq 0 ]] && grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' "$F_EGRESS_BEFORE"; then
        EGRESS_PROBE_OK=1
        record PASS CHK-BSL-06 "external egress captured: $(tr -d '\n' < "$F_EGRESS_BEFORE")"
    else
        EGRESS_PROBE_OK=0
        record SKIP CHK-BSL-06 "egress probe failed (external dependency); egress comparisons deferred to SKIP"
    fi
}

# ===========================================================================
# Phase B — Damage assessment (EC-01, EC-02)
# ===========================================================================
chk_dmg01() {
    if ip -br link show wg1 2>/dev/null | grep -q UP; then
        WG_UP=UP
        record PASS CHK-DMG-01 "wg1 is UP"
    else
        WG_UP=DOWN
        record PASS CHK-DMG-01 "wg1 is DOWN (VPN-survival checks will SKIP)"
    fi
}

chk_dmg02() {
    local c51820 csup
    c51820="$(ip rule show | grep -c 'lookup 51820' || true)"
    csup="$(ip rule show | grep -c 'suppress_prefixlength' || true)"
    if [[ "$WG_UP" == "UP" ]]; then
        if (( c51820 >= 1 && csup >= 1 )); then
            record PASS CHK-DMG-02 "wg-quick policy rules present (lookup 51820 x${c51820}, suppress_prefixlength x${csup})"
        else
            record FAIL CHK-DMG-02 "DAMAGED STATE: wg1 is UP but wg-quick policy rules are missing (defect 1 fired). Expected: not fwmark 0xca6c lookup 51820 + suppress_prefixlength 0. (lookup 51820 x${c51820}, suppress x${csup})"
        fi
    else
        record SKIP CHK-DMG-02 "wg1 tunnel is down; VPN rule survival cannot be verified"
    fi
}

chk_dmg03() {
    local c
    c="$(sudo -n nft list tables 2>/dev/null | grep -c 'wg-quick-wg1' || true)"
    if (( c >= 1 )); then
        record PASS CHK-DMG-03 "wg-quick nft table ip wg-quick-wg1 present"
    elif [[ "$WG_UP" == "UP" ]]; then
        record FAIL CHK-DMG-03 "wg1 is UP but ip wg-quick-wg1 table is missing"
    else
        record SKIP CHK-DMG-03 "wg1 tunnel is down; wg-quick table survival cannot be verified"
    fi
}

# ===========================================================================
# Phase C — make network-down (VC-02, VC-04, VC-06-down, VC-07, EC-05)
# ===========================================================================
chk_dwn01() {
    local out rc=0
    out="$(sudo -n nft list tables 2>&1)" || rc=$?
    printf '%s\n' "$out" | sort > "$F_NFTTABLES_PREDOWN"
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-DWN-01 "foreign table inventory before down: $F_NFTTABLES_PREDOWN"
    else
        record FAIL CHK-DWN-01 "cannot write pre-down nft inventory"
    fi
}

chk_dwn02() {
    DOWN_CYCLE_STARTED=1
    local rc=0
    set +e
    make network-down 2>&1 | tee "$LOG_DOWN"
    rc=${PIPESTATUS[0]}
    set -e
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-DWN-02 "make network-down completed (exit 0)"
    else
        record FAIL CHK-DWN-02 "make network-down failed (exit $rc); see $LOG_DOWN"
    fi
    sleep 2
}

chk_dwn03() {
    local c
    c="$(sudo -n nft list ruleset 2>/dev/null | grep -c 'k8slab' || true)"
    if (( c == 0 )); then
        record PASS CHK-DWN-03 "scoped teardown: no k8slab content in ruleset"
    else
        record FAIL CHK-DWN-03 "k8slab content still present after network-down (count=$c)"
    fi
}

chk_dwn04() {
    local ok=1
    local out rc=0
    out="$(sudo -n nft list tables 2>&1)" || rc=$?
    printf '%s\n' "$out" | sort > "$F_NFTTABLES_POSTDOWN"
    if [[ $rc -ne 0 ]]; then
        record FAIL CHK-DWN-04 "cannot read nft tables after down"
        return
    fi
    local d
    d="$(diff <(grep -vE '^(table inet k8slab|table inet nat|table inet filter)$' "$F_NFTTABLES_PREDOWN") \
              <(grep -vE '^(table inet k8slab|table inet nat|table inet filter)$' "$F_NFTTABLES_POSTDOWN") || true)"
    if [[ -n "$d" ]]; then ok=0; fi
    if [[ "$WG_UP" == "UP" ]]; then
        if ! grep -q 'wg-quick-wg1' "$F_NFTTABLES_POSTDOWN"; then ok=0; fi
    fi
    if [[ $ok -eq 1 ]]; then
        record PASS CHK-DWN-04 "foreign tables intact after down (wg-quick-wg1 present)"
    else
        record FAIL CHK-DWN-04 "foreign table set changed across network-down: $d"
    fi
}

chk_dwn05() {
    local c51820 csup
    c51820="$(ip rule show | grep -c 'lookup 51820' || true)"
    csup="$(ip rule show | grep -c 'suppress_prefixlength' || true)"
    if [[ "$WG_UP" == "UP" ]]; then
        if (( c51820 >= 1 && csup >= 1 )); then
            record PASS CHK-DWN-05 "wg-quick policy rules survive network-down (lookup 51820 x${c51820}, suppress x${csup})"
        else
            record FAIL CHK-DWN-05 "wg-quick policy rules stripped by network-down (lookup 51820 x${c51820}, suppress x${csup})"
        fi
    else
        record SKIP CHK-DWN-05 "wg1 tunnel is down; VPN rule survival cannot be verified"
    fi
}

chk_dwn06() {
    local out
    out="$(ip route show 10.0.10.0/24 || true)"
    if [[ -z "$out" ]]; then
        record PASS CHK-DWN-06 "LB route 10.0.10.0/24 absent after down"
    else
        record FAIL CHK-DWN-06 "LB route still present after down: $out"
    fi
}

chk_dwn07() {
    local out
    out="$(getent hosts example.com || true)"
    if [[ -n "$out" ]]; then
        record PASS CHK-DWN-07 "host DNS unaffected after down (example.com resolves)"
    else
        record SKIP CHK-DWN-07 "host DNS resolution failed after down (upstream DNS dependency)"
    fi
}

chk_dwn08() {
    if [[ "$EGRESS_PROBE_OK" != "1" ]]; then
        record SKIP CHK-DWN-08 "egress baseline unavailable; comparison deferred (external dependency)"
        return
    fi
    local rc=0
    set +e
    timeout 15 curl -sS --max-time 10 https://ifconfig.me > "$F_EGRESS_POSTDOWN" 2>/dev/null
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        record SKIP CHK-DWN-08 "egress probe failed after down (external dependency)"
    elif cmp -s "$F_EGRESS_BEFORE" "$F_EGRESS_POSTDOWN"; then
        record PASS CHK-DWN-08 "egress unchanged after down ($(tr -d '\n' < "$F_EGRESS_POSTDOWN"))"
    else
        record FAIL CHK-DWN-08 "egress changed after down: before=$(tr -d '\n' < "$F_EGRESS_BEFORE") after=$(tr -d '\n' < "$F_EGRESS_POSTDOWN")"
    fi
}

chk_dwn09() {
    if [[ "$DROPIN_BEFORE" != "1" ]]; then
        record SKIP CHK-DWN-09 "networkd.conf.d drop-in absent at baseline; nothing to assert"
        return
    fi
    if [[ -f /etc/systemd/networkd.conf.d/90-k8slab-foreign-rules.conf ]]; then
        record PASS CHK-DWN-09 "networkd.conf.d drop-in survives network-down"
    else
        record FAIL CHK-DWN-09 "networkd.conf.d drop-in removed by network-down"
    fi
}

# ===========================================================================
# Phase D — make network-up (VC-02, VC-03-load, VC-05, VC-06-up, VC-07, EC-05)
# ===========================================================================
chk_upp01() {
    local rc=0
    set +e
    make network-up 2>&1 | tee "$LOG_UP"
    rc=${PIPESTATUS[0]}
    set -e
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-UPP-01 "make network-up completed (exit 0)"
    else
        record FAIL CHK-UPP-01 "make network-up failed (exit $rc); see $LOG_UP"
    fi
    sleep 2
}

chk_upp02() {
    local c51820 csup
    c51820="$(ip rule show | grep -c 'lookup 51820' || true)"
    csup="$(ip rule show | grep -c 'suppress_prefixlength' || true)"
    if [[ "$WG_UP" == "UP" ]]; then
        if (( c51820 >= 1 && csup >= 1 )); then
            record PASS CHK-UPP-02 "wg-quick policy rules survive network-up (lookup 51820 x${c51820}, suppress x${csup})"
        else
            record FAIL CHK-UPP-02 "wg-quick policy rules stripped by network-up (defect 1 regression) (lookup 51820 x${c51820}, suppress x${csup})"
        fi
    else
        record SKIP CHK-UPP-02 "wg1 tunnel is down; VPN rule survival cannot be verified"
    fi
}

chk_upp03() {
    local tables
    tables="$(sudo -n nft list tables 2>/dev/null || true)"
    local ok=1
    if ! grep -q 'table inet k8slab' <<< "$tables"; then ok=0; fi
    if [[ "$WG_UP" == "UP" ]] && ! grep -q 'table ip wg-quick-wg1' <<< "$tables"; then ok=0; fi
    if [[ $ok -eq 1 ]]; then
        record PASS CHK-UPP-03 "tables after up: inet k8slab + $(if [[ "$WG_UP" == 'UP' ]]; then printf 'ip wg-quick-wg1'; fi) present"
    elif [[ "$WG_UP" == "UP" ]]; then
        record FAIL CHK-UPP-03 "tables after up missing expected table: $tables"
    else
        record FAIL CHK-UPP-03 "inet k8slab missing after up: $tables"
    fi
}

chk_upp04() {
    local t
    t="$(sudo -n nft list table inet k8slab 2>/dev/null || true)"
    local cm ch1 co
    cm="$(grep -cE 'ip saddr 192.168.124.0/24 masquerade' <<< "$t" || true)"
    ch1="$(grep -cE 'iifname "k8sbr0" accept' <<< "$t" || true)"
    co="$(grep -cE 'oifname "k8sbr0" accept' <<< "$t" || true)"
    if [[ "$cm" == "1" && "$ch1" == "1" && "$co" == "1" ]]; then
        record PASS CHK-UPP-04 "k8slab has exactly 3 rules (1 masquerade, 2 forward accepts)"
    else
        record FAIL CHK-UPP-04 "k8slab rule counts wrong (masquerade=$cm, iifname=$ch1, oifname=$co); expected 1 1 1"
    fi
}

chk_upp05() {
    local ss53
    ss53="$(sudo -n ss -lunp 2>/dev/null | grep ':53 ' || true)"
    if [[ -z "$ss53" ]]; then
        record FAIL CHK-UPP-05 "no :53 listeners found"
        return
    fi
    local has_bridge=0 has_wild4=0 has_wild6=0
    grep -q '192.168.124.1:53' <<< "$ss53" && has_bridge=1
    grep -q '0.0.0.0:53' <<< "$ss53" && has_wild4=1
    grep -q '\[::\]:53' <<< "$ss53" && has_wild6=1
    if [[ $has_bridge -eq 1 && $has_wild4 -eq 0 && $has_wild6 -eq 0 ]]; then
        record PASS CHK-UPP-05 "dnsmasq bound only to 192.168.124.1:53; no wildcard 0.0.0.0:53 or [::]:53"
    else
        record FAIL CHK-UPP-05 "dnsmasq binding wrong (bridge=$has_bridge, 0.0.0.0:53=$has_wild4, [::]:53=$has_wild6): $ss53"
    fi
}

chk_upp06() {
    local lan_ip
    lan_ip="$(ip -4 addr show enp8s0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)"
    if [[ -z "$lan_ip" ]]; then
        record SKIP CHK-UPP-06 "no IPv4 on enp8s0; cannot probe open-resolver closure from LAN"
        return
    fi
    local rc=0
    set +e
    timeout 8 python3 -c "$DNS_PROBE_PY" "$lan_ip" >/dev/null 2>&1
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        record FAIL CHK-UPP-06 "open resolver still reachable on LAN IP $lan_ip:53"
    else
        record PASS CHK-UPP-06 "open resolver closed: no answer from $lan_ip:53 (rc=$rc)"
    fi
}

chk_upp07() {
    local rc=0
    set +e
    timeout 8 python3 -c "$DNS_PROBE_PY" 192.168.124.1 >/dev/null 2>&1
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-UPP-07 "bridge DNS answers from 192.168.124.1:53"
    else
        record FAIL CHK-UPP-07 "bridge DNS dead: no answer from 192.168.124.1:53 (rc=$rc)"
    fi
}

chk_upp08() {
    local out
    out="$(ip route show 10.0.10.0/24 || true)"
    if grep -qE '^10\.0\.10\.0/24 dev k8sbr0 proto static scope link' <<< "$out"; then
        record PASS CHK-UPP-08 "LB route present after up: $out"
    else
        record FAIL CHK-UPP-08 "LB route missing or not declarative after up: ${out:-<none>}"
    fi
}

chk_upp09() {
    local g out rc
    g="$(getent hosts example.com || true)"
    rc=0
    if [[ -z "$g" ]]; then rc=1; fi
    if grep -q '192.168.124.1' /etc/resolv.conf 2>/dev/null; then rc=1; fi
    if ! grep -qE '^nameserver (1\.1\.1\.1|8\.8\.8\.8)' /etc/resolv.conf 2>/dev/null; then rc=1; fi
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-UPP-09 "host DNS unaffected after up (example.com resolves; resolv.conf still points upstream)"
    else
        record FAIL CHK-UPP-09 "host DNS changed: getent=$g; resolv.conf: $(tr '\n' ' ' < /etc/resolv.conf 2>/dev/null)"
    fi
}

chk_upp10() {
    if [[ "$EGRESS_PROBE_OK" != "1" ]]; then
        record SKIP CHK-UPP-10 "egress baseline unavailable; comparison deferred (external dependency)"
        return
    fi
    local rc=0
    set +e
    timeout 15 curl -sS --max-time 10 https://ifconfig.me > "$F_EGRESS_POSTUP" 2>/dev/null
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        record SKIP CHK-UPP-10 "egress probe failed after up (external dependency)"
    elif cmp -s "$F_EGRESS_BEFORE" "$F_EGRESS_POSTUP"; then
        record PASS CHK-UPP-10 "egress unchanged after up ($(tr -d '\n' < "$F_EGRESS_POSTUP"))"
    else
        record FAIL CHK-UPP-10 "egress changed after up: before=$(tr -d '\n' < "$F_EGRESS_BEFORE") after=$(tr -d '\n' < "$F_EGRESS_POSTUP")"
    fi
}

# ===========================================================================
# Phase E — idempotency and config durability (VC-03, EC-03, EC-04)
# ===========================================================================
chk_idm01() {
    local rc=0
    set +e
    make network-up 2>&1 | tee "$LOG_UP2"
    rc=${PIPESTATUS[0]}
    set -e
    if [[ $rc -eq 0 ]]; then
        record PASS CHK-IDM-01 "repeated make network-up completed (exit 0)"
    else
        record FAIL CHK-IDM-01 "repeated make network-up failed (exit $rc); see $LOG_UP2"
    fi
    sleep 2
}

chk_idm02() {
    local t
    t="$(sudo -n nft list table inet k8slab 2>/dev/null || true)"
    local cm ch1 co
    cm="$(grep -cE 'ip saddr 192.168.124.0/24 masquerade' <<< "$t" || true)"
    ch1="$(grep -cE 'iifname "k8sbr0" accept' <<< "$t" || true)"
    co="$(grep -cE 'oifname "k8sbr0" accept' <<< "$t" || true)"
    if [[ "$cm" == "1" && "$ch1" == "1" && "$co" == "1" ]]; then
        record PASS CHK-IDM-02 "idempotent load: k8slab still exactly 3 rules after repeated network-up"
    else
        record FAIL CHK-IDM-02 "k8slab rule duplication after repeated network-up (masquerade=$cm, iifname=$ch1, oifname=$co); expected 1 1 1"
    fi
}

chk_idm03() {
    local c
    c="$(grep -cE '^\s*conf-dir=.*dnsmasq\.d' /etc/dnsmasq.conf || true)"
    if [[ "$c" == "1" ]]; then
        record PASS CHK-IDM-03 "dnsmasq conf-dir activated exactly once"
    else
        record FAIL CHK-IDM-03 "conf-dir active count=$c; expected exactly 1"
    fi
}

chk_idm04() {
    if [[ ! -f /etc/dnsmasq.d/k8sbr0.conf ]]; then
        record FAIL CHK-IDM-04 "dnsmasq drop-in /etc/dnsmasq.d/k8sbr0.conf missing"
        return
    fi
    local cb ci
    cb="$(grep -c 'bind-dynamic' /etc/dnsmasq.d/k8sbr0.conf || true)"
    ci="$(grep -c 'bind-interfaces' /etc/dnsmasq.d/k8sbr0.conf || true)"
    if [[ "$cb" == "1" && "$ci" == "0" ]]; then
        record PASS CHK-IDM-04 "drop-in uses bind-dynamic (1) and no bind-interfaces (0)"
    else
        record FAIL CHK-IDM-04 "drop-in bind-dynamic=$cb bind-interfaces=$ci; expected 1 and 0"
    fi
}

chk_idm05() {
    local out
    out="$(sudo -n dnsmasq --test 2>&1 || true)"
    if [[ "$out" == *"syntax check OK"* ]]; then
        record PASS CHK-IDM-05 "dnsmasq config syntax valid"
    else
        record FAIL CHK-IDM-05 "dnsmasq --test failed: $out"
    fi
}

chk_idm06() {
    local resolved
    resolved="$(systemd-analyze cat-config systemd/networkd.conf 2>/dev/null | grep -E '^\s*ManageForeignRoutingPolicyRules=' || true)"
    if [[ -f /etc/systemd/networkd.conf.d/90-k8slab-foreign-rules.conf && "$resolved" == *"no"* ]]; then
        record PASS CHK-IDM-06 "networkd drop-in installed; ManageForeignRoutingPolicyRules=no resolved"
    else
        record FAIL CHK-IDM-06 "networkd drop-in missing or ManageForeignRoutingPolicyRules not 'no' (resolved: $resolved)"
    fi
}

chk_idm07() {
    if [[ "$WG_UP" != "UP" ]]; then
        record SKIP CHK-IDM-07 "wg1 tunnel is down; VPN rule survival cannot be verified"
        return
    fi
    sudo -n systemctl reload-or-restart systemd-networkd >/dev/null 2>&1 || true
    sleep 2
    local c51820 csup
    c51820="$(ip rule show | grep -c 'lookup 51820' || true)"
    csup="$(ip rule show | grep -c 'suppress_prefixlength' || true)"
    if (( c51820 >= 1 && csup >= 1 )); then
        record PASS CHK-IDM-07 "wg-quick rules survive explicit systemd-networkd reload (lookup 51820 x${c51820}, suppress x${csup})"
    else
        record FAIL CHK-IDM-07 "wg-quick rules missing after explicit systemd-networkd reload (defect 1 regression)"
    fi
}

chk_idm08() {
    local bad
    bad="$(grep -nE 'nft flush ruleset|ip route add 10\.0\.10\.0/24|bind-interfaces' Makefile network/dnsmasq-k8sbr0.conf || true)"
    local dnt
    dnt="$(grep -n 'network-down:' Makefile || true)"
    if [[ -z "$bad" && -n "$dnt" ]]; then
        record PASS CHK-IDM-08 "no destructive/imperative remnants in repo; network-down target present"
    else
        record FAIL CHK-IDM-08 "repo regression: bad patterns=[${bad:-none}] network-down=[${dnt:-missing}]"
    fi
}

# ===========================================================================
# Phase F — VM-level checks (VC-08)
# ===========================================================================
chk_vm00() {
    local open=0
    local reasons=""
    local ch
    ch="$(pgrep -cf '[c]loud-hypervisor' 2>/dev/null || true)"
    if (( ch > 0 )); then
        open=$((open + 1))
    else
        reasons+="cloud-hypervisor not running; "
    fi
    if [[ -n "$TOFU_BIN" ]] && "$TOFU_BIN" -chdir=terraform output -json nodes 2>/dev/null | grep -q '"mac"'; then
        open=$((open + 1))
    else
        reasons+="tofu nodes output missing MACs; "
    fi
    local i
    for i in 1 2 3 4 5 6 7 8; do
        if networkctl status k8sbr0 2>/dev/null | grep -q 'Offered DHCP leases'; then
            open=$((open + 1))
            break
        fi
        sleep 2
    done
    if [[ $open -ne 3 ]]; then
        reasons+="no offered DHCP leases on k8sbr0; "
    fi
    if [[ $open -eq 3 ]]; then
        VM_GATE_OPEN=1
        record PASS CHK-VM-00 "gate open: cloud-hypervisor running, tofu nodes present, DHCP leases offered"
    else
        VM_GATE_OPEN=0
        record SKIP CHK-VM-00 "no bootable VM detected (${reasons%; })"
    fi
}

chk_vm01() {
    if [[ "$VM_GATE_OPEN" != "1" ]]; then
        record SKIP CHK-VM-01 "no bootable VM detected; DHCP lease check skipped"
        return
    fi
    local all_ok=1
    local mac ip
    for mac in "${NODES_MACS[@]}"; do
        if ip="$(./scripts/vm-ip.sh "$mac" 2>/dev/null || true)" && \
           grep -qE '^192\.168\.124\.(1[0-9][0-9]|2[0-4][0-9]|200|[2-9][0-9])$' <<< "$ip"; then
            printf '    VM lease: %s -> %s\n' "$mac" "$ip"
        else
            printf '    VM lease: %s -> <none/unresolved>\n' "$mac"
            all_ok=0
        fi
    done
    if [[ $all_ok -eq 1 ]]; then
        record PASS CHK-VM-01 "all node MACs have pool DHCP leases (192.168.124.20-200)"
    else
        record FAIL CHK-VM-01 "one or more node MACs lack a pool DHCP lease"
    fi
}

chk_vm02() {
    if [[ "$VM_GATE_OPEN" != "1" ]]; then
        record SKIP CHK-VM-02 "no bootable VM detected; in-VM DNS check skipped"
        return
    fi
    if [[ -z "$SSH_AGENT_SOCK" || -z "$VM_FIRST_IP" ]]; then
        record SKIP CHK-VM-02 "no SSH key for $VM_FIRST_IP (agent socket unavailable)"
        return
    fi
    local out rc=0
    set +e
    out="$(ssh_vm_timeout 15 "$VM_FIRST_IP" "getent hosts example.com" 2>&1)"
    rc=$?
    set -e
    if [[ $rc -eq 0 && -n "$out" ]]; then
        record PASS CHK-VM-02 "VM DNS resolves via bridge ($VM_FIRST_IP: getent example.com)"
    else
        record FAIL CHK-VM-02 "VM DNS resolution failed via SSH to $VM_FIRST_IP (rc=$rc): $out"
    fi
}

chk_vm03() {
    if [[ "$VM_GATE_OPEN" != "1" ]]; then
        record SKIP CHK-VM-03 "no bootable VM detected; VM egress NAT check skipped"
        return
    fi
    if [[ -z "$SSH_AGENT_SOCK" || -z "$VM_FIRST_IP" ]]; then
        record SKIP CHK-VM-03 "no SSH key for $VM_FIRST_IP (agent socket unavailable)"
        return
    fi
    if [[ "$EGRESS_PROBE_OK" != "1" ]]; then
        record SKIP CHK-VM-03 "egress baseline unavailable; VM egress comparison deferred"
        return
    fi
    local out rc=0
    set +e
    out="$(ssh_vm_timeout 25 "$VM_FIRST_IP" "timeout 10 curl -sS --max-time 8 https://ifconfig.me" 2>&1)"
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        local before after
        before="$(tr -d '[:space:]' < "$F_EGRESS_BEFORE")"
        after="$(printf '%s' "$out" | tr -d '[:space:]')"
        if [[ -n "$after" && "$before" == "$after" ]]; then
            record PASS CHK-VM-03 "VM egress NAT works: VM egress == host egress ($after)"
        else
            record FAIL CHK-VM-03 "VM egress differs from host: VM=$after host=$before"
        fi
    else
        record SKIP CHK-VM-03 "VM egress probe failed inside VM (rc=$rc, external dependency): $out"
    fi
}

chk_vm04() {
    if grep -qE '^\s*DNS=192\.168\.124\.1' network/k8sbr0.network \
       && grep -qE '^\s*EmitDNS=yes' network/k8sbr0.network; then
        record PASS CHK-VM-04 "k8sbr0.network declares DNS=192.168.124.1 + EmitDNS=yes"
    else
        record FAIL CHK-VM-04 "k8sbr0.network missing DNS=192.168.124.1 or EmitDNS=yes"
    fi
}

# ===========================================================================
# Phase G — Summary
# ===========================================================================
chk_smy01() {
    if [[ $FAIL_COUNT -eq 0 ]]; then
        record PASS CHK-SMY-01 "aggregate verdict: zero FAILs — all applicable checks PASSed"
    else
        record FAIL CHK-SMY-01 "aggregate verdict: ${FAIL_COUNT} FAIL(s); failing: ${FAIL_IDS[*]}"
    fi
}

# ===========================================================================
# main
# ===========================================================================
main() {
    printf '%s\n' "=== verify-network.sh — VPN-safe cluster network verification ==="
    printf 'repo: %s\n' "$REPO_ROOT"
    printf 'started: %s\n' "$(date -Is)"
    printf '%s\n' ""

    TOFU_BIN="$(resolve_tofu)"
    SSH_AGENT_SOCK="$(resolve_agent_socket || true)"
    [[ -f /etc/systemd/networkd.conf.d/90-k8slab-foreign-rules.conf ]] && DROPIN_BEFORE=1

    printf '%s\n' "--- Phase 0: prerequisites ---"
    chk_prq01
    chk_prq02
    chk_prq03

    printf '%s\n' "--- Phase A: baseline capture (VC-01) ---"
    chk_bsl01
    chk_bsl02
    chk_bsl03
    chk_bsl04
    chk_bsl05
    chk_bsl06

    printf '%s\n' "--- Phase B: damage assessment (EC-01/02) ---"
    chk_dmg01
    chk_dmg02
    chk_dmg03

    printf '%s\n' "--- Phase C: make network-down (VC-02/04/06/07) ---"
    chk_dwn01
    chk_dwn02
    chk_dwn03
    chk_dwn04
    chk_dwn05
    chk_dwn06
    chk_dwn07
    chk_dwn08
    chk_dwn09

    printf '%s\n' "--- Phase D: make network-up (VC-02/03/05/06/07) ---"
    chk_upp01
    chk_upp02
    chk_upp03
    chk_upp04
    chk_upp05
    chk_upp06
    chk_upp07
    chk_upp08
    chk_upp09
    chk_upp10

    printf '%s\n' "--- Phase E: idempotency and config durability (VC-03, EC-03/04) ---"
    chk_idm01
    chk_idm02
    chk_idm03
    chk_idm04
    chk_idm05
    chk_idm06
    chk_idm07
    chk_idm08

    printf '%s\n' "--- Phase F: VM-level checks (VC-08) ---"
    if [[ -n "$TOFU_BIN" ]]; then
        NODES_JSON="$("$TOFU_BIN" -chdir=terraform output -json nodes 2>/dev/null || true)"
        mapfile -t NODES_MACS < <(printf '%s' "$NODES_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for n in data:
    print(n.get("mac", ""))
' 2>/dev/null || true)
    fi
    chk_vm00
    if [[ "$VM_GATE_OPEN" == "1" && "${#NODES_MACS[@]}" -gt 0 ]]; then
        VM_FIRST_IP="$(./scripts/vm-ip.sh "${NODES_MACS[0]}" 2>/dev/null || true)"
    fi
    chk_vm01
    chk_vm02
    chk_vm03
    chk_vm04

    printf '%s\n' "--- Phase G: summary ---"
    chk_smy01

    printf '%s\n' ""
    printf '%s\n' "=== VERIFICATION SUMMARY ==="
    printf 'TOTAL: %d | PASS: %d | FAIL: %d | SKIP: %d\n' \
        "$((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))" "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
    if [[ $FAIL_COUNT -gt 0 ]]; then
        printf 'Failing: %s\n' "${FAIL_IDS[*]}"
    fi
    if [[ $SKIP_COUNT -gt 0 ]]; then
        printf 'Skipped:\n'
        local note
        for note in "${SKIP_NOTES[@]}"; do
            printf '  - %s\n' "$note"
        done
    fi
    if [[ $FAIL_COUNT -eq 0 ]]; then
        printf 'VERDICT: PASS\n'
    else
        printf 'VERDICT: FAIL\n'
    fi
    printf 'finished: %s\n' "$(date -Is)"
    [[ $FAIL_COUNT -eq 0 ]]
}

main "$@"
