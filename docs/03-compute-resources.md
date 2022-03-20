# Provisioning Compute Resources

Kubernetes requires a set of machines to host the Kubernetes control plane and the worker nodes where containers are ultimately run. In this lab you will provision the compute resources required for running a secure and highly available Kubernetes cluster.

## Networking

The Kubernetes [networking model](https://kubernetes.io/docs/concepts/cluster-administration/networking/#kubernetes-model) assumes a flat network in which containers and nodes can communicate with each other. In cases where this is not desired [network policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/) can limit how groups of containers are allowed to communicate with each other and external network endpoints.

> Setting up network policies is out of scope for this tutorial.

### Virtual Machines Network

The VM network is a network where all the VMs are executed. Actually this is a virtual network configured as type 'nated', e.g. all VMs will be placed in the same network address range, they will have access to the outside world using the baremetal server virtual bridge as a default gateway (NAT). However, take into account that any remote server won't be able to reach any VM since they are behind the baremetal server.

> Since the baremetal server hosts the virtual infrastructure it is able to connect to any of the VMs. So, as we will see further in this tutorial, anytime you need to execute commands on any of the VMs, you need to connect first to the baremetal server.

By default as commented in the previous sections, there is a default virtual network named as default configured:
```
kcli list network
```
Output expected

```
+---------+--------+------------------+------+---------+------+
| Network |  Type  |       Cidr       | Dhcp |  Domain | Mode |
+---------+--------+------------------+------+---------+------+
| default | routed | 192.168.122.0/24 | True | default | nat  |
+---------+--------+------------------+------+---------+------+
```

We are going to create a new virtual network to place all the Kubernetes cluster resources:

* The network address range is 192.168.111.0/24
* Name of the network is k8s-net
* The domain of this network is k8s.local. This is important since all VMs in this virtual network will get this domain name as part of its fully qualified name.

```
./ctl.sh network
```

Output expected:

```
Network k8s-net deployed
```

Check the list of virtual networks available:

```
# kcli list network
Listing Networks...
+---------+--------+------------------+------+-----------+------+
| Network |  Type  |       Cidr       | Dhcp |   Domain  | Mode |
+---------+--------+------------------+------+-----------+------+
| default | routed | 192.168.122.0/24 | True |  default  | nat  |
| k8s-net | routed | 192.168.111.0/24 | True | k8s.local | nat  |
+---------+--------+------------------+------+-----------+------+
```

## Images

To be able to create instances, an image should be provided. In this guide we will use CentOS 9 Stream as the base
operating system for all the VMs. With kcli this is super easy, just download the cloud CentOS 9 Stream image with kcli command line:

```
kcli download image centos9stream --pool default
```

> Basically kcli is donwloading the latest CentOS 9 Stream cloud image and placing it in the default pool we already defined  (/var/lib/libvirt/images/)


## DNS

It is required to have a proper DNS configuration that must resolve direct and reverse queries of all the VMs. Unlike other similar tutorials, kcli makes really easy to configure a proper DNS resolution of each VM. Everytime you create a new instance it is possible to create a DNS record into **libvirt dnsmasq** running on the baremetal host. It also can even create a /etc/hosts record in the host that executes the instance creation. This information can be found in the kcli official documentation, section [ip, dns and host reservations](https://kcli.readthedocs.io/en/latest/#ip-dns-and-host-reservations)

> There is no need to maintain a DNS server since DNS record can be automatically created when launching a new instance


## Configuring SSH Access

SSH will be used to configure the loadbalancer, controller and worker instances. By leveraging kcli there is no need to manually exchange the ssh key among all the instances. Kcli automatically injects (using cloudinit) the public ssh key from the baremetal server to all the instances at creation time. Therefore, once the instance is up and running you can easily running `kcli ssh vm_name`


## Compute Instances

Each compute instance will be provisioned with a fixed private IP address to simplify the Kubernetes bootstrapping process.

### Kubernetes Controllers

Create three compute instances which will host the Kubernetes **control plane**. Basically we are creating 3 new instances configured with:

- CentOS image as OS
- 20 GB disk
- Connected to the k8s-net (192.168.111.0/24)
- 2GB of memory and 2 vCPus
- Create a DNS record, in this case ${node}.k8s.local which will included in libvirt's dnsmasq (reservedns=true)
- Reserve the IP, so it is not available to any other VM (reserveip=true)
- Create an record into baremetal server's /etc/host so it can be reached from outside the virtual network domain as well. (reserveip=true)
- Execute "yum update -y" once the server is up and running. This command is injected into the cloudinit, so all instances are up to date since the very beginning.

```
./ctl.sh cluster bootstrap -m 1
```

Verify your masters are up and running

```
# kcli list vm
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
|   Name  | Status |       Ips       |                        Source                        |  Plan |    Profile    |
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
| master1 |   up   |  192.168.111.20 | CentOS-Stream-GenericCloud-9-20211203.0.x86_64.qcow2 | kvirt | centos9stream |
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
```


### Kubernetes Workers

Create two compute instances which will host the Kubernetes worker nodes:

```
./ctl.sh cluster bootstrap -w 2
```

### Verification

List the compute instances:

```
# kcli list vm
```

> output

```
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
|   Name  | Status |       Ips       |                        Source                        |  Plan |    Profile    |
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
| master1 |   up   |  192.168.111.20 | CentOS-Stream-GenericCloud-9-20211203.0.x86_64.qcow2 | kvirt | centos9stream |
| worker1 |   up   | 192.168.111.240 | CentOS-Stream-GenericCloud-9-20211203.0.x86_64.qcow2 | kvirt | centos9stream |
| worker2 |   up   | 192.168.111.158 | CentOS-Stream-GenericCloud-9-20211203.0.x86_64.qcow2 | kvirt | centos9stream |
+---------+--------+-----------------+------------------------------------------------------+-------+---------------+
```

## Reboot

Finally, since all packages were updated during the bootstrap of the instance. we must reboot to run the latest ones

```
for node in master1 worker1 worker2 worker3
do
	kcli restart vm $node
done
```

## Container runtime

Install custom container runtime crun + cri-o(see https://cri-o.io/):

```
# export OS=CentOS_8_Stream
# export VERSION=1.23
# curl -L -o /etc/yum.repos.d/devel:kubic:libcontainers:stable.repo https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/stable/$OS/devel:kubic:libcontainers:stable.repo
# curl -L -o /etc/yum.repos.d/devel:kubic:libcontainers:stable:cri-o:$VERSION.repo https://download.opensuse.org/repositories/devel:kubic:libcontainers:stable:cri-o:$VERSION/$OS/devel:kubic:libcontainers:stable:cri-o:$VERSION.repo
# dnf -y install crun cri-o
```

After installation set crun as default runtime, need edit /etc/crio/crio.conf and /etc/crio/crio.conf.d/00-default.conf

```
# /etc/crio/crio.conf

# The crio.runtime table contains settings pertaining to the OCI runtime used
# and options for how to set up and manage the OCI runtime.
[crio.runtime]
default_runtime="crun"
```

```
# /etc/crio/crio.conf.d/00-default.conf

[crio.runtime.runtimes.crun]
runtime_path = "/bin/crun"
runtime_type = "oci"
runtime_root = "/run/crun"
```


Next: [Instaling kubeadm](04-installing-kubeadm.md)
