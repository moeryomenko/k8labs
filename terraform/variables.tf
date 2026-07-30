variable "base_image_path" {
  description = "Path to the pre-built base OS qcow2 image"
  type        = string
}

variable "firmware_path" {
  description = "Path to CLOUDHV.fd UEFI firmware binary"
  type        = string
}

variable "cloudinit_script" {
  description = "Path to the create-cloudinit.sh script"
  type        = string
  default     = "../scripts/create-cloudinit.sh"
}

variable "control_plane" {
  description = "Control plane node configuration"
  type = object({
    name = string
    cpu  = number
    ram  = number  # MiB
    disk = number  # MiB
    mac  = string
  })
}

variable "workers" {
  description = "Worker node configurations"
  type = list(object({
    name = string
    cpu  = number
    ram  = number
    disk = number
    mac  = string
  }))
  default = []
}

variable "ssh_public_key" {
  description = "SSH public key content to inject into VMs"
  type        = string
}

variable "tap_prefix" {
  description = "Prefix for TAP device names (e.g., 'k8s' => k8s-cp-1)"
  type        = string
  default     = "k8s"
}

variable "network_bridge" {
  description = "Name of the Linux bridge used for VM networking"
  type        = string
  default     = "k8sbr0"
}

variable "dnsmasq_leases" {
  description = "Path to the dnsmasq lease file for IP discovery"
  type        = string
  default     = "/var/lib/misc/dnsmasq/k8sbr0.leases"
}
