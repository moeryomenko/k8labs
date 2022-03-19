#!/usr/bin/env bash

PLATFORM=$(uname -s | tr A-Z a-z)
ARCH=$([[ $(uname -m) == arm64 ]] && echo arm64 || echo amd64)
KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)

case $1 in
"cfssl")
  wget -q --timestamping \
    https://storage.googleapis.com/kubernetes-the-hard-way/cfssl/linux/cfssl \
    https://storage.googleapis.com/kubernetes-the-hard-way/cfssl/linux/cfssljson
  chmod +x cfssl cfssljson
  mv cfssl cfssljson ~/.local/bin
  ;;
"kubectl")
  wget -q --timestamping \
    https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${PLATFORM}/${ARCH}/kubectl \
    https://dl.k8s.io/${KUBECTL_VERSION}/bin/${PLATFORM}/${ARCH}/kubectl.sha256
  SUM=$(cat ./kubectl.sha256); echo "$$SUM kubectl" | sha256sum --check
  rm kubectl.sha256
  chmod +x kubectl
  mv kubectl ~/.local/bin
esac
