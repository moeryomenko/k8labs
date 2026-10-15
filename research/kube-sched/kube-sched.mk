# k8labs Kubernetes-scheduler Research — kube-sched series Makefile
#
# Studies kube-scheduler PLACEMENT behavior on the lab cluster (queue -> filter
# -> score -> bind) while holding the guest Linux scheduler fixed on default
# EEVDF. Independent of linux-sched: separate commands, data, profiles, and
# analysis.
#
# All targets are namespaced `kube-sched-*` and discovered by the research/
# aggregator through `include $(wildcard */*.mk)`.

SHELL := /bin/bash
.ONESHELL:

PROJECT_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

KUBE_SCHED_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
KUBE_SCHED_EXPERIMENTS_DIR := $(KUBE_SCHED_DIR)experiments
KUBE_SCHED_RUNNER := $(KUBE_SCHED_EXPERIMENTS_DIR)/run-placement.sh
KUBE_SCHED_PROFILES_DIR := $(KUBE_SCHED_DIR)profiles
KUBE_SCHED_ANALYSIS_DIR := $(KUBE_SCHED_DIR)analysis
KUBE_SCHED_TEST_DIR := $(KUBE_SCHED_DIR)tests

KUBE_SCHED_DATA_ROOT ?= $(if $(K8LABS_RESEARCH_DATA_ROOT),$(K8LABS_RESEARCH_DATA_ROOT)/kube-sched,$(PROJECT_ROOT)/build/research/kube-sched)
KUBE_SCHED_ANALYSIS_OUTPUT ?= $(KUBE_SCHED_DATA_ROOT)/analysis

KUBECONFIG := $(PROJECT_ROOT)/build/kubeconfig
export KUBECONFIG

.PHONY: kube-sched-setup
kube-sched-setup: ## Verify kube-sched prerequisites (kubectl, workload cluster)
	@echo "=== kube-sched — prerequisites ==="
	@for cmd in kubectl python3; do \
		command -v $$cmd >/dev/null 2>&1 && echo "  [OK] $$cmd" || { echo "  [MISSING] $$cmd" >&2; exit 1; }; \
	done
	@test -f "$(KUBECONFIG)" || { echo "  [MISSING] workload kubeconfig $(KUBECONFIG)" >&2; exit 1; }
	@kubectl --kubeconfig "$(KUBECONFIG)" get nodes --no-headers >/dev/null 2>&1 \
		&& echo "  [OK] workload cluster reachable" \
		|| { echo "  [FAIL] cannot reach workload cluster" >&2; exit 1; }
	@echo "  Data root: $(KUBE_SCHED_DATA_ROOT)"

.PHONY: kube-sched-validate
kube-sched-validate: ## Validate kube-sched scheduler profiles (no cluster needed)
	@$(KUBE_SCHED_RUNNER) --validate --profiles-dir $(KUBE_SCHED_PROFILES_DIR)

.PHONY: kube-sched-plan
kube-sched-plan: ## Print the seeded placement block schedule (no cluster needed)
	@$(KUBE_SCHED_RUNNER) plan --profiles-dir $(KUBE_SCHED_PROFILES_DIR)

.PHONY: kube-sched-experiment
kube-sched-experiment: ## Run placement blocks for one matrix (MATRIX=<name>)
	@if [ -z "$(MATRIX)" ]; then echo "ERROR: set MATRIX=<name> (see kube-sched-validate)" >&2; exit 1; fi
	$(KUBE_SCHED_RUNNER) run --matrix $(MATRIX) --profiles-dir $(KUBE_SCHED_PROFILES_DIR) --data-root $(KUBE_SCHED_DATA_ROOT)

.PHONY: kube-sched-analyze
kube-sched-analyze: ## Analyze placement blocks into a summary
	@if [ ! -f "$(KUBE_SCHED_DATA_ROOT)/pods.csv" ]; then echo "No pods.csv under $(KUBE_SCHED_DATA_ROOT); run an experiment first."; exit 0; fi
	python3 $(KUBE_SCHED_ANALYSIS_DIR)/placement_analysis.py --pods $(KUBE_SCHED_DATA_ROOT)/pods.csv --output-dir $(KUBE_SCHED_ANALYSIS_OUTPUT)

.PHONY: kube-sched-test
kube-sched-test: ## Run the kube-sched test suite
	@echo "Running kube-sched test suite..."
	uv run pytest $(KUBE_SCHED_ANALYSIS_DIR)/tests/
	@if command -v bats >/dev/null 2>&1; then bats $(KUBE_SCHED_TEST_DIR)/*.bats; else echo "  bats not installed; skipping shell tests"; fi

.PHONY: kube-sched-clean
kube-sched-clean: ## Remove kube-sched experiment data (host-owned data root)
	@rm -rf "$(KUBE_SCHED_DATA_ROOT)"
	@echo "Clean complete."
