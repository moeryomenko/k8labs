# K8Labs

This is a repository with experiments for the `Kubernetes Fundamentals(LFS258)` and `Kubernetes for Developers(LFS259)`
courses and adoptation https://github.com/alosadagrande/kubernetes-the-hard-way-libvirt-kvm with semi-automation some of
steps

## Base vm image

- CentOS 9 Stream(enable cgroup2);
- crun + crio (as container runtime interface);
- cilium base components (as container network interface and replacment of kube-proxy).
