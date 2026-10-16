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
# All dependency versions are recorded in versions.lock.yaml. Keep the Packer
# plugin immutable: mutable branches make a golden image unreproducible.
PACKER_VERSION ?= 1.16.1
PACKER_PLUGIN_REF ?= e902aef57dadb47f0dfe40c9a8ade9cd9fb50d2f
KUBERNETES_VERSION ?= v1.37.0
CRIO_VERSION ?= v1.37.0
CRUN_VERSION ?= 1.29.1
CILIUM_VERSION ?= v1.20.2
BAKE_IMAGE := localhost/k8labs-bake:dev

.PHONY: bake-image
bake-image: ## Build the rootless bake container image (packer + CH plugin + bake-net.sh)
	@echo '==> Building bake container image ($(BAKE_IMAGE))...'
	podman build -t "$(BAKE_IMAGE)" \
		--build-arg PACKER_PLUGIN_REF="$(PACKER_PLUGIN_REF)" \
		bake/

# `make plugin` remains an alias for the bake image target.
.PHONY: plugin plugin-rebuild
plugin: bake-image ## Build/refresh the bake container image
plugin-rebuild: bake-image ## Force refresh of the bake container image

.PHONY: versions-check
versions-check: ## Validate the immutable dependency lock and reject mutable references
	@python3 scripts/lock.py --lock versions.lock.yaml

K8LABS_CACHE_ROOT ?= $${XDG_CACHE_HOME:-$${HOME}/.cache}/k8labs
LOCK_DIGEST_FILE := $(K8LABS_CACHE_ROOT)/lock.sha256

.PHONY: prepare-render-capi
prepare-render-capi: versions-check ## Normalize a verified CAPI release manifest before cache validation
	@set -Eeuo pipefail; \
	input="$${CAPI_CORE_COMPONENTS:-$${XDG_CACHE_HOME:-$${HOME}/.cache}/k8labs/bootstrap/cluster-api-v1.14.2/core-components.yaml}"; \
	output="$${CAPI_RENDERED_OUTPUT:-$${XDG_CACHE_HOME:-$${HOME}/.cache}/k8labs/manifests/cluster-api-v1.14.2-core-components.yaml}"; \
	test -f "$$input" || { echo "ERROR: verified CAPI manifest not found: $$input" >&2; exit 1; }; \
	python3 scripts/normalize_manifest.py "$$input" "$$output"; \
	printf 'normalized CAPI manifest: %s\\n' "$$output"

.PHONY: prepare-check
prepare-check: versions-check ## Require a verified artifact cache for offline execution
	@set -Eeuo pipefail; \
	lock_sha=$$(python3 scripts/lock.py --lock versions.lock.yaml | sed -n 's/^lock_sha256=//p'); \
	python3 scripts/offline.py --cache-root "$(K8LABS_CACHE_ROOT)" --lock-digest "$$lock_sha"

.PHONY: prepare-prune
prepare-prune: versions-check ## Remove cache keys not referenced by the active lock
	@python3 scripts/cache.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --prune

.PHONY: registry-install registry-up registry-down registry-check
registry-install: ## Install the k8labs rootless registry quadlet
	@set -Eeuo pipefail; \
	qdir="$${XDG_CONFIG_HOME:-$${HOME}/.config}/containers/systemd"; \
	mkdir -p "$$qdir" "$${HOME}/.local/state/k8slab/registry"; \
	install -m 0644 quadlet/registry.container "$$qdir/k8slabs-registry.container"; \
	systemctl --user daemon-reload

registry-up: registry-install ## Start the local registry
	@systemctl --user start k8slabs-registry.service

registry-down: ## Stop the local registry without deleting its state
	@systemctl --user stop k8slabs-registry.service || true

registry-check: ## Verify the local registry API is reachable
	@python3 -c 'import urllib.request; response=urllib.request.urlopen("http://127.0.0.1:5000/v2/", timeout=5); assert response.status in (200, 401)'

