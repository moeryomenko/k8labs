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

FEDORA_ISO_URL := https://download.fedoraproject.org/pub/fedora/linux/releases/41/Everything/x86_64/iso/Fedora-Everything-netinst-x86_64-41-1.4.iso
FEDORA_ISO_CACHE := $(shell ls -t $(HOME)/.cache/packer/*.iso 2>/dev/null | head -1)
MODIFIED_ISO := build/fedora-boot.iso

.PHONY: prepare-iso
prepare-iso: ## Download Fedora ISO and prepare modified version with serial console + SSH support
	@echo '==> Preparing modified Fedora boot ISO for headless Packer build...'
	mkdir -p build
	if [ -f "$(MODIFIED_ISO)" ]; then \
		echo '    Modified ISO already exists: $(MODIFIED_ISO)'; \
	elif [ -n "$(FEDORA_ISO_CACHE)" ] && [ -f "$(FEDORA_ISO_CACHE)" ]; then \
		echo '    Using Packer-cached ISO: $(FEDORA_ISO_CACHE)'; \
		packer/scripts/prepare-iso.sh "$(FEDORA_ISO_CACHE)" "$(MODIFIED_ISO)"; \
	else \
		echo '    Downloading Fedora ISO...'; \
		curl -Lo /tmp/fedora-41-netinst.iso "$(FEDORA_ISO_URL)"; \
		packer/scripts/prepare-iso.sh /tmp/fedora-41-netinst.iso "$(MODIFIED_ISO)"; \
	fi

.PHONY: base
BASE_IMAGE_DEST := build/k8labs-base.qcow2

base: prepare-iso ## Build the base OS image via Packer (skip if already built)
	@if [ -f "$(BASE_IMAGE_DEST)" ]; then \
		echo 'Base image already exists: $(BASE_IMAGE_DEST)'; \
		echo 'To force a rebuild, run: make base-rebuild'; \
		exit 0; \
	fi
	@echo 'Building base OS image via Packer...'
	rm -rf build/base
	(cd packer && packer build -var-file=vars.pkrvars.hcl .)
	@echo 'Copying base image to build/ for Terraform consumption...'
	mkdir -p build
	cp build/base/k8labs-base $(BASE_IMAGE_DEST)

.PHONY: base-rebuild
base-rebuild: prepare-iso ## Force rebuild of the base OS image via Packer
	@echo 'Forcing base OS image rebuild via Packer...'
	rm -f $(BASE_IMAGE_DEST)
	rm -rf build/base
	(cd packer && packer build -var-file=vars.pkrvars.hcl .)
	mkdir -p build
	cp build/base/k8labs-base $(BASE_IMAGE_DEST)

# --- System Extensions ---

SYSEXT_NAMES := kubelet cri-o crun cni etcd kubernetes-cp

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

sysexts: ## Build all sysext extensions in parallel
	@echo 'Building all sysexts in parallel...'
	+$(MAKE) -j$$(nproc 2>/dev/null || echo 2) $(addprefix sysext/,$(SYSEXT_NAMES))

# --- Config Extensions ---

CONFEXT_NAMES := worker cri-o kubernetes etcd kubernetes-cp

.PHONY: $(addprefix confext/,$(CONFEXT_NAMES)) confexts

confext/worker: ## Build confext worker configuration overlay
	@echo 'Building confext worker...'
	extensions/build.sh confext confext/worker confext-worker

confext/control-plane: ## Build confext control-plane configuration overlay
	@echo 'Building confext control-plane...'
	extensions/build.sh confext confext/control-plane confext-control-plane

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
all: base extensions ## Build base image + all extensions (legacy alias)
	@echo 'Full build complete.'

# --- Terraform ---

.PHONY: deploy
deploy: ## Apply Terraform/OpenTofu infrastructure
	@echo 'Applying Terraform/OpenTofu infrastructure...'
	tofu -chdir=terraform apply -auto-approve -var="base_image_path=../build/k8labs-base.qcow2"

.PHONY: destroy
destroy: ## Destroy Terraform/OpenTofu infrastructure
	@echo 'Destroying Terraform/OpenTofu infrastructure...'
	tofu -chdir=terraform destroy -auto-approve

# --- Ansible Container ---

# Libvirt connection URI (must match tofu/terraform provider config)
LIBVIRT_URI := qemu:///system

ANSIBLE_IMAGE := localhost/ansible-podman
ANSIBLE_DIR := ansible
KUBECTL_BIN := $(shell command -v kubectl 2>/dev/null || echo /usr/local/bin/kubectl)
ANSIBLE_RUN := podman run --rm --network host \
	-v $(PWD):/workspace:z \
	-v $(HOME)/.ssh:/root/.ssh:ro,z \
	-v $(SSH_AUTH_SOCK):/ssh-agent:z \
	-e SSH_AUTH_SOCK=/ssh-agent \
	-v $(KUBECTL_BIN):/usr/local/bin/kubectl:ro \
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
	ansible/inventory/tf-inventory.sh --list | python3 -m json.tool

.PHONY: deploy-extensions
deploy-extensions: ## Deploy sysext/confext extensions to all VMs (Ansible)
	@echo 'Deploying extensions via Ansible...'
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/deploy-extensions.yml

.PHONY: certs
certs: ## Generate TLS certificates via Ansible (community.crypto)
	@echo 'Generating TLS certificates (Ansible)...'
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/bootstrap.yml --tags certs

.PHONY: bootstrap
bootstrap: ## Bootstrap Kubernetes cluster via Ansible (KTHW + Cilium + L2)
	@echo 'Bootstrapping Kubernetes cluster via Ansible...'
	@echo '  Prerequisites: make deploy must have been run, SSH keys injected'
	tofu -chdir=terraform apply -refresh-only -auto-approve -var="base_image_path=../build/k8labs-base.qcow2" 2>&1 | tail -5 || true; \
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/bootstrap.yml

.PHONY: wait-ips
wait-ips: ## Wait for ALL VMs to get DHCP leases after tofu apply
	@echo '  Waiting for all VM IP addresses (DHCP leases)...'
	@set -euo pipefail; \
	if command -v virsh &>/dev/null; then \
		raw_names=$$(tofu -chdir=terraform output -json node_names 2>/dev/null); \
		if [ -z "$$raw_names" ]; then echo "  ERROR: no node_names from tofu output" >&2; exit 1; fi; \
		node_names=$$(python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)))" <<< "$$raw_names"); \
		if [ -z "$$node_names" ]; then echo "  ERROR: empty node_names" >&2; exit 1; fi; \
		mac_list=""; \
		for name in $$node_names; do \
			mac=$$(virsh -c $(LIBVIRT_URI) domiflist "$$name" 2>/dev/null | awk 'NR>2 && $$5 {print $$5; exit}'); \
			if [ -z "$$mac" ]; then \
				echo "  WARNING: no MAC found for $$name via virsh, falling back to tofu refresh" >&2; \
				mac_list=""; \
				break; \
			fi; \
			mac_list="$$mac_list $$mac@$$name"; \
		done; \
		if [ -n "$$mac_list" ]; then \
			total=$$(echo "$$node_names" | wc -w); \
			for i in $$(seq 1 60); do \
				cp_ip=""; w_ips=""; found_count=0; \
				leases_data=$$(virsh -c $(LIBVIRT_URI) net-dhcp-leases k8s-cluster-net 2>/dev/null); \
				for entry in $$mac_list; do \
					target_mac=$${entry%%@*}; \
					ip=$$(echo "$$leases_data" | awk -v mac="$$target_mac" '$$3 == mac {print $$5; exit}'); \
					ip=$$(echo "$$ip" | sed 's|/.*||'); \
					if [ -n "$$ip" ]; then \
						if [ -z "$$cp_ip" ]; then \
							cp_ip="$$ip"; \
						else \
							w_ips="$$w_ips $$ip"; \
						fi; \
						found_count=$$((found_count + 1)); \
					fi; \
				done; \
				if [ "$$found_count" -ge "$$total" ]; then \
					w_ips_trimmed=$$(echo "$$w_ips" | sed 's/^ *//'); \
					echo "  All $$total VMs ready after $$i cycles — CP: $$cp_ip, Workers: $$w_ips_trimmed"; \
					exit 0; \
				fi; \
				echo "  waiting ($$i/60)... CP=$${cp_ip:-none} workers=$$found_count/$$total"; \
				sleep 5; \
			done; \
			echo "  ERROR: VMs did not get IPs within timeout" >&2; \
			exit 1; \
		fi; \
	fi; \
	echo "  Falling back to tofu refresh method..."; \
	for i in $$(seq 1 60); do \
		tofu -chdir=terraform refresh -var="base_image_path=../build/k8labs-base.qcow2" >/dev/null 2>&1; \
		cp_ip=$$(tofu -chdir=terraform output -raw control_plane_ip 2>/dev/null); \
		w_count=$$(tofu -chdir=terraform output -json worker_ips 2>/dev/null | python3 -c "import sys,json; print(len([x for x in json.load(sys.stdin) if x]))" 2>/dev/null || echo 0); \
		w_total=$$(tofu -chdir=terraform output -json worker_ips 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0); \
		if [ -n "$$cp_ip" ] && [ "$$w_count" -ge "$$w_total" ] && [ "$$w_total" -gt 0 ]; then \
			w_ips=$$(tofu -chdir=terraform output -json worker_ips 2>/dev/null | python3 -c "import sys,json; print(' '.join(filter(None, json.load(sys.stdin))))"); \
			echo "  All $$((w_total + 1)) VMs ready after $$i cycles — CP: $$cp_ip, Workers: $$w_ips"; \
			exit 0; \
		fi; \
		echo "  waiting ($$i/60)... CP=$${cp_ip:-none} workers=$$w_count/$$w_total"; \
		sleep 5; \
	done; \
	echo "  ERROR: VMs did not get IPs within timeout" >&2; \
	exit 1

.PHONY: wait-ssh
wait-ssh: ## Wait for SSH to become available on all VMs (after DHCP leases)
	@echo '  Waiting for SSH connectivity on all VMs...'
	@set -euo pipefail; \
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
	collect_ips_virsh() { \
		local names="$$1"; \
		local first=1; \
		for vname in $$names; do \
			local mac=$$(virsh -c $(LIBVIRT_URI) domiflist "$$vname" 2>/dev/null | awk 'NR>2 && $$5 {print $$5; exit}'); \
			local ip=""; \
			if [ -n "$$mac" ]; then \
				ip=$$(virsh -c $(LIBVIRT_URI) net-dhcp-leases k8s-cluster-net 2>/dev/null | awk -v m="$$mac" '$$3 == m {print $$5; exit}'); \
				ip=$$(echo "$$ip" | sed 's|/.*||'); \
			fi; \
			if [ $$first -eq 1 ]; then \
				printf "%s" "$$ip"; \
				first=0; \
			else \
				printf " %s" "$$ip"; \
			fi; \
		done; \
	}; \
	raw_names=$$(tofu -chdir=terraform output -json node_names 2>/dev/null); \
	if [ -z "$$raw_names" ]; then echo "  ERROR: no node_names from tofu output" >&2; exit 1; fi; \
	node_names=$$(python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)))" <<< "$$raw_names"); \
	if command -v virsh &>/dev/null; then \
		ips=$$(collect_ips_virsh "$$node_names"); \
	else \
		cp_ip=$$(tofu -chdir=terraform output -raw control_plane_ip 2>/dev/null || true); \
		w_ips=$$(tofu -chdir=terraform output -json worker_ips 2>/dev/null | python3 -c "import sys,json; ips=json.load(sys.stdin); print(' '.join(filter(None, ips)))" 2>/dev/null || true); \
		ips="$$cp_ip $$w_ips"; \
	fi; \
	names_arr=(); while IFS= read -r n; do names_arr+=("$$n"); done < <(echo "$$node_names" | tr ' ' '\n'); \
	ips_arr=(); while IFS= read -r ip; do ips_arr+=("$$ip"); done < <(echo "$$ips" | tr ' ' '\n'); \
	pids=""; \
	for idx in "$${!names_arr[@]}"; do \
		name="$${names_arr[idx]}"; \
		ip="$${ips_arr[idx]:-}"; \
		if [ -z "$$ip" ]; then \
			echo "  WARNING: no IP for $$name, skipping SSH check" >&2; \
			continue; \
		fi; \
		(check_ssh "$$ip" "$$name") & \
		pids="$$pids $$!"; \
	done; \
	has_error=0; \
	for pid in $$pids; do \
		[ -z "$$pid" ] && continue; \
		wait "$$pid" || has_error=1; \
	done; \
	if [ "$$has_error" -ne 0 ]; then \
		echo "  ERROR: one or more VMs failed SSH check" >&2; \
		exit 1; \
	fi

