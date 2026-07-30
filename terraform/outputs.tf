output "nodes" {
  description = "All nodes with their MAC addresses"
  value = concat(
    [{
      name = var.control_plane.name
      mac  = var.control_plane.mac
      role = "control_plane"
    }],
    [for w in var.workers : {
      name = w.name
      mac  = w.mac
      role = "worker"
    }]
  )
}

output "dnsmasq_lease_file" {
  description = "Path to the dnsmasq DHCP lease file for IP discovery"
  value       = var.dnsmasq_leases
}
