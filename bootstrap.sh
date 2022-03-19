#!/usr/bin/env bash

usage() {
  echo "Helper script for bootstrap local k8s cluster over libvirt(kvm)."
  echo "Syntx: bootstrap.sh [-h]"
  echo "options:"
  echo "h    Print this help"
  echo
  echo "commands:"
  echo "bootstrap [ -m MASTERS ] [ -w WORKERS ] - bootstrap vms with specified numbers master and worker nodes"
  echo
}

# provision masters.
bootstrap_masters() {
  for i in $(seq 1 $1); do
    kcli create vm -i centos9stream \
      -P disks=[20] -P nets=[k8s-net] \
      -P memory=2048 -P numcpus=2 \
      -P cmds=["yum -y update"] \
      -P reservedns=true -P reserveip=true -P reservehost=true master$i
  done
}

# provision worker nodes.
bootstrap_nodes() {
  for i in $(seq 1 $1); do
    kcli create vm -i centos9stream \
      -P disks=[20] -P nets=[k8s-net] \
      -P memory=2048 -P numcpus=2 \
      -P cmds=["yum -y update"] \
      -P reservedns=true -P reserveip=true -P reservehost=true worker$i
  done
}

while getopts ":m:w:" opt; do
  case $opt in
  m) bootstrap_masters $OPTARG ;;
  w) bootstrap_nodes $OPTARG ;;
  \?)
    echo "Invalid option -$OPTARG" >&2
    usage
    exit
    ;;
  esac
done