.PHONY: prepare
prepare: versions-check ## Download and verify artifacts into the lock-keyed cache
	@set -Eeuo pipefail; \
	mkdir -p "$(K8LABS_CACHE_ROOT)"; \
	python3 scripts/cache.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --populate

.PHONY: run-bundle prune-runs
run-bundle: ## Create a redacted schema-versioned run bundle
	@python3 scripts/run_bundle.py --lock versions.lock.yaml $(if $(RUN_ID),--run-id "$(RUN_ID)",)

prune-runs: ## Remove successful run bundles older than RUN_RETENTION_DAYS
	@set -Eeuo pipefail; \
	days="$${RUN_RETENTION_DAYS:-14}"; \
	find build/runs -mindepth 1 -maxdepth 1 -type d -mtime "+$$days" -exec rm -rf -- {} + 2>/dev/null || true

.PHONY: kind-up kind-down kind-check
MGMT_CLUSTER_NAME ?= k8labs-mgmt
MGMT_CONTEXT ?= kind-$(MGMT_CLUSTER_NAME)
MGMT_FORCE ?= 0

kind-up: prepare-check ## Create or reuse the cached-image Kind management cluster
	@set -Eeuo pipefail; \
	command -v kind >/dev/null 2>&1 || { echo "ERROR: kind is required; install the locked kind version" >&2; exit 1; }; \
	python3 scripts/load_management_images.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --image-id kind-node; \
	if kind get clusters 2>/dev/null | grep -qx "$(MGMT_CLUSTER_NAME)"; then \
		echo 'Kind management cluster already exists'; \
		kind export kubeconfig --name "$(MGMT_CLUSTER_NAME)"; \
	else \
		kind create cluster --name "$(MGMT_CLUSTER_NAME)" --config kind.yaml; \
	fi; \
	kubectl config get-contexts -o name | grep -qx "$(MGMT_CONTEXT)" || { \
		echo "ERROR: Kind cluster $(MGMT_CLUSTER_NAME) did not provide context $(MGMT_CONTEXT)" >&2; \
		exit 1; \
	}

kind-down: ## Delete the owned Kind management cluster only when it has no CAPI Clusters (MGMT_FORCE=1 overrides)
	@set -Eeuo pipefail; \
	if ! kind get clusters 2>/dev/null | grep -qx "$(MGMT_CLUSTER_NAME)"; then \
		echo 'Kind management cluster does not exist'; \
		exit 0; \
	fi; \
	if [ "$(MGMT_FORCE)" != 1 ] && kubectl --context "$(MGMT_CONTEXT)" get clusters.cluster.x-k8s.io --no-headers 2>/dev/null | grep -q .; then \
		echo 'ERROR: refusing to delete management cluster while CAPI Cluster objects remain; run make cluster-down or set MGMT_FORCE=1' >&2; \
		exit 1; \
	fi; \
	kind delete cluster --name "$(MGMT_CLUSTER_NAME)"

kind-check: ## Check Kind management-cluster reachability
	@kubectl --context "$(MGMT_CONTEXT)" get nodes --no-headers

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
# The management API is the owned Kind cluster. The provider manager runs in
# that cluster with no host mounts; the HostAgent is a separate rootless host
# service and is the only component that receives KVM and user-session inputs.

CLUSTER_NAME ?= k8labs
WORKLOAD_KUBECONFIG := build/kubeconfig
HOST_AGENT_CONFIG ?= $${XDG_CONFIG_HOME:-$${HOME}/.config}/k8labs/host-agent

.PHONY: host-agent-install host-agent-up host-agent-check
host-agent-install: prepare-check ## Render the separately managed HostAgent service from locked inputs
	@python3 scripts/load_management_images.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --image-id cluster-api-hypervisor
	@python3 scripts/host_agent.py install --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --config-dir "$(HOST_AGENT_CONFIG)"

