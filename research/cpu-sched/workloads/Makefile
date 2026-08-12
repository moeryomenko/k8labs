# k8labs CPU throttling research — Workload build and deploy
# All images are built locally with podman and loaded into CRI-O.
# For cluster access, the kubeconfig file is expected at ../../kubeconfig.
#
# Usage:
#   make stress-ng-image        — build stress-ng image
#   make cpu-burner-image       — build cpu-burner image
#   make all                    — build all images
#   make stress-ng-deploy REQ=100m LIMIT=200m   — deploy stress-ng with CPU params
#   make cpu-burner-deploy REQ=100m LIMIT=200m   — deploy cpu-burner with CPU params
#   make co-located-deploy                      — deploy both co-located workloads
#   make clean                  — remove all deployments

SHELL := /bin/bash
.ONESHELL:

KUBECONFIG := ../../kubeconfig
BUILDER := podman
IMAGE_TAG := latest

# Overridable CPU resource values for template replacement.
REQ ?= 100m
LIMIT ?= 200m

# Co-located CPU values.
LS_REQ ?= 100m
LS_LIMIT ?= 200m
BATCH_REQ ?= 500m
BATCH_LIMIT ?= 1000m

.PHONY: help
help: ## Prints this help message
	@echo "Commands:"
	@grep -F -h '##' $(MAKEFILE_LIST) \
		| grep -F -v fgrep \
		| sort \
		| grep -E '^[a-zA-Z_/.-]+:.*?## .*$$' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'

# --- Build images ---

.PHONY: stress-ng-image
stress-ng-image: ## Build the stress-ng container image locally with podman
	$(BUILDER) build -t stress-ng:$(IMAGE_TAG) -f stress-ng/Containerfile stress-ng/

.PHONY: cpu-burner-image
cpu-burner-image: ## Build the Go CPU burner binary and container image
	$(MAKE) -C cpu-burner image BUILDER=$(BUILDER) IMAGE_TAG=$(IMAGE_TAG)

.PHONY: all
all: stress-ng-image cpu-burner-image ## Build all workload images

# --- Deploy ---

.PHONY: stress-ng-deploy
stress-ng-deploy: ## Deploy stress-ng with CPU request/limit (override via REQ/LIMIT)
	sed -e 's/{{CPU_REQUEST}}/$(REQ)/g' -e 's/{{CPU_LIMIT}}/$(LIMIT)/g' \
		stress-ng/deploy.yaml | kubectl --kubeconfig $(KUBECONFIG) apply -f -

.PHONY: cpu-burner-deploy
cpu-burner-deploy: ## Deploy cpu-burner with CPU request/limit (override via REQ/LIMIT)
	sed -e 's/{{CPU_REQUEST}}/$(REQ)/g' -e 's/{{CPU_LIMIT}}/$(LIMIT)/g' \
		cpu-burner/deploy.yaml | kubectl --kubeconfig $(KUBECONFIG) apply -f -

.PHONY: co-located-deploy
co-located-deploy: ## Deploy both co-located workloads (latency-sensitive + batch-burner)
	sed -e 's/{{LS_CPU_REQUEST}}/$(LS_REQ)/g' \
	    -e 's/{{LS_CPU_LIMIT}}/$(LS_LIMIT)/g' \
	    -e 's/{{BATCH_CPU_REQUEST}}/$(BATCH_REQ)/g' \
	    -e 's/{{BATCH_CPU_LIMIT}}/$(BATCH_LIMIT)/g' \
		co-located/latency-sensitive.yaml | kubectl --kubeconfig $(KUBECONFIG) apply -f -
	sed -e 's/{{BATCH_CPU_REQUEST}}/$(BATCH_REQ)/g' \
	    -e 's/{{BATCH_CPU_LIMIT}}/$(BATCH_LIMIT)/g' \
		co-located/batch-burner.yaml | kubectl --kubeconfig $(KUBECONFIG) apply -f -

# --- Clean ---

.PHONY: clean
clean: ## Remove all workload deployments from the cluster
	-kubectl --kubeconfig $(KUBECONFIG) delete pod stress-ng --ignore-not-found 2>/dev/null
	-kubectl --kubeconfig $(KUBECONFIG) delete pod cpu-burner --ignore-not-found 2>/dev/null
	-kubectl --kubeconfig $(KUBECONFIG) delete pod latency-sensitive --ignore-not-found 2>/dev/null
	-kubectl --kubeconfig $(KUBECONFIG) delete pod batch-burner --ignore-not-found 2>/dev/null
