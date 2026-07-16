# k8labs — Kubernetes OS Image Build System
# Targets for Packer VM baking and system/configuration extensions.

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

.PHONY: base
base: ## Build the base OS image via Packer
	@echo 'Building base OS image via Packer...'
	cd packer && packer build .
	@echo 'Copying base image to build/ for Terraform consumption...'
	mkdir -p build
	cp packer/build/base/k8labs-base build/k8labs-base.qcow2

# --- System Extensions ---

SYSEXT_NAMES := kubelet cri-o crun cni

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

sysexts: download-sysexts $(addprefix sysext/,$(SYSEXT_NAMES)) ## Build all sysext extensions
	@echo 'All sysext extensions built.'

# --- Config Extensions ---

CONFEXT_NAMES := worker control-plane cri-o kubernetes

.PHONY: $(addprefix confext/,$(CONFEXT_NAMES)) confexts

confext/worker: ## Build confext worker configuration overlay
	@echo 'Building confext worker...'

confext/control-plane: ## Build confext control-plane configuration overlay
	@echo 'Building confext control-plane...'

confext/cri-o: ## Build confext cri-o configuration overlay
	@echo 'Building confext cri-o...'

confext/kubernetes: ## Build confext kubernetes configuration overlay
	@echo 'Building confext kubernetes...'

confexts: $(addprefix confext/,$(CONFEXT_NAMES)) ## Build all confext extensions
	@echo 'All confext extensions built.'

# --- Combined Extensions ---

.PHONY: extensions
extensions: sysexts confexts ## Build all extensions (sysexts + confexts)
	@echo 'All extensions built.'

# --- Full Build ---

.PHONY: all
all: base extensions ## Build base image + all extensions
	@echo 'Full build complete.'

# --- Terraform ---

.PHONY: deploy
deploy: ## Apply Terraform infrastructure
	@echo 'Applying Terraform infrastructure...'
	terraform -chdir=terraform apply -var="base_image_path=../build/k8labs-base.qcow2"

.PHONY: destroy
destroy: ## Destroy Terraform infrastructure
	@echo 'Destroying Terraform infrastructure...'
	terraform -chdir=terraform destroy

# --- Cleanup ---

.PHONY: clean
clean: ## Remove build artifacts
	@echo 'Removing build artifacts...'
	rm -rf build/ extensions/release/*.raw

# --- Validation ---

.PHONY: validate-packer
validate-packer: ## Validate Packer template
	@echo 'Validating Packer template...'
	cd packer && packer validate .

.PHONY: validate-terraform
validate-terraform: ## Validate Terraform configuration
	@echo 'Validating Terraform configuration...'
	terraform -chdir=terraform validate

.PHONY: validate
validate: validate-packer validate-terraform ## Run all validations (packer + terraform)
	@echo 'All validations passed.'
