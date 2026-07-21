# Base OS: Fedora 44 (kernel 7.1)
# Required: Fedora 44 netinstall ISO URL and checksum

variable "kernel_version" {
  description = "Kernel version to pin during image baking"
  type        = string
  default     = "7.1"
}
variable "iso_url" {
  type = string
}

variable "iso_checksum" {
  type = string
}

# VM configuration
variable "vm_name" {
  type    = string
  default = "k8labs-base"
}

variable "vm_cpu_cores" {
  type    = number
  default = 2
}

variable "vm_memory" {
  type    = number
  default = 2048
}

variable "vm_disk_size" {
  type    = number
  default = 20480
}

# SSH communicator credentials
variable "ssh_username" {
  type    = string
  default = "root"
}

variable "ssh_password" {
  type = string
}

# Output path for the baked qcow2 image
variable "output_directory" {
  type    = string
  default = "../build/base"
}

# Fedora installer boot command — minimal, just select "Install Fedora 44"
# Kickstart is auto-detected by anaconda from the OEMDRV CD volume.
# Kernel params (console=ttyS0, inst.sshd=1) are baked into the modified grub.cfg.
variable "boot_command" {
  type    = list(string)
  default = ["<up>", "<enter>"]
}

# Path to the kickstart file served via Packer HTTP
variable "kickstart_file" {
  type    = string
  default = "packer/fedora/ks.cfg"
}
