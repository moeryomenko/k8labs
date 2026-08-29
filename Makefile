# k8labs — Kubernetes OS Image Build System
# Targets for Packer VM baking and system/configuration extensions.
# For maximum parallelism, use: make -j$$(nproc) cluster
# This builds base image, extensions, and cluster VMs.

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
# Bake container: the base image is baked with Packer + the Cloud-Hypervisor
# plugin running inside a rootless podman container (its own netns), so the
# bake needs no host bridge/TAP/dnsmasq/NAT. The plugin is built into the
# image from source at PACKER_PLUGIN_REF (default: main).
BAKE_IMAGE := localhost/k8labs-bake:dev
PACKER_PLUGIN_REF ?= main

.PHONY: bake-image
bake-image: ## Build the rootless bake container image (packer + CH plugin + bake-net.sh)
	@echo '==> Building bake container image ($(BAKE_IMAGE))...'
	podman build -t "$(BAKE_IMAGE)" \
		--build-arg PACKER_PLUGIN_REF="$(PACKER_PLUGIN_REF)" \
		bake/

# `make plugin` is now an alias for `bake-image`: the Cloud-Hypervisor Packer
# plugin is built inside the bake container (cloned from source at
# PACKER_PLUGIN_REF during the image build) instead of being built on the host.
.PHONY: plugin plugin-rebuild
plugin: bake-image ## Build/refresh the bake container image (Packer CH plugin now lives inside it)
	@echo '    Cloud-Hypervisor Packer plugin is baked into $(BAKE_IMAGE)'

plugin-rebuild: bake-image ## Force refresh of the bake container image (rebuilds the plugin from source)

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
	rm -f "$$RAW_DEST"; \
	qemu-img convert -O raw "$(FEDORA_CLOUD_DEST)" "$$RAW_DEST"; \
	echo '    Converted to raw format (always fresh: the bake mutates the raw in place)'

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
base: bake-image base-deps base-cloudinit sysexts confexts ## Build base image via Packer in the rootless bake container
	@if [ -f "$(BASE_IMAGE_DEST)" ]; then \
		NEWEST_RAW="$$(ls -t extensions/release/*.raw 2>/dev/null | head -n1)"; \
		if [ -n "$$NEWEST_RAW" ] && [ "$$NEWEST_RAW" -nt "$(BASE_IMAGE_DEST)" ]; then \
			echo 'Extension images are newer than $(BASE_IMAGE_DEST); rebuilding base image...'; \
		else \
			echo 'Base image already exists: $(BASE_IMAGE_DEST)'; \
			echo 'To force a rebuild, run: make base-rebuild'; \
			exit 0; \
		fi; \
	fi
	@echo 'Building base image via Packer in the bake container...'
	rm -rf build/base-image packer/output-base
	$(MAKE) bake-run
	@echo 'Copying base image to build/...'
	@set -euo pipefail; \
	ARTIFACT=$$(find packer/output-base -maxdepth 1 -type f ! -name '*.lock' | head -1); \
	if [ -z "$$ARTIFACT" ]; then \
		echo 'ERROR: no Packer artifact found in packer/output-base/' >&2; \
		exit 1; \
	fi; \
	mkdir -p build; \
	qemu-img convert -O qcow2 "$$ARTIFACT" "$(BASE_IMAGE_DEST)"; \
	echo "    Converted $$ARTIFACT -> $(BASE_IMAGE_DEST)"

# bake-run runs the Packer build inside the rootless bake container. The
# repo's build/, packer/, and extensions/ trees are bind-mounted so the
# template's packer-relative paths (../build, ../extensions/release,
# ../extensions/selinux, scripts/) resolve exactly as on the host. /dev/kvm
# and /dev/net/tun are passed through, and NET_ADMIN/NET_RAW let bake-net.sh
# create the tap + NAT inside the container's own netns — no host privilege.
.PHONY: bake-run
bake-run:
	podman run --rm \
		--device /dev/kvm --device /dev/net/tun \
		--cap-add NET_ADMIN,NET_RAW \
		--sysctl net.ipv4.ip_forward=1 \
		-v "$(CURDIR)/build:/src/build" \
		-v "$(CURDIR)/packer:/src/packer" \
		-v "$(CURDIR)/extensions:/src/extensions" \
		-w /src/packer \
		"$(BAKE_IMAGE)" \
		packer build -var-file=vars.pkrvars.hcl -only=cloud-hypervisor.base .

