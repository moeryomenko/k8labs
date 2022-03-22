#!/usr/bin/env bash

PLATFORM=$(uname -s | tr A-Z a-z)
ARCH=$([[ $(uname -m) == arm64 ]] && echo arm64 || echo amd64)
KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt)

case $1 in
"kubectl")
  wget -q --timestamping \
    https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${PLATFORM}/${ARCH}/kubectl \
    https://dl.k8s.io/${KUBECTL_VERSION}/bin/${PLATFORM}/${ARCH}/kubectl.sha256
  SUM=$(cat ./kubectl.sha256); echo "$$SUM kubectl" | sha256sum --check
  rm kubectl.sha256
  chmod +x kubectl
  mv kubectl ~/.local/bin
esac
