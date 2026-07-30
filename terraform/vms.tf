# ---------------------------------------------------------------------------
# VM definitions for the Kubernetes cluster
# Uses cloudhypervisor provider (not libvirt)
# Networking: TAP devices on k8sbr0 bridge, DHCP via dnsmasq
# ---------------------------------------------------------------------------

locals {
  cp_name    = var.control_plane.name
  worker_names = [for w in var.workers : w.name]
  vm_dir     = abspath("${path.module}/../build/vm-disks")
}

# Template the cloud-init user-data (replaces ${ssh_public_key} with actual key)
resource "local_file" "cp_user_data" {
  content = templatefile("${path.module}/cloud-init/cloud_init.cfg", {
    ssh_public_key = var.ssh_public_key
  })
  filename = "${abspath(path.module)}/cloud-init/out/cp-user-data.yaml"
}

# Cloud-init content for control-plane
resource "local_file" "cp_meta_data" {
  content = templatefile("${path.module}/cloud-init/meta-data.tmpl", {
    instance_id = var.control_plane.name
    hostname    = var.control_plane.name
  })
  filename = "${abspath(path.module)}/cloud-init/out/cp-meta-data.yaml"
}

resource "null_resource" "cp_cloudinit_disk" {
  depends_on = [local_file.cp_user_data, local_file.cp_meta_data]

  provisioner "local-exec" {
    command = <<EOT
${var.cloudinit_script} \
  --user-data ${abspath(path.module)}/cloud-init/out/cp-user-data.yaml \
  --meta-data ${abspath(path.module)}/cloud-init/out/cp-meta-data.yaml \
  --network-config ${abspath(path.module)}/cloud-init/network_config.cfg \
  --output ${abspath(path.module)}/cloud-init/out/${local.cp_name}-cloudinit.img
EOT
  }

  triggers = {
    template_hash = sha256(file("${path.module}/cloud-init/cloud_init.cfg"))
  }
}

# Control-plane VM
resource "cloudhypervisor_vm" "control_plane" {
  depends_on = [null_resource.cp_cloudinit_disk]

  payload = {
    firmware = var.firmware_path
  }

  cpus = {
    boot_vcpus = var.control_plane.cpu
    max_vcpus  = var.control_plane.cpu
  }

  memory = {
    size = var.control_plane.ram * 1024 * 1024  # MiB -> bytes
  }

  disks = [
    {
      path       = "${local.vm_dir}/${var.control_plane.name}-root.qcow2"
      readonly   = false
      image_type = "Qcow2"
      id         = "rootfs"
    },
    {
      path       = "${abspath(path.module)}/cloud-init/out/${local.cp_name}-cloudinit.img"
      readonly   = true
      image_type = "Raw"
      id         = "cloud-init"
    }
  ]

  net = [
    {
      tap = "${var.tap_prefix}-${var.control_plane.name}"
      mac = var.control_plane.mac
    }
  ]
}

# Worker VMs
resource "local_file" "worker_user_data" {
  count   = length(var.workers)
  content = templatefile("${path.module}/cloud-init/cloud_init.cfg", {
    ssh_public_key = var.ssh_public_key
  })
  filename = "${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-user-data.yaml"
}

resource "local_file" "worker_meta_data" {
  count   = length(var.workers)
  content = templatefile("${path.module}/cloud-init/meta-data.tmpl", {
    instance_id = var.workers[count.index].name
    hostname    = var.workers[count.index].name
  })
  filename = "${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-meta-data.yaml"
}

resource "null_resource" "worker_cloudinit_disk" {
  count      = length(var.workers)
  depends_on = [local_file.worker_user_data, local_file.worker_meta_data]

  provisioner "local-exec" {
    command = <<EOT
${var.cloudinit_script} \
  --user-data ${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-user-data.yaml \
  --meta-data ${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-meta-data.yaml \
  --network-config ${abspath(path.module)}/cloud-init/network_config.cfg \
  --output ${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-cloudinit.img
EOT
  }

  triggers = {
    template_hash = sha256(file("${path.module}/cloud-init/cloud_init.cfg"))
  }
}

resource "cloudhypervisor_vm" "worker" {
  count      = length(var.workers)
  depends_on = [null_resource.worker_cloudinit_disk]

  payload = {
    firmware = var.firmware_path
  }

  cpus = {
    boot_vcpus = var.workers[count.index].cpu
    max_vcpus  = var.workers[count.index].cpu
  }

  memory = {
    size = var.workers[count.index].ram * 1024 * 1024  # MiB -> bytes
  }

  disks = [
    {
      path       = "${local.vm_dir}/${var.workers[count.index].name}-root.qcow2"
      readonly   = false
      image_type = "Qcow2"
      id         = "rootfs"
    },
    {
      path       = "${abspath(path.module)}/cloud-init/out/${var.workers[count.index].name}-cloudinit.img"
      readonly   = true
      image_type = "Raw"
      id         = "cloud-init"
    }
  ]

  net = [
    {
      tap = "${var.tap_prefix}-${var.workers[count.index].name}"
      mac = var.workers[count.index].mac
    }
  ]
}