.PHONY: base-rebuild
base-rebuild: bake-image base-deps base-cloudinit sysexts confexts ## Force rebuild of the base image
	@echo 'Forcing base image rebuild via Packer in the bake container...'
	rm -f "$(BASE_IMAGE_DEST)"
	rm -rf build/base-image packer/output-base
	$(MAKE) bake-run
	@set -euo pipefail; \
	ARTIFACT=$$(find packer/output-base -maxdepth 1 -type f ! -name '*.lock' | head -1); \
	if [ -z "$$ARTIFACT" ]; then \
		echo 'ERROR: no Packer artifact found in packer/output-base/' >&2; \
		exit 1; \
	fi; \
	mkdir -p build; \
	qemu-img convert -O qcow2 "$$ARTIFACT" "$(BASE_IMAGE_DEST)"; \
	echo "    Converted $$ARTIFACT -> $(BASE_IMAGE_DEST)"

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

CONFEXT_NAMES := cri-o kubernetes containers

.PHONY: $(addprefix confext/,$(CONFEXT_NAMES)) confexts

confext/cri-o: ## Build confext cri-o configuration overlay
	@echo 'Building confext cri-o...'
	extensions/build.sh confext confext/cri-o confext-cri-o

confext/kubernetes: ## Build confext kubernetes configuration overlay
	@echo 'Building confext kubernetes...'
	extensions/build.sh confext confext/kubernetes confext-kubernetes

confext/containers: ## Build confext containers configuration overlay
	@echo 'Building confext containers...'
	extensions/build.sh confext confext/containers confext-containers

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

# --- CAPI Management Plane ---
#
# The cluster lifecycle is driven by a rootless capishim management plane:
# quadlet units run capishim-pod (etcd + apiserver), k8netd (userspace
# networking) and the cluster-api-hypervisor provider manager as systemd
# user services. clusterctl-style flows apply capi/cluster.yaml against that
# plane; the resulting workload kubeconfig lands in build/kubeconfig.

CLUSTER_NAME ?= k8labs
CAPISHIM_KUBECONFIG := $${HOME}/.kube/capishim.kubeconfig
WORKLOAD_KUBECONFIG := build/kubeconfig
MGMT_UNITS := capishim-pod.service k8netd.service cluster-api-hypervisor.service

# Provider state root: the provider quadlet bind-mounts this into /build and
# boots machines from the baked base image, firmware, and vm-disks. It also
# needs /tmp/ch-capi (CH api socket dir) to exist.
PROVIDER_STATE := $${HOME}/.local/state/k8slab/build
PROVIDER_STATE_SSH_KEY := $(PROVIDER_STATE)/ssh-lab.pub

.PHONY: provider-state
provider-state: ## Create provider state dirs and publish bake artifacts into the provider's build mount (idempotent)
	@set -euo pipefail; \
	echo '==> Creating provider state dirs...'; \
	mkdir -p "$(PROVIDER_STATE)/vm-disks"; \
	mkdir -p /tmp/ch-capi; \
	echo '==> Publishing bake artifacts to $(PROVIDER_STATE)...'; \
	cp -f "$(BASE_IMAGE_DEST)" "$(PROVIDER_STATE)/k8labs-base.qcow2"; \
	cp -f "$(FIRMWARE_DEST)" "$(PROVIDER_STATE)/CLOUDHV.fd"; \
	if [ ! -f "$(SSH_KEY).pub" ]; then \
		echo "ERROR: $(SSH_KEY).pub not found - run 'make base-ssh-key' first" >&2; \
		exit 1; \
	fi; \
	cp -f "$(SSH_KEY).pub" "$(PROVIDER_STATE_SSH_KEY)"; \
	echo "    Published base image, firmware, and lab SSH key"