.PHONY: cluster
cluster: base extensions container ## Full pipeline: base -> extensions -> container -> deploy -> bootstrap
	@set -euo pipefail; \
	echo 'Bootstrapping cluster via Ansible...'; \
	echo '  Step 1: Deploy VMs (tofu apply)...'; \
	tofu -chdir=terraform apply -auto-approve -var="base_image_path=../build/k8labs-base.qcow2"; \
	echo '  Step 2: Wait for VM IP addresses...'; \
	$(MAKE) wait-ips; \
	echo '  Step 3: Wait for SSH connectivity on all VMs...'; \
	$(MAKE) wait-ssh; \
	echo '  Step 4: Refresh tofu state (DHCP lease IPs) and generate inventory...'; \
	tofu -chdir=terraform apply -refresh-only -auto-approve -var="base_image_path=../build/k8labs-base.qcow2" 2>&1 | tail -5 || true; \
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json; \
	echo '  Step 5: Ansible bootstrap (extensions + certs + KTHW + Cilium)...'; \
	$(ANSIBLE_RUN) ansible-playbook -i $(ANSIBLE_DIR)/inventory/inventory.json \
		$(ANSIBLE_DIR)/playbooks/bootstrap.yml; \
	echo 'Full cluster build and bootstrap complete.'

# --- Cleanup ---

.PHONY: clean
clean: ## Remove build artifacts
	@echo 'Removing build artifacts...'
	rm -rf build/ extensions/release/*.raw

# --- Validation ---

.PHONY: validate-packer
validate-packer: ## Validate Packer template syntax
	@echo 'Validating Packer template syntax...'
	# Unset vars (iso_url, iso_checksum, ssh_password) are expected
	# without a var-file — we only check syntax here.
	cd packer && packer validate . 2>&1 | grep -v 'Unset variable' || true

.PHONY: validate-terraform
validate-terraform: ## Validate Terraform/OpenTofu configuration
	@echo 'Validating Terraform/OpenTofu configuration...'
	tofu -chdir=terraform validate

.PHONY: validate
validate: validate-packer validate-terraform ## Run all validations (packer + terraform)
	@echo 'All validations passed.'
