#!/usr/bin/env bash

export OS=CentOS_8_Stream
export VERSION=1.24:1.24.1
curl -L -o /etc/yum.repos.d/devel:kubic:libcontainers:stable.repo https://download.opensuse.org/repositories/devel:/kubic:/libcontainers:/stable/$OS/devel:kubic:libcontainers:stable.repo
curl -L -o /etc/yum.repos.d/devel:kubic:libcontainers:stable:cri-o:$VERSION.repo https://download.opensuse.org/repositories/devel:kubic:libcontainers:stable:cri-o:$VERSION/$OS/devel:kubic:libcontainers:stable:cri-o:$VERSION.repo
dnf -y install crun cri-o

sed -i '/\[crio\.runtime\]/a default_runtime\=\"crun\"' /etc/crio/crio.conf

mkdir /etc/crio/crio.conf.d
cat > /etc/crio/crio.conf.d/00-default.conf <<EOF
[crio.runtime.runtimes.crun]
runtime_path = "/bin/crun"
runtime_type = "oci"
runtime_root = "/run/crun"
EOF
