# k8labs — Kubernetes OS Image Build System
# Targets for Packer VM baking and system/configuration extensions.
# For maximum parallelism, use: make -j$$(nproc) cluster
# This builds base image, extensions, and container simultaneously.

SHELL := /bin/bash
.ONESHELL:

.DEFAULT_GOAL := help

.PHONY: help
help: ## Prints this help message
	@echo "Commands:"
	@grep -F -h '##' $(MAKEFILE_LIST) \
		| grep -F -v fgrep \
		| sort \
		| grep -E '^[a-zA-Z_/.-]+:.*?## .*$$' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'

# --- Base Image ---

FIRMWARE_URL := https://github.com/cloud-hypervisor/edk2/releases/latest/download/CLOUDHV.fd
FIRMWARE_DEST := build/CLOUDHV.fd
FEDORA_CLOUD_URL := https://mirror.arizona.edu/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
FEDORA_CLOUD_CHECKSUM := 28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f
FEDORA_CLOUD_DEST := build/fedora-cloud-base.qcow2
BASE_IMAGE_DEST := build/k8labs-base.qcow2
CLOUDINIT_DISK := build/cloudinit.img
SSH_KEY := build/packer-ssh-key
TAP := packer-tap
PACKER_PLUGIN_SRC := /home/eryoma/workspace/packer-plugin-cloud-hypervisor
PACKER_PLUGIN := ~/.packer.d/plugins/packer-plugin-cloud-hypervisor

.PHONY: plugin
plugin: ## Build and install Cloud-Hypervisor Packer plugin
	@echo '==> Building Cloud-Hypervisor Packer plugin...'
	@if [ ! -f "$(PACKER_PLUGIN)" ]; then \
		mkdir -p ~/.packer.d/plugins; \
		$(MAKE) -C "$(PACKER_PLUGIN_SRC)" build; \
		cp "$(PACKER_PLUGIN_SRC)/packer-plugin-cloud-hypervisor" "$(PACKER_PLUGIN)"; \
		echo '    Installed Packer plugin'; \
	else \
		echo '    Plugin already installed'; \
	fi

.PHONY: base-deps
base-deps: ## Download CLOUDHV.fd firmware and Fedora Cloud Base image
	@echo '==> Downloading Cloud-Hypervisor UEFI firmware (CLOUDHV.fd)...'
	mkdir -p build
	if [ ! -f "$(FIRMWARE_DEST)" ]; then \
		curl -fL -o "$(FIRMWARE_DEST)" "$(FIRMWARE_URL)"; \
		echo '    Downloaded CLOUDHV.fd'; \
	else \
		echo '    CLOUDHV.fd already exists'; \
	fi
	@echo '==> Downloading Fedora Cloud Base image...'
	if [ ! -f "$(FEDORA_CLOUD_DEST)" ]; then \
		curl -fL -o "$(FEDORA_CLOUD_DEST)" "$(FEDORA_CLOUD_URL)"; \
		echo "    Verifying checksum..."; \
		echo "$(FEDORA_CLOUD_CHECKSUM)  $(FEDORA_CLOUD_DEST)" | sha256sum -c - || { \
			echo '    WARNING: checksum mismatch, deleting corrupted download'; \
			rm -f "$(FEDORA_CLOUD_DEST)"; \
			exit 1; \
		}; \
	else \
		echo '    Fedora Cloud image already exists'; \
	fi
	@echo '==> Converting Fedora Cloud image to raw format (CH is more compatible)...'
	RAW_DEST="$(FEDORA_CLOUD_DEST:.qcow2=.raw)"; \
	if [ ! -f "$$RAW_DEST" ]; then \
		qemu-img convert -O raw "$(FEDORA_CLOUD_DEST)" "$$RAW_DEST"; \
		echo '    Converted to raw format'; \
	else \
		echo '    Raw image already exists'; \
	fi

.PHONY: base-ssh-key
base-ssh-key: ## Generate SSH keypair for Packer provisioning
	@echo '==> Generating Packer SSH keypair...'
	if [ ! -f "$(SSH_KEY)" ]; then \
		ssh-keygen -t ed25519 -f "$(SSH_KEY)" -N "" -q; \
		echo '    Generated $(SSH_KEY)'; \
	else \
		echo '    SSH key already exists'; \
	fi

.PHONY: base-cloudinit
base-cloudinit: base-ssh-key ## Generate cloud-init CIDATA disk for Packer build
	@echo '==> Generating cloud-init disk for Packer build...'
	PUB_KEY=$$(cat "$(SSH_KEY).pub"); \
	sed "s|__SSH_PUBLIC_KEY__|$$PUB_KEY|" packer/cloud-init/user-data > /tmp/base-user-data; \
	scripts/create-cloudinit.sh \
		--user-data /tmp/base-user-data \
		--meta-data packer/cloud-init/meta-data \
		--network-config packer/cloud-init/network-config \
		--output "$(CLOUDINIT_DISK)"

