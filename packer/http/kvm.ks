text
lang en_US.UTF-8
keyboard us
timezone --utc Europe/Moscow
# add console and reorder in %post
bootloader --timeout=1 --location=mbr --append="console=ttyS0,115200n8 no_timer_check crashkernel=auto net.ifnames=0"
auth --enableshadow --passalgo=sha512
#authselect select sssd
selinux --enforcing
firewall --enabled --service=ssh
network --bootproto=dhcp --device=link --activate --onboot=on
#services --enabled=sshd,ovirt-guest-agent --disabled kdump,rhsmcertd
services --enabled=sshd,NetworkManager,cloud-init,cloud-init-local,cloud-config,cloud-final --disabled kdump,rhsmcertd
rootpw --iscrypted nope

#
# Partition Information. Change this as necessary
# This information is used by appliance-tools but
# not by the livecd tools.
#
zerombr
clearpart --all --initlabel
reqpart
part / --fstype="xfs" --mkfsoptions "-m bigtime=0,inobtcount=0" --ondisk=vda --size=20000
reboot

%packages --ignoremissing
# dnf group info minimal-environment
@^minimal-environment
sudo
# Exclude unnecessary firmwares
-iwl*firmware
%end

%post --nochroot --logfile=/mnt/sysimage/root/ks-post.log
# Update time
/usr/sbin/ntpdate -bu 0.ru.pool.ntp.org 1.ru.pool.ntp.org

# sudo
echo "centos ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

chmod 0700 -R /home/centos/.ssh
chown centos:centos -R /home/centos/.ssh
sed -i 's/^.*requiretty/#Defaults requiretty/' /etc/sudoers
sed -i 's/rhgb //' /etc/default/grub

# Disable consistent network device naming
/usr/bin/ln -s /dev/null /etc/udev/rules.d/80-net-name-slot.rules

# sshd PermitRootLogin yes
sed -i "s/#PermitRootLogin yes/PermitRootLogin yes/g" /etc/ssh/sshd_config

# Enable NetworkManager, sshd and disable firewalld
#/usr/bin/systemctl enable NetworkManager
/usr/bin/systemctl enable sshd
/usr/bin/systemctl disable firewalld

# Need for host/guest communication
/usr/bin/systemctl enable qemu-guest-agent

sed -i 's|^enabled=1|enabled=0|' /etc/yum/pluginconf.d/product-id.conf
sed -i 's|^enabled=1|enabled=0|' /etc/yum/pluginconf.d/subscription-manager.conf

# Update all packages
/usr/bin/yum -y update
# clean up installation logs"
rm -rf /var/log/yum.log
rm -rf /var/lib/yum/*
rm -rf /root/install.log
rm -rf /root/install.log.syslog
rm -rf /root/anaconda-ks.cfg
rm -rf /var/log/anaconda*

echo "Fixing SELinux contexts."
touch /var/log/cron
touch /var/log/boot.log
mkdir -p /var/cache/yum
/usr/sbin/fixfiles -R -a restore

# remove random-seed so it's not the same every time
rm -f /var/lib/systemd/random-seed

# Remove machine-id on the pre generated images
cat /dev/null > /etc/machine-id

# Anaconda is writing to /etc/resolv.conf from the generating environment.
# The system should start out with an empty file.
truncate -s 0 /etc/resolv.conf
%end
