# k8labs base image build variables
# Usage: packer build -var-file=vars.pkrvars.hcl .

# Cloud-Hypervisor firmware
firmware_path        = "../build/CLOUDHV.fd"
ch_binary_path       = "cloud-hypervisor"

# TAP networking for Packer SSH
tap_device           = "packer-tap"
guest_ip             = "192.168.124.10"
guest_mac            = "de:ad:be:ef:00:01"

# Fedora Cloud Base image (download if not present)
cloud_image_url      = "https://mirror.arizona.edu/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
cloud_image_checksum = "28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"
cloud_image_path     = "../build/fedora-cloud-base.raw"

# Cloud-init disk for Packer SSH access
cloudinit_disk_path  = "../build/cloudinit.img"

# SSH provisioning
ssh_username         = "root"
ssh_private_key_file = "../build/packer-ssh-key"
ssh_timeout          = "15m"

# VM hardware
vm_cpu_cores         = 2
vm_memory            = 2048

# Output
output_directory     = "../build/base-image"
output_image_name    = "k8labs-base.qcow2"
