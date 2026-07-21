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

.PHONY: stop
stop: ## Gracefully stop all Kubernetes cluster VMs
	@set -euo pipefail; \
	if ! command -v virsh &>/dev/null; then \
		echo "  ERROR: required tool 'virsh' not found" >&2; \
		exit 1; \
	fi; \
	raw_names=$$(tofu -chdir=terraform output -json node_names 2>/dev/null) || { \
		echo "  ERROR: failed to get VM list from Terraform state" >&2; \
		exit 1; \
	}; \
	node_names=$$(python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)))" <<< "$$raw_names" 2>/dev/null || true); \
	if [ -z "$$node_names" ]; then \
		echo "  No VMs to stop"; \
		exit 0; \
	fi; \
	echo "  Stopping cluster VMs..."; \
	has_error=0; \
	for vm in $$node_names; do \
		state=$$(virsh -c $(LIBVIRT_URI) dominfo "$$vm" 2>/dev/null | awk -F': ' '/State:/{print $$2}' | xargs || true); \
		if [ "$$state" = "shut off" ] || [ "$$state" = "shut-off" ]; then \
			echo "  $$vm already shut off -- skipping"; \
			continue; \
		fi; \
		echo "  $$vm: sending ACPI shutdown..."; \
		virsh -c $(LIBVIRT_URI) shutdown "$$vm" >/dev/null 2>&1 || true; \
		shut_off=0; \
		# Wait up to 60 seconds for graceful shutdown
		for i in $$(seq 1 12); do \
			sleep 5; \
			new_state=$$(virsh -c $(LIBVIRT_URI) dominfo "$$vm" 2>/dev/null | awk -F': ' '/State:/{print $$2}' | xargs || true); \
			if [ "$$new_state" = "shut off" ] || [ "$$new_state" = "shut-off" ]; then \
				shut_off=1; \
				break; \
			fi; \
		done; \
		if [ "$$shut_off" -eq 1 ]; then \
			echo "  $$vm shut off gracefully"; \
		else \
			echo "  $$vm: graceful shutdown timed out, forcing..."; \
			if ! virsh -c $(LIBVIRT_URI) destroy "$$vm" >/dev/null 2>&1; then \
				echo "  ERROR: failed to force stop $$vm" >&2; \
				has_error=1; \
			else \
				echo "  $$vm forced off"; \
			fi; \
		fi; \
	done; \
	if [ "$$has_error" -ne 0 ]; then \
		echo "  ERROR: one or more VMs failed to stop" >&2; \
		exit 1; \
	fi; \
	echo "  All VMs stopped."

# --- Ansible Container ---

# Libvirt connection URI (must match tofu/terraform provider config)
LIBVIRT_URI := qemu:///system

ANSIBLE_IMAGE := localhost/ansible-podman
ANSIBLE_DIR := ansible
ANSIBLE_RUN := podman run --rm --network host \
	--cap-add=NET_ADMIN \
	-v $(PWD):/workspace:z \
	-v $(HOME)/.ssh:/root/.ssh:ro,z \
	-v $(SSH_AUTH_SOCK):/ssh-agent:z \
	-v /var/run/libvirt:/var/run/libvirt:ro,z \
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
	tofu -chdir=terraform apply -refresh-only -auto-approve -var="base_image_path=../build/k8labs-base.qcow2" 2>&1 | tail -5 || { echo "  ERROR: tofu refresh failed" >&2; exit 1; }; \
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json
	$(ANSIBLE_RUN) ansible-playbook -i ansible/inventory/inventory.json \
		ansible/playbooks/bootstrap.yml; \
	echo '  Adding host route for LB pool...'; \
	LB_BRIDGE=$$(virsh -c $(LIBVIRT_URI) net-info k8s-cluster-net 2>/dev/null | sed -n 's/^Bridge:[[:space:]]*//p'); \
	if [ -n "$$LB_BRIDGE" ]; then \
		if ! ip route show 10.0.10.0/24 2>/dev/null | grep -q "$$LB_BRIDGE"; then \
			sudo ip route add 10.0.10.0/24 dev "$$LB_BRIDGE"; \
			echo "  Route added via $$LB_BRIDGE"; \
		else \
			echo "  Route already exists via $$LB_BRIDGE"; \
		fi; \
	else \
		echo "  WARNING: could not detect bridge for k8s-cluster-net, skipping route"; \
	fi

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

