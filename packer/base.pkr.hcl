# Packer QEMU/KVM base image definition for k8labs.
# This source block builds a minimal Fedora 41 VM image that serves
# as the immutable base for Kubernetes node sysext/confext layering.

source "qemu" "kvm" {
  iso_url          = var.iso_url
  iso_checksum     = var.iso_checksum
  output_directory = var.output_directory
  shutdown_timeout = "5m"
  disk_size        = var.vm_disk_size
  format           = "qcow2"
  accelerator      = "kvm"
  http_directory   = "fedora"
  ssh_username     = var.ssh_username
  ssh_password     = var.ssh_password
  ssh_timeout      = "20m"
  boot_wait        = "10s"
  boot_command     = var.boot_command
  vm_name          = var.vm_name
  memory           = var.vm_memory
  cores            = var.vm_cpu_cores
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  headless         = true
  qemu_binary      = "qemu-system-x86_64"
}

build {
  sources = ["source.qemu.kvm"]

  provisioner "shell" {
    scripts = [
      "scripts/01-configure-system.sh",
      "scripts/02-cleanup.sh",
      "scripts/03-seal.sh"
    ]
  }

  post-processor "manifest" {
    output     = "${var.output_directory}/manifest.json"
    strip_path = true
  }
}
