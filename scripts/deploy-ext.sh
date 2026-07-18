#!/usr/bin/env bash
# deploy-ext.sh — Deploy sysext/confext files to VMs via tar-over-SSH
# Runs on the HOST (not in container), used by Makefile before Ansible
set -Eeuo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

usage() { echo "Usage: $0 <IP> [IP2 ...]" >&2; exit 1; }
[ $# -ge 1 ] || usage

for HOST in "$@"; do
  echo "=== Deploying to $HOST ==="

  # Sysexts: copy usr/ to /usr/
  for src in "$PROJECT_DIR"/sysext/*/usr; do
    [ -d "$src" ] || continue
    name=$(basename "$(dirname "$src")")
    echo "  sysext: $name"
    tar cf - -C "$src/.." usr/ 2>/dev/null | \
      ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@$HOST 'cd / && tar xpf - 2>/dev/null' || true
  done

  # Confexts: copy etc/ to /etc/
  for src in "$PROJECT_DIR"/confext/*/etc; do
    [ -d "$src" ] || continue
    name=$(basename "$(dirname "$src")")
    echo "  confext: $name"
    tar cf - -C "$src/.." etc/ 2>/dev/null | \
      ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@$HOST 'cd / && tar xpf - 2>/dev/null' || true
  done

  # Verify
  ssh -o StrictHostKeyChecking=no root@$HOST 'for b in crio crun kubelet etcd kube-apiserver; do [ -f "/usr/bin/$b" ] && echo "  OK: $b" || echo "  MISSING: $b"; done' 2>&1

  echo "  Done"
done
