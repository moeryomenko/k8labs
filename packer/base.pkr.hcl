# Packer QEMU/KVM base image definition for k8labs.
# This source block builds a minimal Fedora 44 VM image that serves
# as the immutable base for Kubernetes node sysext/confext layering.

source "qemu" "kvm" {
  iso_url          = var.iso_url
  iso_checksum     = var.iso_checksum
  output_directory = var.output_directory
  shutdown_timeout = "5m"
  disk_size        = var.vm_disk_size
  format           = "qcow2"
  accelerator      = "kvm"
  ssh_username     = var.ssh_username
  ssh_password     = var.ssh_password
  ssh_timeout      = "30m"
  boot_wait        = "30s"
  boot_command     = var.boot_command
  vm_name          = var.vm_name
  memory           = var.vm_memory
  cores            = var.vm_cpu_cores
  disk_interface   = "virtio"
  net_device       = "virtio-net"
  headless            = true
  use_default_display = true
  vnc_bind_address    = "127.0.0.1"
  qemu_binary         = "qemu-system-x86_64"

  # Attach kickstart as OEMDRV volume (auto-detected by anaconda)
  cd_files = ["fedora/ks.cfg"]
  cd_label = "OEMDRV"

  # Capture serial console for debugging
  qemuargs = [
    ["-serial", "file:/tmp/packer-serial.log"]
  ]
}

build {
  sources = ["source.qemu.kvm"]

  provisioner "shell" {
    environment_vars = [
      "KERNEL_VERSION=${var.kernel_version}",
    ]
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
