#!/bin/sh

CILIUM_VERSION=1.11.2

# install
helm install cilium cilium/cilium --version ${CILIUM_VERSION} \
   --namespace kube-system \
   --set k8sServiceHost=192.168.111.20 \
   --set k8sServicePort=6443 \
   --set nodeinit.enabled=true \
   --set nodeinit.reconfigureKubelet=true \
   --set kubeProxyReplacement=strict \
   --set hostServices.enabled=false \
   --set externalIPs.enabled=true \
   --set nodePort.enabled=true \
   --set hostPort.enabled=true \
   --set bpf.masquerade=false \
   --set image.pullPolicy=IfNotPresent \
   --set ipam.mode=kubernetes \
   --set hubble.enabled=true \
   --set hubble.relay.enabled=true \
   --set hubble.ui.enabled=true \
   --set prometheus.enabled=true \
   --set operator.prometheus.enabled=true
