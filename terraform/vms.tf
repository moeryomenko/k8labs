# ---------------------------------------------------------------------------
# Root volumes cloned from the base image
# ---------------------------------------------------------------------------

resource "libvirt_volume" "control_plane_root" {
  name = "${var.control_plane.name}-root"
  pool = libvirt_pool.cluster.name

  backing_store = {
    path   = libvirt_volume.base_image.path
    format = { type = "qcow2" }
  }
}

resource "libvirt_volume" "worker_root" {
  count = length(var.workers)
  name  = "${var.workers[count.index].name}-root"
  pool  = libvirt_pool.cluster.name

  backing_store = {
    path   = libvirt_volume.base_image.path
    format = { type = "qcow2" }
  }
}

# ---------------------------------------------------------------------------
# Cloud-init ISO shared by all VMs
# ---------------------------------------------------------------------------

resource "libvirt_cloudinit_disk" "commoninit" {
  name      = "commoninit.iso"
  user_data = templatefile("${path.module}/cloud-init/cloud_init.cfg", {
    ssh_public_key = file(var.ssh_public_key_path)
  })
  network_config = file("${path.module}/cloud-init/network_config.cfg")
  meta_data      = file("${path.module}/cloud-init/meta-data")
}

# ---------------------------------------------------------------------------
# Control-plane VM
# ---------------------------------------------------------------------------

resource "libvirt_domain" "control_plane" {
  name   = var.control_plane.name
  type   = "kvm"
  memory = var.control_plane.ram
  vcpu   = var.control_plane.cpu

  autostart = true

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "q35"
  }

  devices = {
    disks = [
      {
        source = {
          volume = {
            pool   = libvirt_pool.cluster.name
            volume = libvirt_volume.control_plane_root.name
          }
        }
        target = {
          dev = "vda"
          bus = "virtio"
        }
      },
      {
        source = {
          file = {
            file = libvirt_cloudinit_disk.commoninit.path
          }
        }
        device = "cdrom"
        target = {
          dev = "vdb"
          bus = "sata"
        }
      }
    ]

    interfaces = [
      {
        source = {
          network = {
            network = libvirt_network.cluster.name
          }
        }
      }
    ]

    channels = [
      {
        source = {
          unix = {}
        }
        target = {
          virt_io = {
            name = "org.qemu.guest_agent.0"
          }
        }
      }
    ]
  }
}

# ---------------------------------------------------------------------------
# Worker VMs
# ---------------------------------------------------------------------------

resource "libvirt_domain" "worker" {
  count  = length(var.workers)
  name   = var.workers[count.index].name
  type   = "kvm"
  memory = var.workers[count.index].ram
  vcpu   = var.workers[count.index].cpu

  autostart = true

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "q35"
  }

  devices = {
    disks = [
      {
        source = {
          volume = {
            pool   = libvirt_pool.cluster.name
            volume = libvirt_volume.worker_root[count.index].name
          }
        }
        target = {
          dev = "vda"
          bus = "virtio"
        }
      },
      {
        source = {
          file = {
            file = libvirt_cloudinit_disk.commoninit.path
          }
        }
        device = "cdrom"
        target = {
          dev = "vdb"
          bus = "sata"
        }
      }
    ]

    interfaces = [
      {
        source = {
          network = {
            network = libvirt_network.cluster.name
          }
        }
      }
    ]

    channels = [
      {
        source = {
          unix = {}
        }
        target = {
          virt_io = {
            name = "org.qemu.guest_agent.0"
          }
        }
      }
    ]
  }
}
