output "control_plane_ip" {
  description = "IP address of the control-plane node"
  value = try(
    data.libvirt_domain_interface_addresses.control_plane.interfaces[0].addrs[0].addr,
    null
  )
}

output "worker_ips" {
  description = "IP addresses of worker nodes"
  value = [
    for i in range(length(var.workers)) :
    try(
      data.libvirt_domain_interface_addresses.worker[i].interfaces[0].addrs[0].addr,
      null
    )
  ]
}

output "node_names" {
  description = "All node names in the cluster"
  value = concat(
    [try(libvirt_domain.control_plane.name, "")],
    [for w in libvirt_domain.worker : try(w.name, "")]
  )
}

output "connection_instructions" {
  description = "SSH connection commands for each node"
  value = merge(
    try({
      (libvirt_domain.control_plane.name) = "ssh -i ${var.ssh_public_key_path} root@${data.libvirt_domain_interface_addresses.control_plane.interfaces[0].addrs[0].addr}"
    }, {}),
    try({
      for i in range(length(var.workers)) :
      (libvirt_domain.worker[i].name) => "ssh -i ${var.ssh_public_key_path} root@${data.libvirt_domain_interface_addresses.worker[i].interfaces[0].addrs[0].addr}"
    }, {})
  )
}