.PHONY: base
base: base-deps base-cloudinit ## Build base image via Packer
	@if [ -f "$(BASE_IMAGE_DEST)" ]; then \
		echo 'Base image already exists: $(BASE_IMAGE_DEST)'; \
		echo 'To force a rebuild, run: make base-rebuild'; \
		exit 0; \
	fi
	@echo 'Building base image via Packer...'
	rm -rf build/base-image
	(cd packer && PACKER_PLUGIN_PATH=~/.packer.d/plugins \
		packer build -var-file=vars.pkrvars.hcl -only=cloud-hypervisor.base .)
	@echo 'Copying base image to build/ for Terraform consumption...'
	@set -euo pipefail; \
	ARTIFACT=$$(find packer/output-base -maxdepth 1 -type f ! -name '*.lock' | head -1); \
	if [ -z "$$ARTIFACT" ]; then \
		echo 'ERROR: no Packer artifact found in packer/output-base/' >&2; \
		exit 1; \
	fi; \
	mkdir -p build; \
	qemu-img convert -O qcow2 "$$ARTIFACT" "$(BASE_IMAGE_DEST)"; \
	echo "    Converted $$ARTIFACT -> $(BASE_IMAGE_DEST)"

.PHONY: base-rebuild
base-rebuild: base-deps base-cloudinit ## Force rebuild of the base image
	@echo 'Forcing base image rebuild via Packer...'
	rm -f "$(BASE_IMAGE_DEST)"
	rm -rf build/base-image
	(cd packer && PACKER_PLUGIN_PATH=~/.packer.d/plugins \
		packer build -var-file=vars.pkrvars.hcl -only=cloud-hypervisor.base .)
	@set -euo pipefail; \
	ARTIFACT=$$(find packer/output-base -maxdepth 1 -type f ! -name '*.lock' | head -1); \
	if [ -z "$$ARTIFACT" ]; then \
		echo 'ERROR: no Packer artifact found in packer/output-base/' >&2; \
		exit 1; \
	fi; \
	mkdir -p build; \
	qemu-img convert -O qcow2 "$$ARTIFACT" "$(BASE_IMAGE_DEST)"; \
	echo "    Converted $$ARTIFACT -> $(BASE_IMAGE_DEST)"

# --- Networking ---

.PHONY: network-up
network-up: ## Configure bridge+TAP networking + NAT/forwarding + DNS forwarder
	@echo '==> Installing systemd-networkd bridge and TAP configs...'
	sudo cp network/k8sbr0.netdev network/k8sbr0.network /etc/systemd/network/
	sudo cp network/k8s-cp1.netdev network/k8s-cp1.network /etc/systemd/network/
	sudo cp network/k8s-w1.netdev network/k8s-w1.network /etc/systemd/network/
	sudo cp network/packer-tap.netdev network/packer-tap.network /etc/systemd/network/
	sudo mkdir -p /etc/systemd/networkd.conf.d
	sudo cp network/90-k8slab-foreign-rules.conf /etc/systemd/networkd.conf.d/
	sudo systemctl reload-or-restart systemd-networkd
	@echo '==> Loading nftables NAT/forwarding rules...'
	sudo nft -f network/nat.nft
	@echo '==> Enabling DNS forwarder on bridge (dnsmasq)...'
	sudo mkdir -p /etc/dnsmasq.d
	sudo cp network/dnsmasq-k8sbr0.conf /etc/dnsmasq.d/k8sbr0.conf
	@echo '==> Activating dnsmasq conf-dir=/etc/dnsmasq.d/,*.conf...'
	@set -euo pipefail; \
	if sudo grep -qE '^conf-dir=.*/etc/dnsmasq\.d/' /etc/dnsmasq.conf; then \
		echo '    conf-dir already active for /etc/dnsmasq.d/, skipping'; \
	elif sudo grep -qE '^conf-dir=' /etc/dnsmasq.conf; then \
		echo 'ERROR: active conf-dir= line points elsewhere; refusing to modify /etc/dnsmasq.conf' >&2; \
		exit 1; \
	elif sudo grep -qE '^#conf-dir=/etc/dnsmasq\.d/,.*\.conf' /etc/dnsmasq.conf; then \
		sudo sed -i 's|^#conf-dir=/etc/dnsmasq\.d/,.*\.conf|conf-dir=/etc/dnsmasq.d/,*.conf|' /etc/dnsmasq.conf; \
		echo '    uncommented existing conf-dir=/etc/dnsmasq.d/,*.conf line'; \
	else \
		echo 'conf-dir=/etc/dnsmasq.d/,*.conf' | sudo tee -a /etc/dnsmasq.conf >/dev/null; \
		echo '    appended conf-dir=/etc/dnsmasq.d/,*.conf to /etc/dnsmasq.conf'; \
	fi
	sudo systemctl enable --now dnsmasq 2>/dev/null || true
	sudo systemctl restart dnsmasq
	@echo '==> Network ready (bridge k8sbr0, DHCP 192.168.124.20-200, DNS 192.168.124.1)'

