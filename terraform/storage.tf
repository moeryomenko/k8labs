resource "libvirt_pool" "cluster" {
  name = "k8s-cluster-pool"
  type = "dir"

  target = {
    path = var.pool_path
  }
}

resource "libvirt_volume" "base_image" {
  name   = "k8labs-base"
  pool   = libvirt_pool.cluster.name
  target = {
    format = { type = "qcow2" }
  }

  create = {
    content = {
      # base_image_path is relative to terraform/ directory: ../build/k8labs-base.qcow2
      url = "file://${abspath(var.base_image_path)}"
    }
  }
}
