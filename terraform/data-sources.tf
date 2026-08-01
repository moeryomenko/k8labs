# VM IPs are obtained from the systemd-networkd DHCP server lease file:
#   ${var.dnsmasq_leases}
#
# The Ansible inventory script (ansible/inventory/inventory.py) reads this
# lease file and maps MAC addresses to IP addresses.
#
# To get a VM IP from the command line:
#   grep <mac-address> $(var.dnsmasq_leases) | awk '{print $3}'