# Management-plane images come from CI: each sibling repo (capishim,
# cluster-api-hypervisor, k8netd) builds and pushes its images to ghcr.io via
# GitHub Actions. mgmt-images pulls the pinned tags and retags them to the
# localhost/* names the installed quadlets reference, so the quadlet Image=
# lines stay unchanged.
GHCR_OWNER := moeryomenko
MGMT_IMAGE_TAG ?= latest

.PHONY: mgmt-images
mgmt-images: ## Pull management-plane images from ghcr.io and retag to the quadlet localhost/* names (idempotent; keeps existing local images when the registry tag is not yet published)
	@set -euo pipefail; \
	pull_retag() { \
		src="$$1"; dst="$$2"; \
		if podman image exists "$${dst}" 2>/dev/null; then \
			echo "==> $${dst} already present (keeping local image)"; \
			return 0; \
		fi; \
		echo "==> $(GHCR_OWNER)/$${src}:$(MGMT_IMAGE_TAG) -> $${dst}"; \
		podman pull "ghcr.io/$(GHCR_OWNER)/$${src}:$(MGMT_IMAGE_TAG)"; \
		podman tag "ghcr.io/$(GHCR_OWNER)/$${src}:$(MGMT_IMAGE_TAG)" "$${dst}"; \
	}; \
	pull_retag "k8netd" "localhost/k8netd:dev"; \
	pull_retag "cluster-api-hypervisor" "localhost/cluster-api-hypervisor:dev"; \
	for comp in setup core cabpk kcp capd; do \
		pull_retag "capishim-$${comp}" "localhost/capishim-$${comp}:v0.1.0"; \
	done; \
	echo '    Management-plane images present (localhost/* names)'

.PHONY: prereq
prereq: ## Validate CAPI tooling (cloud-hypervisor, openssl, systemctl, jq, python3 venv, clusterctl, kubectl, podman), quadlet units, the capishim kubeconfig, KVM readiness (/dev/kvm + kvm group), and baked build artifacts
	@set -euo pipefail; \
	fail=0; \
	for cmd in cloud-hypervisor openssl systemctl jq python3 clusterctl kubectl; do \
		if ! command -v $$cmd &>/dev/null; then \
			echo "ERROR: required tool '$$cmd' not found on PATH" >&2; \
			fail=1; \
		fi; \
	done; \
	QDIR="$${XDG_CONFIG_HOME:-$$HOME/.config}/containers/systemd"; \
	for unit in capishim.pod k8netd.container cluster-api-hypervisor.container; do \
		if [ ! -f "$$QDIR/$$unit" ]; then \
			echo "ERROR: quadlet unit '$$unit' not found in $$QDIR - install it for the capishim management plane" >&2; \
			fail=1; \
		fi; \
	done; \
	KC="$${HOME}/.kube/capishim.kubeconfig"; \
	if [ ! -f "$$KC" ]; then \
		echo "ERROR: capishim kubeconfig not found at $$KC - provision the management plane first" >&2; \
		fail=1; \
	fi; \
	if [ ! -d ".venv" ]; then \
		echo "ERROR: Python virtual environment '.venv' not found - run make node-tools first" >&2; \
		fail=1; \
	fi; \
	if [ ! -e /dev/kvm ]; then \
		echo 'ERROR: /dev/kvm not found - KVM acceleration is required to run lab VMs' >&2; \
		fail=1; \
	fi; \
	if ! id -nG 2>/dev/null | grep -qw kvm; then \
		echo "ERROR: current user is not in the kvm group - add your user to the kvm group: sudo usermod -aG kvm $$USER" >&2; \
		fail=1; \
	fi; \
	if ! command -v podman &>/dev/null; then \
		echo "ERROR: required tool 'podman' not found on PATH - install podman (rootless container runtime for the management plane)" >&2; \
		fail=1; \
	fi; \
	if [ ! -f "$(BASE_IMAGE_DEST)" ]; then \
		echo "ERROR: base image '$(BASE_IMAGE_DEST)' not found - run 'make base' to bake it first" >&2; \
		fail=1; \
	fi; \
	if [ ! -f "$(FIRMWARE_DEST)" ]; then \
		echo "ERROR: firmware '$(FIRMWARE_DEST)' not found - run 'make base' to fetch it first" >&2; \
		fail=1; \
	fi; \
	exit $$fail