host-agent-up: host-agent-install ## Start the separately managed HostAgent service
	@python3 scripts/host_agent.py start --config-dir "$(HOST_AGENT_CONFIG)"

host-agent-check: ## Verify HostAgent service state and required local inputs
	@python3 scripts/host_agent.py check --config-dir "$(HOST_AGENT_CONFIG)"

.PHONY: prereq
prereq: ## Validate tools, KVM access, baked artifacts, and the locked management cache
	@set -euo pipefail; \
	fail=0; \
	for cmd in cloud-hypervisor openssl jq python3 clusterctl kubectl podman kind skopeo; do \
		if ! command -v $$cmd >/dev/null 2>&1; then \
			echo "ERROR: required tool '$$cmd' not found on PATH" >&2; \
			fail=1; \
		fi; \
	done; \
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
	if [ ! -f "$(BASE_IMAGE_DEST)" ]; then \
		echo "ERROR: base image '$(BASE_IMAGE_DEST)' not found - run 'make base' to bake it first" >&2; \
		fail=1; \
	fi; \
	if [ ! -f "$(FIRMWARE_DEST)" ]; then \
		echo "ERROR: firmware '$(FIRMWARE_DEST)' not found - run 'make base' to fetch it first" >&2; \
		fail=1; \
	fi; \
	exit $$fail

.PHONY: mgmt-bootstrap mgmt-check mgmt-up mgmt-down
mgmt-bootstrap: kind-up host-agent-up ## Bootstrap cert-manager, CAPI, and the hardened provider manager in Kind
	@set -Eeuo pipefail; \
	python3 scripts/load_management_images.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --image-id cluster-api-hypervisor --kind-name "$(MGMT_CLUSTER_NAME)"; \
	python3 scripts/bootstrap_management.py --lock versions.lock.yaml --cache-root "$(K8LABS_CACHE_ROOT)" --context "$(MGMT_CONTEXT)" --host-agent-config "$(HOST_AGENT_CONFIG)"

mgmt-check: kind-check host-agent-check ## Verify management controllers and HostAgent connectivity
	@set -Eeuo pipefail; \
	kubectl --context "$(MGMT_CONTEXT)" -n cert-manager wait --for=condition=Available deployment --all --timeout=5m; \
	kubectl --context "$(MGMT_CONTEXT)" -n hypervisor-system wait --for=condition=Available deployment/controller-manager --timeout=5m; \
	python3 scripts/host_agent.py check --config-dir "$(HOST_AGENT_CONFIG)"

mgmt-up: prereq mgmt-bootstrap mgmt-check ## Create the Kind management plane and bootstrap its controllers

mgmt-down: kind-down ## Destructive alias for kind-down; HostAgent state is preserved

.PHONY: provider-loopback-forward
provider-loopback-forward: ## Work around the pinned provider dialing loopback (see research/linux-sched/GATES.md)
	@python3 scripts/provider_loopback_forward.py --context "$(MGMT_CONTEXT)"

.PHONY: cluster-up
cluster-up: ## Server-side apply capi/cluster.yaml against the Kind management plane (idempotent)
	@set -euo pipefail; \
	echo '==> Applying capi/cluster.yaml (server-side) to the management plane...'; \
	kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f capi/cluster.yaml; \
	echo '    Cluster manifest applied'

# --- Generated CAPI Addon Secrets ---
#
# The addon resource-set Secrets are generated from the committed source trees
# (cilium/, coredns/, rbac/) into build/capi-addons/. capi/addons/ retains
# only the three ClusterResourceSet definitions. The generated output is
# deterministic and reproducible; run `make addons-generate` to refresh it.
# build/capi-addons/ is ignored by .gitignore (build/ rule).

.PHONY: addons-generate
addons-generate: ## Generate addon resource-set Secrets into build/capi-addons/ (deterministic output)
	@set -euo pipefail; \
	echo '==> Generating addon resource-set Secrets...'; \
	uv run python scripts/generate_capi_addons.py --repo-root . --output-dir build/capi-addons; \
	echo '    Generated secrets written to build/capi-addons/'

