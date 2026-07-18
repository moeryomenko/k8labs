# ---------------------------------------------------------------------------
# Data sources to query VM IP addresses from libvirt
# libvirt provider v0.9+ uses a separate data source instead of
# exposing addresses directly on libvirt_domain.network_interface.
# ---------------------------------------------------------------------------

data "libvirt_domain_interface_addresses" "control_plane" {
  domain = libvirt_domain.control_plane.name
  source = "lease"
}

data "libvirt_domain_interface_addresses" "worker" {
  count  = length(var.workers)
  domain = libvirt_domain.worker[count.index].name
  source = "lease"
}