# Setup ordering: after the management API answers, mgmt-up first waits for
# the capishim-setup oneshot to converge (Result=success via `systemctl --user
# show -p Result --value capishim-setup.service`; a probe that cannot read the
# unit counts as converged; 'unknown'/empty Result while activating keeps
# polling) BEFORE the readyz gate below, so cluster applies never race
# not-yet-installed webhooks.
#
# REQ-012 (D7): after the management API answers, mgmt-up additionally gates
# on the provider health endpoint http://127.0.0.1:9440/readyz so cluster-up
# never races fail-closed admission. Only an HTTP 200 answer passes the gate.
# Every other outcome — a non-200 status or a transport-level failure such as
# connection refused (the provider unit is dead) — counts as not-ready and is
# retried until the bounded timeout expires (~60s, 2s interval), then fails
# naming the provider unit and the health address. Probe mechanism: curl,
# falling back to wget, then python3 (prereq guarantees python3).
.PHONY: mgmt-up
mgmt-up: ## Start the capishim management plane units and wait until its API and the provider /readyz respond (idempotent)
	@set -euo pipefail; \
	echo '==> Starting capishim management plane ($(MGMT_UNITS))...'; \
	systemctl --user start $(MGMT_UNITS); \
	plane_ready=0; \
	for i in $$(seq 1 30); do \
		if kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" get namespaces >/dev/null 2>&1; then \
			plane_ready=1; \
			break; \
		fi; \
		echo "    waiting for management plane ($$i/30)..."; \
		sleep 2; \
	done; \
	if [ "$$plane_ready" != "1" ]; then \
		echo 'ERROR: capishim management plane did not respond within timeout' >&2; \
		exit 1; \
	fi; \
	echo '    Management plane ready'; \
	echo '==> Waiting for capishim-setup oneshot convergence (capishim-setup.service)...'; \
	setup_ok=0; \
	for i in $$(seq 1 30); do \
		if ! setup_result=$$(systemctl --user show -p Result --value capishim-setup.service 2>/dev/null); then \
			setup_ok=1; \
			break; \
		fi; \
		if [ "$$setup_result" = "success" ]; then \
			setup_ok=1; \
			break; \
		fi; \
		echo "    waiting for capishim-setup ($$i/30)..."; \
		sleep 2; \
	done; \
	if [ "$$setup_ok" != "1" ]; then \
		echo 'ERROR: capishim-setup.service did not converge within the bounded wait; applying clusters now would race not-yet-installed webhooks' >&2; \
		echo '       Inspect the setup oneshot: journalctl --user -u capishim-setup -n 50' >&2; \
		exit 1; \
	fi; \
	echo '    capishim-setup converged'; \
	echo '==> Waiting for provider readiness (cluster-api-hypervisor.service)...'; \
	readyz_url='http://127.0.0.1:9440/readyz'; \
	if command -v curl >/dev/null 2>&1; then \
		readyz_probe() { [ "$$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$$readyz_url")" = "200" ]; }; \
	elif command -v wget >/dev/null 2>&1; then \
		readyz_probe() { wget -q -T 5 -O /dev/null "$$readyz_url"; }; \
	elif command -v python3 >/dev/null 2>&1; then \
		readyz_probe() { python3 -c 'import urllib.request as u, sys; r = u.urlopen(sys.argv[1], timeout=5); sys.exit(0 if r.status == 200 else 1)' "$$readyz_url" 2>/dev/null; }; \
	else \
		echo 'ERROR: no fetch tool (curl/wget/python3) on PATH to probe the provider health address http://127.0.0.1:9440/readyz' >&2; \
		exit 1; \
	fi; \
	for i in $$(seq 1 30); do \
		if readyz_probe; then \
			echo '    Provider ready'; \
			exit 0; \
		fi; \
		echo "    waiting for provider readyz ($$i/30)..."; \
		sleep 2; \
	done; \
	echo 'ERROR: cluster-api-hypervisor.service never became ready: http://127.0.0.1:9440/readyz did not answer ok within the bounded wait' >&2; \
	echo '       Inspect the provider unit: journalctl --user -u cluster-api-hypervisor.service -n 50' >&2; \
	exit 1