.PHONY: addons-check
addons-check: ## Validate that generated addon secrets match the committed source trees
	@set -euo pipefail; \
	echo '==> Checking addon resource-set Secrets against source trees...'; \
	uv run python scripts/generate_capi_addons.py --repo-root . --output-dir build/capi-addons --check; \
	echo '    Addon secrets validated OK'

.PHONY: addons-clean
addons-clean: ## Remove generated addon resource-set Secrets (build/capi-addons/)
	@set -euo pipefail; \
	echo '==> Removing generated addon secrets...'; \
	rm -rf build/capi-addons; \
	echo '    Removed build/capi-addons/'

.PHONY: addons-up
addons-up: addons-generate ## Generate then apply addon resources: Secrets first, then CRS manifests (idempotent)
	@set -euo pipefail; \
	echo '==> Applying generated addon Secrets (server-side)...'; \
	kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f build/capi-addons/; \
	echo '    Generated Secrets applied'; \
	echo '==> Applying capi/addons/ ClusterResourceSets (server-side)...'; \
	kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f capi/addons/; \
	echo '    ClusterResourceSet addons applied'

.PHONY: cluster-down
cluster-down: ## Delete the workload Cluster via the management plane and wait for reclamation
	@set -euo pipefail; \
	echo '==> Deleting Cluster $(CLUSTER_NAME) via the management plane...'; \
	kubectl --context "$(MGMT_CONTEXT)" delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"; \
	echo '==> Waiting for infrastructure reclamation...'; \
	kubectl --context "$(MGMT_CONTEXT)" wait --for=delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=10m; \
	echo '    Cluster deleted and reclaimed'

.PHONY: cluster-clean
cluster-clean: ## Remove leftover workload-cluster resources on the management plane without deleting the plane itself
	@set -euo pipefail; \
	if ! command -v kubectl >/dev/null 2>&1; then \
		echo 'ERROR: kubectl not found on PATH' >&2; \
		exit 1; \
	fi; \
	echo '==> Cleaning leftover workload-cluster resources on the management plane...'; \
	kubectl --context "$(MGMT_CONTEXT)" delete hypervisormachines.infrastructure.cluster.x-k8s.io -l "cluster.x-k8s.io/cluster-name=$(CLUSTER_NAME)" --ignore-not-found; \
	kubectl --context "$(MGMT_CONTEXT)" delete hypervisormachinetemplates.infrastructure.cluster.x-k8s.io -l "cluster.x-k8s.io/cluster-name=$(CLUSTER_NAME)" --ignore-not-found; \
	echo '    Leftover workload-cluster resources cleaned'

.PHONY: cluster
cluster: mgmt-up ## Full CAPI pipeline: management bootstrap -> cluster -> addons -> ready -> kubeconfig -> smoke-test
	@set -euo pipefail; \
	$(MAKE) cluster-up; \
	$(MAKE) addons-up; \
	echo '==> Waiting for Cluster $(CLUSTER_NAME) to become Available...'; \
	kubectl --context "$(MGMT_CONTEXT)" wait --for=condition=Available "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=30m; \
	$(MAKE) kubeconfig; \
	echo '==> Waiting for all three workload Nodes to become Ready...'; \
	kubectl --kubeconfig "$(WORKLOAD_KUBECONFIG)" wait --for=condition=Ready nodes --all --timeout=10m; \
	node_count=$$(kubectl --kubeconfig "$(WORKLOAD_KUBECONFIG)" get nodes --no-headers | wc -l | tr -d '[:space:]'); \
	if [ "$$node_count" -ne 3 ]; then echo "ERROR [readiness] expected 3 workload Nodes, found $$node_count" >&2; exit 1; fi; \
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
	kubectl --context "$(MGMT_CONTEXT)" get secret "$(CLUSTER_NAME)-kubeconfig" -n default -o json > "$$secret_json"; \
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
# Resource floor: the concurrent workload VMs need roughly 14 CPU cores and
# ~24 GiB RAM free on the host (k8labs: 1 control plane at 2 vCPU/2 GiB plus 2
# workers at 2 vCPU/4 GiB; the second cluster's manifest still asks for 3
# workers), on top of the Kind/k8netd management
# plane and host-side k8netd; budget ~20-40 minutes end to end.
#
# Every per-cluster kubectl lifecycle call (readiness wait, delete,
# reclamation wait) pins --cache-dir to build/.kube-cache-<cluster>:
# concurrent kubectl runs get isolated discovery caches, and each logged
# invocation self-identifies the cluster it drives.

