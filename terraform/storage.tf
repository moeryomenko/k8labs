# Storage for VMs.
#
# Unlike libvirt, Cloud-Hypervisor does not manage storage pools. VM disk
# images are files on the host filesystem, passed directly by path.
#
# The base image is built by Packer (make base) and stored at
# build/k8labs-base.qcow2. Each VM shares this base image as its root disk.
#
# For copy-on-write efficiency, consider using qcow2 backing files:
#   qemu-img create -b ../build/k8labs-base.qcow2 -f qcow2 /path/to/overlay.qcow2
# and pass the overlay path as base_image_path per VM.

output "base_image" {
  description = "Base image path for VMs"
  value = {
    path   = var.base_image_path
    format = "qcow2"
  }
}