.PHONY: mgmt-down
mgmt-down: ## Stop the capishim management plane units (never deletes management state)
	@set -euo pipefail; \
	echo '==> Stopping capishim management plane ($(MGMT_UNITS))...'; \
	systemctl --user stop $(MGMT_UNITS); \
	echo '    Management plane stopped (state preserved)'

.PHONY: cluster-up
cluster-up: ## Server-side apply capi/cluster.yaml against the capishim management plane (idempotent)
	@set -euo pipefail; \
	echo '==> Applying capi/cluster.yaml (server-side) to the management plane...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" apply --server-side -f capi/cluster.yaml; \
	echo '    Cluster manifest applied'

.PHONY: addons-up
addons-up: ## Server-side apply capi/addons/ (ClusterResourceSets + resource Secrets) against the capishim management plane (idempotent)
	@set -euo pipefail; \
	echo '==> Applying capi/addons/ (server-side) to the management plane...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" apply --server-side -f capi/addons/; \
	echo '    ClusterResourceSet addons applied'

.PHONY: cluster-down
cluster-down: ## Delete the workload Cluster via the management plane and wait for reclamation
	@set -euo pipefail; \
	echo '==> Deleting Cluster $(CLUSTER_NAME) via the management plane...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"; \
	echo '==> Waiting for infrastructure reclamation...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" wait --for=delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=10m; \
	echo '    Cluster deleted and reclaimed'

.PHONY: cluster-clean
cluster-clean: ## Remove leftover workload-cluster resources on the management plane without deleting the plane itself
	@set -euo pipefail; \
	if ! command -v kubectl >/dev/null 2>&1; then \
		echo 'ERROR: kubectl not found on PATH' >&2; \
		exit 1; \
	fi; \
	if [ ! -f "$(CAPISHIM_KUBECONFIG)" ]; then \
		echo "ERROR: capishim kubeconfig not found at $(CAPISHIM_KUBECONFIG)" >&2; \
		exit 1; \
	fi; \
	echo '==> Cleaning leftover workload-cluster resources on the management plane...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" delete hypervisormachines.infrastructure.cluster.x-k8s.io -l "cluster.x-k8s.io/cluster-name=$(CLUSTER_NAME)" --ignore-not-found; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" delete hypervisormachinetemplates.infrastructure.cluster.x-k8s.io -l "cluster.x-k8s.io/cluster-name=$(CLUSTER_NAME)" --ignore-not-found; \
	echo '    Leftover workload-cluster resources cleaned'

.PHONY: cluster
cluster: prereq ## Full CAPI pipeline: prereq -> mgmt-images -> provider-state -> mgmt-up -> cluster-up -> addons-up -> wait Cluster ready -> kubeconfig -> smoke-test
	@set -euo pipefail; \
	$(MAKE) mgmt-images; \
	$(MAKE) provider-state; \
	$(MAKE) mgmt-up; \
	$(MAKE) cluster-up; \
	$(MAKE) addons-up; \
	echo '==> Waiting for Cluster $(CLUSTER_NAME) to become Ready...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" wait --for=condition=Ready "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=30m; \
	$(MAKE) kubeconfig; \
	$(MAKE) smoke-test; \
	echo 'Cluster ready.'

# --- kubeconfig ---

.PHONY: kubeconfig update-kubeconfig
kubeconfig: ## Fetch the workload Secret $(CLUSTER_NAME)-kubeconfig from the management plane, decode data.value, write build/kubeconfig
	@set -euo pipefail; \
	mkdir -p build; \
	secret_json=$$(mktemp); \
	kubeconfig_out=$$(mktemp); \
	trap 'rm -f "$$secret_json" "$$kubeconfig_out"' EXIT; \
	echo "==> Fetching Secret $(CLUSTER_NAME)-kubeconfig from the management plane..."; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" get secret "$(CLUSTER_NAME)-kubeconfig" -n default -o json > "$$secret_json"; \
	scripts/fetch-kubeconfig "$$secret_json" > "$$kubeconfig_out"; \
	mv "$$kubeconfig_out" "$(WORKLOAD_KUBECONFIG)"; \
	chmod 600 "$(WORKLOAD_KUBECONFIG)"; \
	echo "    Workload kubeconfig written to $(WORKLOAD_KUBECONFIG)"