.PHONY: network-down
network-down: ## Remove networking configs and scoped k8slab nftables table
	@echo '==> Removing network configs...'
	sudo rm -f /etc/systemd/network/k8sbr0.netdev /etc/systemd/network/k8sbr0.network
	sudo rm -f /etc/systemd/network/k8s-cp1.netdev /etc/systemd/network/k8s-cp1.network
	sudo rm -f /etc/systemd/network/k8s-w1.netdev /etc/systemd/network/k8s-w1.network
	sudo rm -f /etc/systemd/network/packer-tap.netdev /etc/systemd/network/packer-tap.network
	sudo rm -f /etc/dnsmasq.d/k8sbr0.conf
	sudo systemctl restart systemd-networkd
	@echo '==> Removing k8slab nftables table...'
	sudo nft destroy table inet k8slab
	@echo '==> Network teardown complete'

# --- System Extensions ---

SYSEXT_NAMES := kubelet cri-o crun cni etcd kubernetes-cp perfetto

.PHONY: $(addprefix sysext/,$(SYSEXT_NAMES)) sysexts download-sysexts

download-sysexts: ## Download pre-built sysext binaries from upstream
	extensions/download-sysexts.sh

sysext/kubelet:
	@echo 'Downloading kubelet binary...'
	extensions/download-sysexts.sh kubelet
	@echo 'Packaging kubelet sysext...'
	extensions/build.sh sysext sysext/kubelet kubelet

sysext/cri-o:
	@echo 'Downloading cri-o binaries...'
	extensions/download-sysexts.sh cri-o
	@echo 'Packaging cri-o sysext...'
	extensions/build.sh sysext sysext/cri-o cri-o

sysext/crun:
	@echo 'Downloading crun binary...'
	extensions/download-sysexts.sh crun
	@echo 'Packaging crun sysext...'
	extensions/build.sh sysext sysext/crun crun

sysext/cni:
	@echo 'Downloading CNI plugins...'
	extensions/download-sysexts.sh cni
	@echo 'Packaging CNI sysext...'
	extensions/build.sh sysext sysext/cni cni

sysext/etcd: ## Build etcd sysext (etcd + etcdctl + systemd unit)
	@echo 'Downloading etcd binaries...'
	extensions/download-sysexts.sh etcd
	@echo 'Packaging etcd sysext...'
	extensions/build.sh sysext sysext/etcd etcd

sysext/kubernetes-cp: ## Build kubernetes-cp sysext (apiserver, cm, scheduler, kubectl)
	@echo 'Downloading Kubernetes control-plane binaries...'
	extensions/download-sysexts.sh kubernetes-cp
	@echo 'Packaging kubernetes-cp sysext...'
	extensions/build.sh sysext sysext/kubernetes-cp kubernetes-cp

sysext/perfetto: ## Build perfetto sysext (tracebox)
	@echo 'Downloading tracebox binary...'
	extensions/download-sysexts.sh perfetto
	@echo 'Packaging perfetto sysext...'
	extensions/build.sh sysext sysext/perfetto perfetto

sysexts: ## Build all sysext extensions in parallel
	@echo 'Building all sysexts in parallel...'
	+$(MAKE) -j$$(nproc 2>/dev/null || echo 2) $(addprefix sysext/,$(SYSEXT_NAMES))

# --- Config Extensions ---

CONFEXT_NAMES := worker cri-o kubernetes etcd kubernetes-cp

.PHONY: $(addprefix confext/,$(CONFEXT_NAMES)) confexts

confext/worker: ## Build confext worker configuration overlay
	@echo 'Building confext worker...'
	extensions/build.sh confext confext/worker confext-worker

confext/cri-o: ## Build confext cri-o configuration overlay
	@echo 'Building confext cri-o...'
	extensions/build.sh confext confext/cri-o confext-cri-o

confext/kubernetes: ## Build confext kubernetes configuration overlay
	@echo 'Building confext kubernetes...'
	extensions/build.sh confext confext/kubernetes confext-kubernetes

confext/etcd: ## Build confext etcd configuration overlay
	@echo 'Building confext etcd...'
	extensions/build.sh confext confext/etcd confext-etcd

confext/kubernetes-cp: ## Build confext kubernetes-cp configuration overlay
	@echo 'Building confext kubernetes-cp...'
	extensions/build.sh confext confext/kubernetes-cp confext-kubernetes-cp

