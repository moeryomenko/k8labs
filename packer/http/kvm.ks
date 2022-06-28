# Kickstart file to build CentOS-Stream-9 Guest image.
# This image is used to test CentOS-Stream-9 content for
# the cloud instances. This image provides minimally configured
# system image.

url --url="http://mirror.stream.centos.org/9-stream/BaseOS/x86_64/os/"
text
firstboot --disable
keyboard --vckeymap=us --xlayouts='us'
lang en_US.UTF-8
network --bootproto=dhcp --device=eth0 --ipv6=auto --activate
network --hostname=centos9.localdomain
selinux --disabled
rootpw testtest
user --groups=wheel --name=user --password=testtest --uid=1000 --gecos="user" --gid=1000
sshkey --username=user "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDUXg2vJmOBNIHd5j6gWFBs0/I4IWXp1jIHBn93FyUQsgiVOG82jhCA69G2SqCYbZHRJSJhwOFSMtMsvDno5Gz+tZMSASliiQnDD26YxiqZZUOApqCpdYKYEhwjVcokjKfm1rVdYhysk1K/qmlL6D0SVAzZxsepl7x8JksMVjvOsuGsZywsvh/Ck7JqEMt9O/NDWv0iFGkGy7J888eAnc+bMyiVV4ND+yYPqpCtL+fPU/dY7+LMR9uDoiJK8fAOmCrBvRLwmKOCh4NNRsHk58L36gl3ArUpNlqWrotpLROHhrXcuh4hSmPuTVsxQOTrzaHM2oVkw/+LBpFFqMLJrAaM8sVrfUBAhRD91cFHjazXg7RvXE1dbkPWDH6THJ71CS1FLyz2htMd7nYuJX/3J2bk533JKZVy/nOEtb0k2s1yCw4WNhT7M+RSFjsvgFsJJkvcGKPpIUwdkctzAXj4hAC1sdhiLsdh/j9E5yw2Tr6rRZ4nuBGDUOqlHABSZBm1d6k= packer-kvm-default-key"
sshkey --username=root "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDUXg2vJmOBNIHd5j6gWFBs0/I4IWXp1jIHBn93FyUQsgiVOG82jhCA69G2SqCYbZHRJSJhwOFSMtMsvDno5Gz+tZMSASliiQnDD26YxiqZZUOApqCpdYKYEhwjVcokjKfm1rVdYhysk1K/qmlL6D0SVAzZxsepl7x8JksMVjvOsuGsZywsvh/Ck7JqEMt9O/NDWv0iFGkGy7J888eAnc+bMyiVV4ND+yYPqpCtL+fPU/dY7+LMR9uDoiJK8fAOmCrBvRLwmKOCh4NNRsHk58L36gl3ArUpNlqWrotpLROHhrXcuh4hSmPuTVsxQOTrzaHM2oVkw/+LBpFFqMLJrAaM8sVrfUBAhRD91cFHjazXg7RvXE1dbkPWDH6THJ71CS1FLyz2htMd7nYuJX/3J2bk533JKZVy/nOEtb0k2s1yCw4WNhT7M+RSFjsvgFsJJkvcGKPpIUwdkctzAXj4hAC1sdhiLsdh/j9E5yw2Tr6rRZ4nuBGDUOqlHABSZBm1d6k= packer-kvm-default-key"
timezone Europe/Paris --utc
bootloader --location=mbr --append=" net.ifnames=0 biosdevname=0 crashkernel=no"
# Clear the Master Boot Record
zerombr
# Remove partitions
clearpart --all --initlabel
# Automatically create partitions using LVM
autopart --type=lvm
# Reboot after successful installation
reboot

# Packages
%packages --excludedocs
sudo
qemu-guest-agent
openssh-server
-kexec-tools
-dracut-config-rescue
-plymouth*
-iwl*firmware
%end

%addon com_redhat_kdump --disable
%end

%post
# Update time
#/usr/sbin/ntpdate -bu 0.fr.pool.ntp.org 1.fr.pool.ntp.org

#sed -i 's/^.*requiretty/#Defaults requiretty/' /etc/sudoers
sed -i 's/rhgb //' /etc/default/grub

# Disable consistent network device naming
#/usr/bin/ln -s /dev/null /etc/udev/rules.d/80-net-name-slot.rules

# sshd PermitRootLogin yes
sed -i "s/#PermitRootLogin prohibit-password/PermitRootLogin yes/g" /etc/ssh/sshd_config
#echo "user ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
cat <<EOF >> /etc/sudoers
Defaults !requiretty
root ALL=(ALL) ALL
user ALL=(ALL) NOPASSWD: ALL
EOF

# Enable NetworkManager, sshd and disable firewalld
#/usr/bin/systemctl enable NetworkManager
/usr/bin/systemctl enable sshd
/usr/bin/systemctl start sshd
#/usr/bin/systemctl disable firewalld

# Need for host/guest communication
/usr/bin/systemctl enable qemu-guest-agent
/usr/bin/systemctl start qemu-guest-agent

# Update all packages
#/usr/bin/yum -y update
#/usr/bin/yum clean all

# Not really needed since the kernel update already did this. Furthermore,
# running this here reverts the grub menu to the current kernel.
grub2-mkconfig -o /boot/grub2/grub.cfg
%end
