# Required: Fedora 41 netinstall ISO URL and checksum
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
  default = 10240
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

# Fedora installer boot command with kickstart URL
variable "boot_command" {
  type    = list(string)
  default = ["<tab><wait>", " inst.ks=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ks.cfg<enter><wait>"]
}

# Path to the kickstart file served via Packer HTTP
variable "kickstart_file" {
  type    = string
  default = "packer/fedora/ks.cfg"
}