confexts: ## Build all confext extensions in parallel
	@echo 'Building all confexts in parallel...'
	+$(MAKE) -j$$(nproc 2>/dev/null || echo 2) $(addprefix confext/,$(CONFEXT_NAMES))

# --- Combined Extensions ---

.PHONY: extensions
extensions: sysexts confexts ## Build all extensions (sysexts + confexts)
	@echo 'All extensions built.'

# --- Full Build ---

.PHONY: all
all: base extensions ## Build base image + all extensions
	@echo 'Full build complete.'

# --- Terraform ---

TFVARS := build/deploy.tfvars

.PHONY: tfvars
tfvars: ## Generate terraform.tfvars from defaults for deployment
	@echo 'Generating deployment tfvars...'
	@if [ ! -f "$(TFVARS)" ]; then \
		echo '==> Creating $(TFVARS) from example...'; \
		PUB_KEY="$$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || echo 'YOUR_SSH_PUB_KEY')"; \
		sed "s|ssh-ed25519 AAAAC3.*|$$PUB_KEY|" \
			terraform/terraform.tfvars.example > "$(TFVARS)"; \
		echo '    Edit $(TFVARS) to adjust VM count and MAC addresses'; \
	fi

.PHONY: vm-disks
vm-disks: ## Create per-VM root disk images from the base image (qcow2, no backing chain)
	@echo 'Creating VM root disks from base image...'
	@set -euo pipefail; \
	if [ ! -f "$(BASE_IMAGE_DEST)" ]; then \
		echo 'ERROR: base image not found: $(BASE_IMAGE_DEST) (run make base first)' >&2; \
		exit 1; \
	fi; \
	if [ ! -f "$(TFVARS)" ]; then \
		echo 'ERROR: $(TFVARS) not found (run make tfvars first)' >&2; \
		exit 1; \
	fi; \
	mkdir -p build/vm-disks; \
	VDIR="$$(pwd)/build/vm-disks"; \
	BASE="$$(pwd)/$(BASE_IMAGE_DEST)"; \
	{ \
		grep -A8 '^control_plane' "$(TFVARS)" | grep -E 'name|disk' | tr -d ' ,"' | awk -F= '{print $$2}'; \
		grep -A8 'name = "w' "$(TFVARS)" | grep -E 'name|disk' | tr -d ' ,"' | awk -F= '{print $$2}'; \
	} | paste - - 2>/dev/null | while read -r node size; do \
		disk="$${VDIR}/$${node}-root.qcow2"; \
		if [ ! -f "$$disk" ]; then \
			echo "  Creating $$disk ($${size} MiB)..."; \
			qemu-img convert -O qcow2 "$$BASE" "$$disk"; \
			qemu-img resize "$$disk" "$${size}M" >/dev/null; \
		else \
			echo "  $$disk already exists"; \
		fi; \
	done; \
	echo '  VM disks ready'

.PHONY: deploy
deploy: network-up tfvars vm-disks ## Apply Terraform/OpenTofu infrastructure
	@echo 'Applying Terraform infrastructure...'
	tofu -chdir=terraform apply -auto-approve -var-file="../$(TFVARS)"

.PHONY: destroy
destroy: tfvars ## Destroy all VMs
	@echo 'Destroying VMs...'
	tofu -chdir=terraform destroy -auto-approve -var-file="../$(TFVARS)"

# --- VM Lifecycle ---

.PHONY: start stop
start: ## Start all VMs via ch-remote API (or curl fallback)
	@set -euo pipefail; \
	for sock in /tmp/ch-tf-*/api.sock; do \
		[ -S "$$sock" ] || continue; \
		name=$$(basename "$$(dirname "$$sock")"); \
		if command -v ch-remote &>/dev/null; then \
			echo "  $$name: starting..."; \
			ch-remote --api-socket "$$sock" resume-vm 2>/dev/null || true; \
		else \
			echo "  $$name: starting via API..."; \
			curl -s --unix-socket "$$sock" -X PUT "http://localhost/api/v1/vm.boot" >/dev/null 2>&1 || echo "  WARNING: could not start $$name"; \
		fi; \
	done

stop: ## Gracefully stop all VMs via ACPI shutdown
	@set -euo pipefail; \
	for sock in /tmp/ch-tf-*/api.sock; do \
		[ -S "$$sock" ] || continue; \
		name=$$(basename "$$(dirname "$$sock")"); \
		if command -v ch-remote &>/dev/null; then \
			echo "  $$name: shutting down..."; \
			ch-remote --api-socket "$$sock" shutdown-vm 2>/dev/null && echo "    shutdown sent" || echo "    shutdown failed"; \
		else \
			echo "  $$name: shutting down via API..."; \
			curl -s --unix-socket "$$sock" -X PUT "http://localhost/api/v1/vm.shutdown" >/dev/null 2>&1 || echo "  WARNING: could not shutdown $$name"; \
		fi; \
	done