update-kubeconfig: kubeconfig ## Alias for kubeconfig — explicitly signals refresh

# --- Smoke Test ---

# Legacy cluster-ops targets (rbac/cilium/coredns/metrics-server) still point
# at the in-repo kubeconfig path.
KUBECONFIG := kubeconfig

# Kubeconfig the smoke-test drives. Defaults to the single-cluster flow's
# build/kubeconfig; multi-cluster-test overrides it per fetched cluster
# kubeconfig without changing the default behavior.
SMOKE_KUBECONFIG ?= $(WORKLOAD_KUBECONFIG)

.PHONY: smoke-test
smoke-test: kubeconfig ## Apply capi/smoke-test/job.yaml against the workload cluster (default build/kubeconfig; override with SMOKE_KUBECONFIG=...) and wait for Job success
	@set -euo pipefail; \
	echo '==> Running smoke-test Job against the workload cluster...'; \
	kubectl --kubeconfig "$(SMOKE_KUBECONFIG)" apply -f capi/smoke-test/job.yaml; \
	kubectl --kubeconfig "$(SMOKE_KUBECONFIG)" -n default wait --for=condition=complete job/lb-smoke-test --timeout=300s; \
	echo '    Smoke test passed'

# --- Multi-Cluster Verification ---

MULTI_CLUSTER2_NAME := k8labs-2
MULTI_CLUSTER2_MANIFEST := capi/cluster-lab2.yaml
MULTI_KUBECONFIG_1 := build/kubeconfig.k8labs
MULTI_KUBECONFIG_2 := build/kubeconfig.k8labs-2

# REQ-013 (D10): prove TWO concurrent workload clusters on one host in a
# single automated run. The second cluster (k8labs-2) is the checked-in
# topology-only manifest capi/cluster-lab2.yaml (P4); its ClusterClass and
# templates come from capi/cluster.yaml, applied by mgmt-up + the applies
# below.
#
# Resource floor: the 8 concurrent workload VMs need ~16 CPU cores and
# ~28 GiB RAM free on the host (per cluster: 1 control plane at 2 vCPU/2 GiB
# plus 3 workers at 2 vCPU/4 GiB), on top of the capishim/k8netd management
# plane; budget ~20-40 minutes end to end.
#
# Every per-cluster kubectl lifecycle call (readiness wait, delete,
# reclamation wait) pins --cache-dir to build/.kube-cache-<cluster>:
# concurrent kubectl runs get isolated discovery caches, and each logged
# invocation self-identifies the cluster it drives.

