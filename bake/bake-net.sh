#!/bin/sh
# bake-net.sh — rootless bake networking entrypoint.
#
# Runs inside the k8labs-bake container (own network namespace, namespace
# root). Creates the packer-tap device, gives it the network gateway IP,
# enables forwarding + NAT so the bake guest reaches the internet (Fedora
# repos) through the container's slirp/pasta uplink, and starts dnsmasq for
# DHCP+DNS. Then execs the real command (packer build).
#
# No host privilege is required: everything happens inside the container's
# private user+net namespace.
#
# Configuration mirrors packer/vars.pkrvars.hcl:
#   TAP         packer-tap
#   CIDR       192.168.124.0/24 (gateway .1 on the tap)
#   GUEST_MAC  de:ad:be:ef:00:01 (the CH virtio-net MAC, -> .10)
set -eu

TAP_NAME="${TAP_NAME:-packer-tap}"
NET_CIDR="${NET_CIDR:-192.168.124.0/24}"
GATEWAY_IP="${GATEWAY_IP:-192.168.124.1}"
GUEST_MAC="${GUEST_MAC:-de:ad:be:ef:00:01}"
GUEST_IP="${GUEST_IP:-192.168.124.10}"
DHCP_RANGE="${DHCP_RANGE:-192.168.124.10,192.168.124.200,255.255.255.0}"

echo "==> bake-net: creating ${TAP_NAME} on ${NET_CIDR} (gateway ${GATEWAY_IP})"
ip tuntap add dev "${TAP_NAME}" mode tap
ip addr add "${GATEWAY_IP}/24" dev "${TAP_NAME}"
ip link set "${TAP_NAME}" up

# Enable forwarding and NAT to the container's uplink (the default-route
# interface, e.g. pasta/slirp eth0/enp8s0) so the guest can reach Fedora
# repos during the bake. `make bake-run` passes --sysctl
# net.ipv4.ip_forward=1 (rootless podman mounts /proc/sys read-only, so the
# in-container write below is a best-effort fallback only).
if ! echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null; then
    echo "==> bake-net: note: net.ipv4.ip_forward already set via --sysctl"
fi
UPLINK="$(ip route show default | awk '{print $5; exit}')"
if [ -z "${UPLINK}" ]; then
    echo "ERROR: no default-route interface to NAT through" >&2
    exit 1
fi
echo "==> bake-net: NAT ${NET_CIDR} -> ${UPLINK}"
iptables -t nat -A POSTROUTING -s "${NET_CIDR}" -o "${UPLINK}" -j MASQUERADE
iptables -A FORWARD -i "${TAP_NAME}" -o "${UPLINK}" -j ACCEPT
iptables -A FORWARD -i "${UPLINK}" -o "${TAP_NAME}" -m state \
    --state ESTABLISHED,RELATED -j ACCEPT

# DHCP + DNS on the tap. The guest MAC is pinned to GUEST_IP so Packer's
# ssh_host (var.guest_ip) resolves; dnsmasq forwards DNS upstream via the
# container's resolv.conf (slirp/pasta resolver).
echo "==> bake-net: starting dnsmasq (DHCP ${DHCP_RANGE}, ${GUEST_MAC}->${GUEST_IP})"
dnsmasq \
    --keep-in-foreground \
    --interface="${TAP_NAME}" \
    --bind-interfaces \
    --dhcp-range="${DHCP_RANGE}" \
    --dhcp-host="${GUEST_MAC},${GUEST_IP}" \
    --dhcp-option=3,"${GATEWAY_IP}" \
    --no-hosts \
    --log-facility=- &

DNSMASQ_PID=$!
trap 'kill ${DNSMASQ_PID} 2>/dev/null || true' EXIT INT TERM

# Give dnsmasq a moment to bind, then run the requested command (packer).
sleep 1
echo "==> bake-net: network ready; executing: $*"
exec "$@"