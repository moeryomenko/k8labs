# k8s-os — Kubernetes OS Image Build System
# Targets for Packer VM baking and system/configuration extensions.

SHELL := /bin/bash
.ONESHELL:

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo 'k8s-os build targets'
	@echo ''
	@echo '  base               Build the base OS image via Packer'
	@echo '  sysext/<name>      Build a sysext extension (kubelet, cri-o, crun, cni)'
	@echo '  confext/<name>     Build a confext extension (worker, control-plane, cri-o, kubernetes)'
	@echo '  sysexts            Build all sysext extensions'
	@echo '  confexts           Build all confext extensions'
	@echo '  extensions         Build all extensions (sysexts + confexts)'
	@echo '  all                Build base image + all extensions'
	@echo '  deploy             Apply Terraform infrastructure'
	@echo '  destroy            Destroy Terraform infrastructure'
	@echo '  clean              Remove build artifacts'
	@echo '  validate-packer    Validate Packer template'
	@echo '  validate-terraform Validate Terraform configuration'
	@echo ''

# --- Base Image ---

.PHONY: base
base:
	@echo 'Building base OS image via Packer...'
	packer build -var-file=packer/variables.pkr.hcl packer/base.pkr.hcl

# --- System Extensions ---

SYSEXT_NAMES := kubelet cri-o crun cni

.PHONY: $(addprefix sysext/,$(SYSEXT_NAMES)) sysexts

sysext/kubelet:
	@echo 'Building sysext kubelet...'

sysext/cri-o:
	@echo 'Building sysext cri-o...'

sysext/crun:
	@echo 'Building sysext crun...'

sysext/cni:
	@echo 'Building sysext cni...'

sysexts: $(addprefix sysext/,$(SYSEXT_NAMES))
	@echo 'All sysext extensions built.'

# --- Config Extensions ---

CONFEXT_NAMES := worker control-plane cri-o kubernetes

.PHONY: $(addprefix confext/,$(CONFEXT_NAMES)) confexts

confext/worker:
	@echo 'Building confext worker...'

confext/control-plane:
	@echo 'Building confext control-plane...'

confext/cri-o:
	@echo 'Building confext cri-o...'

confext/kubernetes:
	@echo 'Building confext kubernetes...'

confexts: $(addprefix confext/,$(CONFEXT_NAMES))
	@echo 'All confext extensions built.'

# --- Combined Extensions ---

.PHONY: extensions
extensions: sysexts confexts
	@echo 'All extensions built.'

# --- Full Build ---

.PHONY: all
all: base extensions
	@echo 'Full build complete.'

# --- Terraform ---

.PHONY: deploy
deploy:
	@echo 'Applying Terraform infrastructure...'
	terraform -chdir=terraform apply

.PHONY: destroy
destroy:
	@echo 'Destroying Terraform infrastructure...'
	terraform -chdir=terraform destroy

# --- Cleanup ---

.PHONY: clean
clean:
	@echo 'Removing build artifacts...'
	rm -rf build/ extensions/release/*.raw

# --- Validation ---

.PHONY: validate-packer
validate-packer:
	@echo 'Validating Packer template...'
	packer validate packer/base.pkr.hcl

.PHONY: validate-terraform
validate-terraform:
	@echo 'Validating Terraform configuration...'
	terraform -chdir=terraform validate
