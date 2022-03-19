# Prerequisites

## Libvirt Platform

This tutorial leverages libvirt and KVM/QEMU to streamline provisioning of the compute infrastructure required to
bootstrap a Kubernetes cluster from the ground up. First step is to find a baremetal server with enough resources to
run a Kubernetes cluster on virtual machines. In my case I am lucky to borrow a Dell Blade with the following resources

During this tutorial You can configure a high availability Kubernetes cluster made by the following virtual machines.
Note that all will run virtualized in the baremetal server, possible configuration:

|  VM Name     | Purpose    |   OS            | vCPUs | Memory | Disk  |
| ------------ | ---------- | ----------------|-------|--------|-------|
| master1      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |
| master2      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |
| master2      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |
| worker1      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |
| worker2      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |
| worker3      | controller | CentOS 9 Stream |   2   |  2 GB  | 20 GB |

## Libvirt CLI

In order to deploy all the virtual devices needed to run the infrastructure we can make use of the virsh command line.
The virsh program is the main interface for managing virsh guest domains. The program can be used to create, pause,
and shutdown domains. It can also be used to list current domains.

However, much easier to use [kcli](https://kcli.readthedocs.io/en/latest/) to deploy my virtual infrastructure.
**Kcli** is a tool meant to interact with existing virtualization providers (libvirt, kubevirt, ovirt, openstack,
gcp and aws, vsphere) and to easily deploy and customize vms from cloud images. You can also interact with those
vms (list, info, ssh, start, stop, delete, console, serialconsole, add/delete disk, add/delete nic,…).
Futhermore, you can deploy vms using predefined profiles, several at once using plan files or entire products
for which plans were already created for you.


### Install kcli

Follow the kcli [documentation](https://kcli.readthedocs.io/en/latest/#installation) to install and configure all the
binaries needed to manage the libvirt daemon of the baremetal server


### Configure kcli to manage the local libvirt

Once you have kcli configured in your baremetal server you need to create a ssh key pair with an empty passphrase to interact with the libvirt daemon. Also this public ssh key will be automatically injected into the virtual machines you are about to create, allowing you to ssh from the baremetal server automatically once the vm is up and running.

```
# ssh-keygen -t rsa -b 2048
Generating public/private rsa key pair.
Enter file in which to save the key (/root/.ssh/id_rsa):
Enter passphrase (empty for no passphrase):
Enter same passphrase again:
Your identification has been saved in /root/.ssh/id_rsa.
Your public key has been saved in /root/.ssh/id_rsa.pub.
The key fingerprint is:
SHA256:C4/ecwZji6RNyQ91q9UVs7P0HF4Jj/u0mgcco3A6jVs root@smc-master.cloud.lab.eng.bos.redhat.com
The key's randomart image is:
+---[RSA 2048]----+
|                 |
|             .o  |
|              ++.|
|        .... +=+.|
|     ..oS.*ooo==o|
|      *++=oE.+.oo|
|     =.=o*+   + .|
|    ..o.=.o   .+ |
|      . .+   oo  |
+----[SHA256]-----+
```

Establish the connection between the kcli and the local libvirt.

```
kcli create host kvm -H 127.0.0.1 localhost
```

Verify

```
+-----------+------+---------+---------+
| Client    | Type | Enabled | Current |
+-----------+------+---------+---------+
| localhost | kvm  |   True  |    X    |
+-----------+------+---------+---------+
```

Next step is create a pool where the cloud images are donwloaded and where the vms are going to be placed in the baremetal server. In this case, we use the defacto libvirt image path:

```
kcli create pool -p /var/lib/libvirt/images/ default
Adding pool default...
```

Finally, gather all the information from the host you already installed:

```
# kcli info host
Host: arch
Cpus: 8
Vms Running: 3
Total Memory Assigned: 6144MB of 14992MB
Storage:default Type: dir Path:/var/lib/libvirt/images Used space: 26.07GB Available space: 36.37GB
Storage:downloads Type: dir Path:/home/moeryomenko/downloads Used space: 91.51GB Available space: 281.89GB
Network: wlan0 Type: bridged
Network: k8s-net Type: bridged
Network: default Type: routed Cidr: 192.168.122.0/24 Dhcp: True
Network: k8s-net Type: routed Cidr: 192.168.111.0/24 Dhcp: True
```

> Note that there is a default network already configured when installing libvirt called default (192.168.122.0/24)

## Running Commands in Parallel with tmux

[tmux](https://github.com/tmux/tmux/wiki) can be used to run commands on multiple compute instances at the same time. Labs in this tutorial may require running the same commands across multiple compute instances, in those cases consider using tmux and splitting a window into multiple panes with `synchronize-panes` enabled to speed up the provisioning process.

Alternative solution, use [kitty](https://sw.kovidgoyal.net/kitty/) [broadcast](https://sw.kovidgoyal.net/kitty/remote-control/#broadcasting-what-you-type-to-all-kitty-windows)

Next: [Installing the Client Tools](02-client-tools.md)
