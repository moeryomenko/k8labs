# Networking for VMs.
#
# Unlike the libvirt setup, Cloud-Hypervisor does not manage networks.
# The external bridge 'k8sbr0' is created and managed by host infrastructure
# (scripts/create-taps.sh or systemd-networkd).
#
# Each VM gets a TAP device connected to k8sbr0. DHCP is provided by dnsmasq
# running on the bridge host.
#
# Key networking properties:
#   Bridge:     k8sbr0 (192.168.124.1/24)
#   DHCP pool:  192.168.124.20 - 192.168.124.200 (12h lease)
#   DNS:        forwarded to system resolvers
#   Domain:     k8s.local
#
# DHCP reservations for predictable IP assignment are configured in
# scripts/dnsmasq.conf on the host.

output "bridge_info" {
  description = "Information about the VM network bridge"
  value = {
    name       = var.network_bridge
    cidr       = "192.168.124.0/24"
    gateway    = "192.168.124.1"
    dhcp_range = "192.168.124.20-192.168.124.200"
  }
}