.PHONY: multi-cluster-test
multi-cluster-test: prereq ## Provision k8labs AND k8labs-2 concurrently, smoke-test both, tear both down (REQ-013; heavy: ~16 CPU / ~28 GiB RAM floor)
	@set -euo pipefail; \
	echo '==> Bringing up the capishim management plane...'; \
	$(MAKE) mgmt-up; \
	echo '==> Applying both Cluster manifests (server-side)...'; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" apply --server-side -f capi/cluster.yaml; \
	kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" apply --server-side -f "$(MULTI_CLUSTER2_MANIFEST)"; \
	echo '    Both Cluster manifests applied'; \
	echo '==> Waiting for Cluster $(CLUSTER_NAME) to become Ready...'; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" wait --for=condition=Ready "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=30m; then \
		echo "ERROR [readiness] Cluster $(CLUSTER_NAME) did not become Ready within 30m" >&2; \
		exit 1; \
	fi; \
	echo '==> Waiting for Cluster $(MULTI_CLUSTER2_NAME) to become Ready...'; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" wait --for=condition=Ready "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)" --timeout=30m; then \
		echo "ERROR [readiness] Cluster $(MULTI_CLUSTER2_NAME) did not become Ready within 30m" >&2; \
		exit 1; \
	fi; \
	echo '    Both Clusters Ready'; \
	mkdir -p build; \
	fetch_wc_kubeconfig() { \
		cluster="$$1"; \
		out="$$2"; \
		secret_json="$$(mktemp)"; \
		echo "==> Fetching Secret $${cluster}-kubeconfig from the management plane..."; \
		kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" get secret "$${cluster}-kubeconfig" -n default -o json > "$$secret_json"; \
		scripts/fetch-kubeconfig "$$secret_json" > "$$out"; \
		chmod 600 "$$out"; \
		rm -f "$$secret_json"; \
	}; \
	fetch_wc_kubeconfig "$(CLUSTER_NAME)" "$(MULTI_KUBECONFIG_1)"; \
	fetch_wc_kubeconfig "$(MULTI_CLUSTER2_NAME)" "$(MULTI_KUBECONFIG_2)"; \
	url1="$$(awk '/^[[:space:]]*server:/ {print $$2; exit}' "$(MULTI_KUBECONFIG_1)")"; \
	url2="$$(awk '/^[[:space:]]*server:/ {print $$2; exit}' "$(MULTI_KUBECONFIG_2)")"; \
	if [ -z "$$url1" ] || [ -z "$$url2" ]; then \
		echo 'ERROR [kubeconfig-endpoint] no server URL found in one of the fetched kubeconfigs' >&2; \
		exit 1; \
	fi; \
	if [ "$$url1" = "$$url2" ]; then \
		echo "ERROR [kubeconfig-endpoint] port collision: $(CLUSTER_NAME) ($$url1) and $(MULTI_CLUSTER2_NAME) ($$url2) resolve to the SAME server URL; distinct published ports are required" >&2; \
		exit 1; \
	fi; \
	echo "    Distinct API endpoints: $(CLUSTER_NAME) $$url1, $(MULTI_CLUSTER2_NAME) $$url2"; \
	echo '==> Running the smoke-test Job against $(CLUSTER_NAME)...'; \
	$(MAKE) smoke-test SMOKE_KUBECONFIG="$(MULTI_KUBECONFIG_1)"; \
	echo '==> Running the smoke-test Job against $(MULTI_CLUSTER2_NAME)...'; \
	$(MAKE) smoke-test SMOKE_KUBECONFIG="$(MULTI_KUBECONFIG_2)"; \
	echo '==> Deleting both Clusters (best-effort)...'; \
	del_failures=''; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"; then \
		echo "ERROR [teardown] deleting Cluster $(CLUSTER_NAME) failed" >&2; \
		del_failures=" $$del_failures $(CLUSTER_NAME)"; \
	fi; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)"; then \
		echo "ERROR [teardown] deleting Cluster $(MULTI_CLUSTER2_NAME) failed" >&2; \
		del_failures=" $$del_failures $(MULTI_CLUSTER2_NAME)"; \
	fi; \
	echo '==> Waiting for infrastructure reclamation (both Clusters)...'; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" wait --for=delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=10m; then \
		echo "ERROR [reclamation] Cluster $(CLUSTER_NAME) was not reclaimed within 10m" >&2; \
		del_failures=" $$del_failures $(CLUSTER_NAME)"; \
	fi; \
	if ! kubectl --kubeconfig "$(CAPISHIM_KUBECONFIG)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" wait --for=delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)" --timeout=10m; then \
		echo "ERROR [reclamation] Cluster $(MULTI_CLUSTER2_NAME) was not reclaimed within 10m" >&2; \
		del_failures=" $$del_failures $(MULTI_CLUSTER2_NAME)"; \
	fi; \
	if [ -n "$$del_failures" ]; then \
		echo "ERROR [teardown] multi-cluster-test failed for cluster(s):$${del_failures}" >&2; \
		exit 1; \
	fi; \
	echo 'Multi-cluster test passed.'
# --- Metrics Server ---

