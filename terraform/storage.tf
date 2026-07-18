resource "libvirt_pool" "cluster" {
  name = "k8s-cluster-pool"
  type = "dir"

  target = {
    path = var.pool_path
  }
}

resource "libvirt_volume" "base_image" {
  name = "k8labs-base"
  pool = libvirt_pool.cluster.name

  backing_store = {
    path = var.base_image_path
    format = {
      type = "qcow2"
    }
  }
}
