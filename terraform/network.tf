resource "libvirt_network" "cluster" {
  name = "k8s-cluster-net"

  domain = {
    name       = "k8s.local"
    local_only = "yes"
  }

  forward = {
    mode = "nat"
  }

  ips = [{
    address = cidrhost(var.network_cidr, 1)
    prefix  = tonumber(split("/", var.network_cidr)[1])
    dhcp = {
      ranges = [{
        start = cidrhost(var.network_cidr, 10)
        end   = cidrhost(var.network_cidr, 200)
      }]
    }
  }]

  dns = {
    enable = "yes"
  }
}