.PHONY: destroy-full
destroy-full: destroy ## Destroy all artifacts (VMs + certs + inventory + kubeconfig)
	@echo '==> Preparing to clean up generated artifacts...'
	@if [ "${YES}" != "1" ]; then \
		read -t 30 -r -p "Remove all generated certificates and kubeconfigs? [y/N] " confirm; \
		case "$$confirm" in \
			[yY]|[yY][eE][sS]) ;; \
			*) echo "  Aborted."; exit 1 ;; \
		esac; \
	fi
	@echo '==> Removing generated certificates...'
	@find certs/ -type f ! -name '.gitkeep' -delete
	@find certs/ -type d -empty -delete
	@touch certs/.gitkeep
	@echo '==> Removing ansible/inventory/inventory.json...'
	@rm -f ansible/inventory/inventory.json
	@echo '==> Removing root kubeconfig...'
	@rm -f kubeconfig
	@echo '==> Cleanup complete.'

# --- Ansible Container ---

ANSIBLE_IMAGE := localhost/ansible-podman
ANSIBLE_DIR := ansible
ANSIBLE_RUN := podman run --rm --network host \
	--cap-add=NET_ADMIN \
	-v $(PWD):/workspace:z \
	-v $(HOME)/.ssh:/root/.ssh:ro,z \
	-v $(SSH_AUTH_SOCK):/ssh-agent:z \
	-e SSH_AUTH_SOCK=/ssh-agent \
	-e ANSIBLE_ROLES_PATH=/workspace/$(ANSIBLE_DIR)/roles \
	-e ANSIBLE_INVENTORY=/workspace/$(ANSIBLE_DIR)/inventory/inventory.json \
	-w /workspace \
	$(ANSIBLE_IMAGE):latest

.PHONY: container
container: .container.stamp ## Build Ansible runner container image

.container.stamp: container/Containerfile
	@echo 'Building Ansible runner container image...'
	podman build -t $(ANSIBLE_IMAGE):latest -f container/Containerfile
	@touch .container.stamp

.PHONY: inventory
inventory: ## Test dynamic inventory output
	@echo 'Testing Ansible dynamic inventory...'
	ansible/inventory/inventory.py --list | python3 -m json.tool

.PHONY: deploy-extensions
deploy-extensions: ## Deploy sysext/confext extensions to all VMs (Ansible)
	@echo 'Deploying extensions via Ansible...'
	$(ANSIBLE_DIR)/inventory/inventory.py --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/deploy-extensions.yml

.PHONY: certs
certs: ## Generate TLS certificates via Ansible (community.crypto)
	@echo 'Generating TLS certificates (Ansible)...'
	$(ANSIBLE_DIR)/inventory/inventory.py --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/bootstrap.yml --tags certs

.PHONY: bootstrap
bootstrap: ## Bootstrap Kubernetes cluster via Ansible (KTHW + Cilium + L2)
	@echo 'Bootstrapping Kubernetes cluster via Ansible...'
	@echo '  Prerequisites: make deploy must have been run, SSH keys injected'
	@echo '  Generating Ansible inventory from tofu state + dnsmasq leases...'
	$(ANSIBLE_DIR)/inventory/inventory.py --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/bootstrap.yml

.PHONY: wait-ips
wait-ips: ## Wait for ALL VMs to get DHCP leases (reads systemd-networkd lease file by MAC)
	@echo '  Waiting for all VM IP addresses (DHCP leases)...'
	@set -euo pipefail; \
	NODES_JSON=$$(tofu -chdir=terraform output -json nodes 2>/dev/null); \
	if [ -z "$$NODES_JSON" ]; then echo "  ERROR: tofu output failed" >&2; exit 1; fi; \
	total=$$(echo "$$NODES_JSON" | jq length); \
	for i in $$(seq 1 60); do \
		cp_ip=""; w_ips=""; found_count=0; \
		while IFS=, read -r name mac; do \
			ip=$$(scripts/vm-ip.sh "$$mac" 2>/dev/null || true); \
			if [ -n "$$ip" ]; then \
				if [ -z "$$cp_ip" ]; then cp_ip="$$ip"; \
				else w_ips="$$w_ips $$ip"; fi; \
				found_count=$$((found_count + 1)); \
			fi; \
		done < <(echo "$$NODES_JSON" | jq -r '.[] | "\(.name),\(.mac)"'); \
		if [ "$$found_count" -ge "$$total" ]; then \
			echo "  All $$total VMs ready after $$i cycles -- CP: $$cp_ip, Workers:$$w_ips"; \
			exit 0; \
		fi; \
		echo "  waiting ($$i/60)... CP=$${cp_ip:-none} workers=$$found_count/$$total"; \
		sleep 5; \
	done; \
	echo "  ERROR: VMs did not get IPs within timeout" >&2; \
	exit 1