.PHONY: metrics-server
metrics-server: ## Deploy metrics-server for kubectl top
	@echo 'Deploying metrics-server...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f cilium/metrics-server.yaml
	@echo 'Waiting for metrics-server to be ready...'
	kubectl --kubeconfig $(KUBECONFIG) -n kube-system wait --for=condition=Available deployment/metrics-server --timeout=60s
	@echo 'metrics-server ready. Run: kubectl top nodes'

# --- CoreDNS ---

.PHONY: coredns
coredns: ## Deploy CoreDNS cluster DNS (kube-dns Service at 10.96.0.10)
	@echo 'Deploying CoreDNS...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f coredns/
	@echo 'Waiting for CoreDNS deployment to be Available...'
	kubectl --kubeconfig $(KUBECONFIG) -n kube-system wait --for=condition=Available deployment/coredns --timeout=5m
	@echo 'Verifying kube-dns Service clusterIP...'
	@set -euo pipefail; \
	clusterip="$$(kubectl --kubeconfig $(KUBECONFIG) -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')"; \
	if [ "$$clusterip" != "10.96.0.10" ]; then \
		echo "FAIL: kube-dns clusterIP is $$clusterip, expected 10.96.0.10"; \
		exit 1; \
	fi; \
	echo "PASS: kube-dns clusterIP is 10.96.0.10"
	@echo 'CoreDNS ready.'

# --- Cluster Ops ---

.PHONY: rbac
rbac: ## Apply cluster RBAC (kubelet bootstrap, system:nodes, admin, apiserver-proxy)
	@echo 'Applying cluster RBAC manifests...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f rbac/
	@echo 'RBAC ready.'

.PHONY: cilium
cilium: rbac ## Install Cilium from committed manifests (cilium.io CRDs + install + policies)
	@echo 'Applying cilium.io CRD bundle (v1.19.6) first...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f cilium/install/00-crds/
	@echo 'Waiting for cilium.io CRDs to be established...'
	kubectl --kubeconfig $(KUBECONFIG) wait --for=condition=Established \
		crd/ciliumloadbalancerippools.cilium.io \
		crd/ciliuml2announcementpolicies.cilium.io \
		crd/ciliumgatewayclassconfigs.cilium.io \
		crd/ciliumclusterwidenetworkpolicies.cilium.io \
		crd/ciliumnetworkpolicies.cilium.io \
		crd/ciliumcidrgroups.cilium.io \
		crd/ciliumnodes.cilium.io \
		crd/ciliumendpoints.cilium.io \
		crd/ciliumidentities.cilium.io \
		crd/ciliumegressgatewaypolicies.cilium.io \
		crd/ciliumenvoyconfigs.cilium.io \
		crd/ciliumlocalredirectpolicies.cilium.io --timeout=5m
	@echo 'Applying Cilium install manifests (Gateway API CRDs + install)...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f cilium/install/
	@echo 'Applying LB pool, L2 policy, and Gateway manifests...'
	kubectl --kubeconfig $(KUBECONFIG) apply -f cilium/gatewayclass.yaml \
		-f cilium/gateway-class-config.yaml -f cilium/gateway.yaml \
		-f cilium/http-route.yaml -f cilium/lb-pool.yaml \
		-f cilium/l2-policy.yaml
	@echo 'Cilium ready.'

# --- Cleanup ---

.PHONY: clean
clean: ## Remove build artifacts
	@echo 'Removing build artifacts...'
	rm -rf build/ extensions/release/*.raw

# --- Python Tooling ---

.PHONY: node-tools
node-tools: ## Sync Python tooling with uv (creates .venv, idempotent)
	@echo '==> Syncing Python tooling (uv sync)...'
	uv sync

.PHONY: test
test: ## Run the Python test suite (pytest)
	@echo '==> Running Python tests (pytest)...'
	uv run pytest tests/

# --- Validation ---

.PHONY: validate-packer
validate-packer: ## Validate Packer template syntax
	@echo 'Validating Packer template syntax...'
	# Validate the real template with its var file; no masking so an
	# unset-variable error fails the target (was masked by `|| true`).
	cd packer && packer validate -var-file=vars.pkrvars.hcl .

.PHONY: validate
validate: node-tools validate-packer ## Run all validations (packer + python tooling)
	@echo 'All validations passed.'