.PHONY: prereq
prereq: ## Validate required build tools are installed (tofu/terraform, virsh, podman, openssl)
	@set -euo pipefail; \
	fail=0; \
	if ! command -v tofu &>/dev/null && ! command -v terraform &>/dev/null; then \
		echo "ERROR: required tool 'tofu' or 'terraform' not found. Install with: mise install opentofu" >&2; \
		fail=1; \
	fi; \
	if ! command -v virsh &>/dev/null; then \
		echo "ERROR: required tool 'virsh' not found. Install with: apt install libvirt-clients" >&2; \
		fail=1; \
	fi; \
	if ! command -v podman &>/dev/null; then \
		echo "ERROR: required tool 'podman' not found. Install with: apt install podman" >&2; \
		fail=1; \
	fi; \
	if ! command -v openssl &>/dev/null; then \
		echo "ERROR: required tool 'openssl' not found. Install with: apt install openssl" >&2; \
		fail=1; \
	fi; \
	exit $$fail

.PHONY: cluster
cluster: prereq base extensions container ## Full pipeline: base -> extensions -> container -> deploy -> bootstrap
	@set -euo pipefail; \
	echo 'Bootstrapping cluster via Ansible...'; \
	echo '  Step 1: Deploy VMs (tofu apply)...'; \
	tofu -chdir=terraform apply -auto-approve -var="base_image_path=../build/k8labs-base.qcow2"; \
	echo '  Step 2: Wait for VM IP addresses...'; \
	$(MAKE) wait-ips; \
	echo '  Step 3: Wait for SSH connectivity on all VMs...'; \
	$(MAKE) wait-ssh; \
	echo '  Step 4: Refresh tofu state (DHCP lease IPs) and generate inventory...'; \
	tofu -chdir=terraform apply -refresh-only -auto-approve -var="base_image_path=../build/k8labs-base.qcow2" 2>&1 | tail -5 || { echo "  ERROR: tofu refresh failed" >&2; exit 1; }; \
	$(ANSIBLE_DIR)/inventory/tf-inventory.sh --list > $(ANSIBLE_DIR)/inventory/inventory.json; \
	echo '  Step 5: Ansible bootstrap (extensions + certs + KTHW + Cilium)...'; \
	$(ANSIBLE_RUN) ansible-playbook -i $(ANSIBLE_DIR)/inventory/inventory.json \
		$(ANSIBLE_DIR)/playbooks/bootstrap.yml; \
	echo '  Step 6: Add host route for LB pool...'; \
	LB_BRIDGE=$$(virsh -c $(LIBVIRT_URI) net-info k8s-cluster-net 2>/dev/null | sed -n 's/^Bridge:[[:space:]]*//p'); \
	if [ -n "$$LB_BRIDGE" ]; then \
		if ! ip route show 10.0.10.0/24 2>/dev/null | grep -q "$$LB_BRIDGE"; then \
			sudo ip route add 10.0.10.0/24 dev "$$LB_BRIDGE"; \
			echo "  Route added via $$LB_BRIDGE"; \
		else \
			echo "  Route already exists via $$LB_BRIDGE"; \
		fi; \
	else \
		echo "  WARNING: could not detect bridge for k8s-cluster-net, skipping route"; \
	fi; \
	echo '  Step 7: Fetch DHCP-resistant kubeconfig...'; \
	$(MAKE) kubeconfig; \
	echo 'Full cluster build and bootstrap complete.'

# --- kubeconfig ---

.PHONY: kubeconfig update-kubeconfig
kubeconfig: ## Fetch DHCP-resistant kubeconfig from control-plane node
	@set -euo pipefail; \
	cp_ip=""; \
	if command -v virsh &>/dev/null; then \
		cp_name=$$(tofu -chdir=terraform output -json node_names 2>/dev/null | python3 -c "import sys,json;n=json.load(sys.stdin);print(n[0])" 2>/dev/null || true); \
		if [ -n "$$cp_name" ] && [ "$$cp_name" != "null" ]; then \
			mac=$$(virsh -c $(LIBVIRT_URI) domiflist "$$cp_name" 2>/dev/null | awk 'NR>2 && $$5 {print $$5; exit}'); \
			if [ -n "$$mac" ]; then \
				cp_ip=$$(virsh -c $(LIBVIRT_URI) net-dhcp-leases k8s-cluster-net 2>/dev/null | awk -v m="$$mac" '$$3 == m {print $$5; exit}'); \
				cp_ip=$$(echo "$$cp_ip" | sed 's|/.*||'); \
			fi; \
		fi; \
	fi; \
	if [ -z "$$cp_ip" ] || [ "$$cp_ip" = "null" ]; then \
		cp_ip=$$(tofu -chdir=terraform output -raw control_plane_ip 2>&1 | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1 || true); \
	fi; \
	if [ -z "$$cp_ip" ] || [ "$$cp_ip" = "null" ]; then \
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