.PHONY: wait-ssh
wait-ssh: ## Wait for SSH to become available on all VMs (reads DHCP leases for IPs)
	@echo '  Waiting for SSH connectivity on all VMs...'
	@set -euo pipefail; \
	resolve_ip() { \
		local vname="$$1"; \
		local mac=$$(tofu -chdir=terraform output -json nodes 2>/dev/null | jq -r ".[] | select(.name==\"$$vname\") | .mac"); \
		scripts/vm-ip.sh "$$mac" 2>/dev/null || true; \
	}; \
	check_ssh() { \
		local ip="$$1" name="$$2"; \
		for i in $$(seq 1 30); do \
			ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
				-o ConnectTimeout=2 -o BatchMode=yes root@$$ip true 2>/dev/null && { \
				echo "  SSH ready on $$name ($$ip)"; \
				return 0; \
			}; \
			sleep 5; \
		done; \
		echo "  ERROR: SSH not available on $$name ($$ip) after 30 attempts" >&2; \
		return 1; \
	}; \
	raw_names=$$(tofu -chdir=terraform output -json nodes 2>/dev/null); \
	if [ -z "$$raw_names" ]; then echo "  ERROR: no nodes from tofu output" >&2; exit 1; fi; \
	node_names=$$(echo "$$raw_names" | jq -r '.[].name'); \
	ips_arr=(); names_arr=(); \
	for name in $$node_names; do \
		ip=$$(resolve_ip "$$name"); \
		names_arr+=("$$name"); ips_arr+=("$$ip"); \
	done; \
	pids=""; has_error=0; \
	for idx in "$${!names_arr[@]}"; do \
		name="$${names_arr[idx]}"; ip="$${ips_arr[idx]:-}"; \
		if [ -z "$$ip" ]; then echo "  WARNING: no IP for $$name" >&2; continue; fi; \
		(check_ssh "$$ip" "$$name") & pids="$$pids $$!"; \
	done; \
	for pid in $$pids; do [ -z "$$pid" ] && continue; wait "$$pid" || has_error=1; done; \
	if [ "$$has_error" -ne 0 ]; then echo "  ERROR: one or more VMs failed SSH check" >&2; exit 1; fi

.PHONY: prereq
prereq: ## Validate required build tools (tofu, cloud-hypervisor, podman, openssl, nft, systemctl)
	@set -euo pipefail; \
	fail=0; \
	for cmd in tofu cloud-hypervisor podman openssl nft systemctl; do \
		if ! command -v $$cmd &>/dev/null; then \
			echo "ERROR: required tool '$$cmd' not found" >&2; \
			fail=1; \
		fi; \
	done; \
	exit $$fail

.PHONY: cluster
cluster: network-up base tfvars vm-disks sysexts confexts container ## Full pipeline: network -> base -> extensions -> container -> deploy -> bootstrap
	@set -euo pipefail; \
	echo 'Bootstrapping cluster...'; \
	echo '  Step 1: Deploy VMs (tofu apply)...'; \
	tofu -chdir=terraform apply -auto-approve -var-file="../build/deploy.tfvars"; \
	echo '  Step 2: Wait for VM IP addresses...'; \
	$(MAKE) wait-ips; \
	echo '  Step 3: Wait for SSH connectivity on all VMs...'; \
	$(MAKE) wait-ssh; \
	echo '  Step 4: Generate Ansible inventory...'; \
	$(ANSIBLE_DIR)/inventory/inventory.py --list > $(ANSIBLE_DIR)/inventory/inventory.json; \
	echo '  Step 5: Ansible bootstrap (extensions + certs + KTHW + Cilium)...'; \
	$(ANSIBLE_RUN) ansible-playbook -i $(ANSIBLE_DIR)/inventory/inventory.json \
		$(ANSIBLE_DIR)/playbooks/bootstrap.yml; \
	echo '  Step 7: Fetch kubeconfig...'; \
	$(MAKE) kubeconfig; \
	echo '  Step 8: Wait for control plane node Ready (up to 10m)...'; \
	for i in $$(seq 1 120); do \
		if kubectl --kubeconfig=kubeconfig wait --for=condition=Ready node/cp1 --timeout=10s 2>/dev/null; then \
			echo "  Control plane node cp1 is Ready after $$((i * 5))s"; \
			break; \
		fi; \
		echo "  waiting for cp1 Ready ($$i/120)..."; \
		sleep 5; \
	done; \
	if ! kubectl --kubeconfig=kubeconfig get node cp1 2>/dev/null | grep -q "Ready"; then \
		echo "  WARNING: control plane node cp1 did not become Ready within timeout" >&2; \
		echo "  Check node status: kubectl --kubeconfig=kubeconfig get nodes" >&2; \
	else \
		echo "  Control plane node cp1 is Ready"; \
	fi; \
	echo 'Cluster build and bootstrap complete.'

