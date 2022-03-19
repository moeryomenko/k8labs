#!/usr/bin/env bash

Help() {
  echo "Helper script for provision local k8s cluster over libvirt(kvm)."
  echo "Syntx: ctl.sh [-h]"
  echo "options:"
  echo "h    Print this help"
  echo
  echo "commands:"
  echo "cluster"
  echo "         bootstrap (-m|-w) - bootstrap vms with specified numbers master and worker nodes"
  echo "         bootstrap -h      - print bootstrap command help message"
  echo
}

while [[ $# -gt 0 ]]; do
  case $1 in
  -h | --help)
    Help
    exit
    ;;
  network)
    kcli create network -c 192.168.111.0/24 k8s-net --domain k8s.local
    exit
    ;;
  install)
    ./install.sh "${@:2}"
    exit
    ;;
  cluster)
    case $2 in
    up)
      # for node in master worker0 worker1 worker2; do kcli start vm $node; done
      echo "up cluster"
      echo
      exit
      ;;
    down)
      # for node in master worker0 worker1 worker2; do kcli stop vm $node; done
      echo "down cluster"
      echo
      exit
      ;;
    bootstrap)
      ./bootstrap.sh "${@:3}"
      exit
      ;;
    *)
      Help
      exit
      ;;
    esac
    exit
    ;;
  *)
    Help
    exit
    ;;
  esac
done

# for node in master worker0 worker1 worker2; do kcli restart vm $node; done
