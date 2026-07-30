# VM IPs are obtained from the dnsmasq DHCP lease file:
#   ${var.dnsmasq_leases}
#
# The Ansible inventory script (ansible/inventory/tf-inventory.sh) reads this
# lease file and maps MAC addresses to IP addresses.
#
# To get a VM IP from the command line:
#   grep <mac-address> $(var.dnsmasq_leases) | awk '{print $3}'
