variable "libvirt_uri" {
  description = "URI of the libvirt connection"
  type        = string
  default     = "qemu:///system"
}

variable "base_image_path" {
  description = "Path to the Packer-built base OS qcow2 image"
  type        = string
}

variable "pool_path" {
  description = "Path to the libvirt storage pool directory"
  type        = string
  default     = "/var/lib/libvirt/k8s-pool"
}

variable "control_plane" {
  description = "Control plane node configuration"
  type = object({
    name = string
    cpu  = number
    ram  = number
    disk = number
  })
}

variable "workers" {
  description = "Worker node configurations"
  type = list(object({
    name = string
    cpu  = number
    ram  = number
    disk = number
  }))
  default = []
}

variable "network_cidr" {
  description = "CIDR notation of the VM network"
  type        = string
  default     = "192.168.124.0/24"
}

variable "bridge_name" {
  description = "Name of the libvirt bridge network"
  type        = string
  default     = "virbr0"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key to inject into VMs"
  type        = string
}