# --- kubeconfig ---

.PHONY: kubeconfig update-kubeconfig
kubeconfig: ## Fetch DHCP-resistant kubeconfig from control-plane node
	@set -euo pipefail; \
	CP_MAC=$$(tofu -chdir=terraform output -json nodes 2>/dev/null | jq -r '.[0].mac'); \
	cp_ip=$$(scripts/vm-ip.sh "$$CP_MAC" 2>/dev/null || true); \
	if [ -z "$$cp_ip" ]; then \
		echo "ERROR: Cannot determine control-plane IP. Ensure VMs are deployed (make deploy)." >&2; \
		exit 1; \
	fi; \
	echo "  Fetching kubeconfig from CP ($${cp_ip})..."; \
	ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o ConnectTimeout=5 -o BatchMode=yes \
		root@$${cp_ip} "cat /etc/kubernetes/admin.kubeconfig" > .kubeconfig.tmp; \
	{ echo "# WARNING: This kubeconfig contains the control-plane IP directly and will break"; \
	  echo "# if DHCP renews and the control-plane node gets a new IP address."; \
	  echo "# To refresh, run: make update-kubeconfig"; \
	  cat .kubeconfig.tmp; } > kubeconfig; \
	rm -f .kubeconfig.tmp; \
	chmod 600 kubeconfig; \
	echo "  kubeconfig saved to ./kubeconfig (mode 600)"

update-kubeconfig: kubeconfig ## Alias for kubeconfig — explicitly signals refresh

# --- Smoke Test ---

# smoke-test validates cluster health after 'make cluster'.
# Checks: nodes Ready, kube-system pods Running, Cilium health, test pod scheduling.
KUBECONFIG := kubeconfig

