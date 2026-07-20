# ---------------------------------------------------------------------------
# Root volumes cloned from the base image
# ---------------------------------------------------------------------------

resource "libvirt_volume" "control_plane_root" {
  name     = "${var.control_plane.name}-root"
  pool     = libvirt_pool.cluster.name
  capacity = var.control_plane.disk * 1024 * 1024  # MB → bytes
  target = {
    format = { type = "qcow2" }
  }

  backing_store = {
    path   = libvirt_volume.base_image.path
    format = { type = "qcow2" }
  }
}

resource "libvirt_volume" "worker_root" {
  count    = length(var.workers)
  name     = "${var.workers[count.index].name}-root"
  pool     = libvirt_pool.cluster.name
  capacity = var.workers[count.index].disk * 1024 * 1024  # MB → bytes
  target = {
    format = { type = "qcow2" }
  }

  backing_store = {
    path   = libvirt_volume.base_image.path
    format = { type = "qcow2" }
  }
}

# ---------------------------------------------------------------------------
# Cloud-init ISOs — per-VM meta-data for unique instance identity
# ---------------------------------------------------------------------------

resource "libvirt_cloudinit_disk" "cp_init" {
  name      = "${var.control_plane.name}-commoninit.iso"
  user_data = templatefile("${path.module}/cloud-init/cloud_init.cfg", {
    ssh_public_key = file(var.ssh_public_key_path)
  })
  network_config = file("${path.module}/cloud-init/network_config.cfg")
  meta_data      = templatefile("${path.module}/cloud-init/meta-data.tmpl", {
    instance_id = var.control_plane.name
    hostname    = var.control_plane.name
  })
}

resource "libvirt_cloudinit_disk" "worker_init" {
  count = length(var.workers)
  name  = "${var.workers[count.index].name}-commoninit.iso"
  user_data = templatefile("${path.module}/cloud-init/cloud_init.cfg", {
    ssh_public_key = file(var.ssh_public_key_path)
  })
  network_config = file("${path.module}/cloud-init/network_config.cfg")
  meta_data      = templatefile("${path.module}/cloud-init/meta-data.tmpl", {
    instance_id = var.workers[count.index].name
    hostname    = var.workers[count.index].name
  })
}

# ---------------------------------------------------------------------------
# Control-plane VM
# ---------------------------------------------------------------------------

resource "libvirt_domain" "control_plane" {
  name   = var.control_plane.name
  type   = "kvm"
  memory = var.control_plane.ram * 1024  # MB → KiB
  vcpu   = var.control_plane.cpu

  running   = true
  autostart = true

  features = {
    acpi = true
  }

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "pc"
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
        driver = {
          name = "qemu"
          type = "qcow2"
        }
        target = {
          dev = "vda"
          bus = "virtio"
        }
      },
      {
        source = {
          file = {
            file = libvirt_cloudinit_disk.cp_init.path
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
        model = { type = "virtio" }
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
  memory = var.workers[count.index].ram * 1024  # MB → KiB
  vcpu   = var.workers[count.index].cpu

  running   = true
  autostart = true

  features = {
    acpi = true
  }

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "pc"
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
        driver = {
          name = "qemu"
          type = "qcow2"
        }
        target = {
          dev = "vda"
          bus = "virtio"
        }
      },
      {
        source = {
          file = {
            file = libvirt_cloudinit_disk.worker_init[count.index].path
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
        model = { type = "virtio" }
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