.PHONY: multi-cluster-test
multi-cluster-test: prereq ## Provision k8labs AND k8labs-2 concurrently, smoke-test both, tear both down (REQ-013; heavy: ~16 CPU / ~28 GiB RAM floor)
	@set -euo pipefail; \
	echo '==> Bringing up the Kind management plane...'; \
	$(MAKE) mgmt-up; \
	echo '==> Applying both Cluster manifests (server-side)...'; \
	kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f capi/cluster.yaml; \
	kubectl --context "$(MGMT_CONTEXT)" apply --server-side -f "$(MULTI_CLUSTER2_MANIFEST)"; \
	echo '    Both Cluster manifests applied'; \
	echo '==> Waiting for Cluster $(CLUSTER_NAME) to become Available...'; \
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" wait --for=condition=Available "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=30m; then \
		echo "ERROR [readiness] Cluster $(CLUSTER_NAME) did not become Available within 30m" >&2; \
		exit 1; \
	fi; \
	echo '==> Waiting for Cluster $(MULTI_CLUSTER2_NAME) to become Available...'; \
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" wait --for=condition=Available "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)" --timeout=30m; then \
		echo "ERROR [readiness] Cluster $(MULTI_CLUSTER2_NAME) did not become Available within 30m" >&2; \
		exit 1; \
	fi; \
	echo '    Both Clusters Available'; \
	mkdir -p build; \
	fetch_wc_kubeconfig() { \
		cluster="$$1"; \
		out="$$2"; \
		secret_json="$$(mktemp)"; \
		echo "==> Fetching Secret $${cluster}-kubeconfig from the management plane..."; \
		kubectl --context "$(MGMT_CONTEXT)" get secret "$${cluster}-kubeconfig" -n default -o json > "$$secret_json"; \
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
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)"; then \
		echo "ERROR [teardown] deleting Cluster $(CLUSTER_NAME) failed" >&2; \
		del_failures=" $$del_failures $(CLUSTER_NAME)"; \
	fi; \
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)"; then \
		echo "ERROR [teardown] deleting Cluster $(MULTI_CLUSTER2_NAME) failed" >&2; \
		del_failures=" $$del_failures $(MULTI_CLUSTER2_NAME)"; \
	fi; \
	echo '==> Waiting for infrastructure reclamation (both Clusters)...'; \
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(CLUSTER_NAME)" wait --for=delete "cluster.cluster.x-k8s.io/$(CLUSTER_NAME)" --timeout=10m; then \
		echo "ERROR [reclamation] Cluster $(CLUSTER_NAME) was not reclaimed within 10m" >&2; \
		del_failures=" $$del_failures $(CLUSTER_NAME)"; \
	fi; \
	if ! kubectl --context "$(MGMT_CONTEXT)" --cache-dir "build/.kube-cache-$(MULTI_CLUSTER2_NAME)" wait --for=delete "cluster.cluster.x-k8s.io/$(MULTI_CLUSTER2_NAME)" --timeout=10m; then \
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
	@echo 'Applying cilium.io CRD bundle (v1.20.2) first...'
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
validate: node-tools versions-check validate-packer ## Run all validations (dependency lock + packer + python tooling)
	@echo 'All validations passed.'