.PHONY: smoke-test
smoke-test:
	@set -euo pipefail; \
	POD_NAME="smoke-test-$$(date +%s)"; \
	trap 'kubectl --kubeconfig $(KUBECONFIG) delete pod "$$POD_NAME" --ignore-not-found --now 2>/dev/null || true' EXIT; \
	fail=0; \
	echo "=== smoke-test: validating cluster health ==="; \
	echo "--- check 1: nodes Ready ---"; \
	NODES=$$(kubectl --kubeconfig $(KUBECONFIG) get nodes --no-headers 2>/dev/null); \
	if [ -z "$$NODES" ]; then \
		echo "  FAIL: no nodes found"; \
		fail=1; \
	else \
		NOT_READY=$$(echo "$$NODES" | awk '{if($$2!="Ready"){print $$1}}'); \
		if [ -n "$$NOT_READY" ]; then \
			echo "  FAIL: nodes not Ready: $$NOT_READY"; \
			kubectl --kubeconfig $(KUBECONFIG) get nodes; \
			fail=1; \
		else \
			echo "  PASS: all nodes Ready"; \
		fi; \
	fi; \
	echo "--- check 2: kube-system pods Running ---"; \
	NOT_RUNNING=$$(kubectl --kubeconfig $(KUBECONFIG) get pods -n kube-system --no-headers 2>/dev/null | awk '{if($$3!="Running"&&$$3!="Completed"){print $$1":"$$3}}'); \
	if [ -n "$$NOT_RUNNING" ]; then \
		echo "  FAIL: some kube-system pods not Running: $$NOT_RUNNING"; \
		kubectl --kubeconfig $(KUBECONFIG) get pods -n kube-system; \
		fail=1; \
	else \
		echo "  PASS: kube-system pods Running"; \
	fi; \
	echo "--- check 3: Cilium health (NetworkUnavailable=False) ---"; \
	NET_AVAIL=$$(kubectl --kubeconfig $(KUBECONFIG) get nodes -o jsonpath='{.items[*].status.conditions[?(@.type=="NetworkUnavailable")].status}' 2>/dev/null); \
	if [ -n "$$NET_AVAIL" ]; then \
		all_false=1; \
		for s in $$NET_AVAIL; do \
			if [ "$$s" != "False" ]; then all_false=0; break; fi; \
		done; \
		if [ "$$all_false" -eq 1 ]; then \
			echo "  PASS: Cilium healthy on all nodes (NetworkUnavailable=False)"; \
		else \
			echo "  FAIL: some nodes have network unavailable"; \
			fail=1; \
		fi; \
	else \
		echo "  FAIL: no NetworkUnavailable node condition found"; \
		fail=1; \
	fi; \
	echo "--- check 3b: kubectl exec into Cilium pod ---"; \
	CILIUM_POD=$$(kubectl --kubeconfig $(KUBECONFIG) -n kube-system get pods -l k8s-app=cilium -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); \
	if [ -n "$$CILIUM_POD" ]; then \
		if kubectl --kubeconfig $(KUBECONFIG) -n kube-system exec "$$CILIUM_POD" -c cilium-agent -- cilium status --brief 2>/dev/null; then \
			echo "  PASS: Cilium exec works"; \
		else \
			echo "  WARN: Cilium exec failed (RBAC may need system:kube-apiserver-proxy binding)"; \
		fi; \
	else \
		echo "  SKIP: no Cilium pod found"; \
	fi; \
	echo "--- check 5a: GatewayClass exists ---"; \
	if kubectl --kubeconfig $(KUBECONFIG) get gatewayclass cilium &>/dev/null; then \
		echo "  PASS: GatewayClass cilium exists"; \
	else \
		echo "  FAIL: GatewayClass cilium not found"; \
		fail=1; \
	fi; \
	echo "--- check 5b: Gateway programmed ---"; \
	GW_STATUS=$$(kubectl --kubeconfig $(KUBECONFIG) get gateway -n default cilium-gw -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null); \
	if [ "$$GW_STATUS" = "True" ]; then \
		echo "  PASS: Gateway cilium-gw is Programmed"; \
	else \
		echo "  FAIL: Gateway cilium-gw not Programmed (status=$${GW_STATUS:-unknown})"; \
		fail=1; \
	fi; \
	echo "--- check 5c: HTTPRoute accepted ---"; \
	HR_STATUS=$$(kubectl --kubeconfig $(KUBECONFIG) get httproute -n default http-echo -o jsonpath='{.status.parents[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null); \
	if [ "$$HR_STATUS" = "True" ]; then \
		echo "  PASS: HTTPRoute http-echo is Accepted"; \
	else \
		echo "  FAIL: HTTPRoute http-echo not Accepted (status=$${HR_STATUS:-unknown})"; \
		fail=1; \
	fi; \
	echo "--- check 4: schedule test pod ---"; \
	if kubectl --kubeconfig $(KUBECONFIG) run "$$POD_NAME" --image=nginx --restart=Never --port=80 2>/dev/null; then \
		for i in $$(seq 1 15); do \
			status=$$(kubectl --kubeconfig $(KUBECONFIG) get pod "$$POD_NAME" -o jsonpath='{.status.phase}' 2>/dev/null); \
			if [ "$$status" = "Running" ]; then \
				echo "  PASS: test pod reached Running"; \
				break; \
			fi; \
			sleep 2; \
		done; \
		if [ "$$status" != "Running" ]; then \
			echo "  FAIL: test pod did not reach Running"; \
			kubectl --kubeconfig $(KUBECONFIG) get pod "$$POD_NAME"; \
			fail=1; \
		fi; \
	else \
		echo "  FAIL: could not create test pod"; \
		fail=1; \
	fi; \
	kubectl --kubeconfig $(KUBECONFIG) delete pod "$$POD_NAME" --now --ignore-not-found 2>/dev/null || true; \
	echo "=== smoke-test complete ==="; \
	if [ "$$fail" -eq 0 ]; then \
		echo "PASS: all checks passed"; \
	else \
		echo "FAIL: one or more checks failed"; \
	fi; \
	exit $$fail

# --- Metrics Server ---

.PHONY: metrics-server
metrics-server: ## Deploy metrics-server for kubectl top
	@echo 'Deploying metrics-server...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f cilium/metrics-server.yaml
	@echo 'Waiting for metrics-server to be ready...'
	kubectl --kubeconfig $(KUBECONFIG) -n kube-system wait --for=condition=Available deployment/metrics-server --timeout=60s
	@echo 'metrics-server ready. Run: kubectl top nodes'

# --- Cleanup ---

.PHONY: clean
clean: ## Remove build artifacts
	@echo 'Removing build artifacts...'
	rm -rf build/ extensions/release/*.raw

# --- Validation ---

.PHONY: validate-packer
validate-packer: ## Validate Packer template syntax
	@echo 'Validating Packer template syntax...'
	# Some vars don't have defaults (firmware_path, etc.) -- checked at build time.
	cd packer && packer validate -var="firmware_path=/tmp/test" -var="cloud_image_path=/tmp/test" -var="cloudinit_disk_path=/tmp/test" -var="ssh_private_key_file=/tmp/test" . 2>&1 || true

.PHONY: validate-terraform
validate-terraform: ## Validate Terraform/OpenTofu configuration
	@echo 'Validating Terraform/OpenTofu configuration...'
	tofu -chdir=terraform validate

.PHONY: validate
validate: validate-packer validate-terraform ## Run all validations (packer + terraform)
	@echo 'All validations passed.'
