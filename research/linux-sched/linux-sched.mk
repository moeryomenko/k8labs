# k8labs Linux CPU-scheduler Research — linux-sched series Makefile
#
# Compares the default EEVDF scheduler against sched_ext implementations
# (scx_bpfland, scx_lavd, experimental scx_mavd) on a pinned worker VM using a
# randomized, seeded paired-block crossover. See README.md for the protocol and
# the approved plan for the frozen decision rule.
#
# All targets are namespaced `linux-sched-*` and are discovered by the
# research/ aggregator through `include $(wildcard */*.mk)`.

SHELL := /bin/bash
.ONESHELL:

PROJECT_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

# Series root: the directory holding this makefile (trailing slash).
LINUX_SCHED_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
LINUX_SCHED_EXPERIMENTS_DIR := $(LINUX_SCHED_DIR)experiments
LINUX_SCHED_RUNNER := $(LINUX_SCHED_EXPERIMENTS_DIR)/run-block.sh
LINUX_SCHED_CONFIGS_DIR := $(LINUX_SCHED_EXPERIMENTS_DIR)/scenarios
LINUX_SCHED_CAPTURE_DIR := $(LINUX_SCHED_DIR)capture
LINUX_SCHED_ANALYSIS_DIR := $(LINUX_SCHED_DIR)analysis
LINUX_SCHED_TEST_DIR := $(LINUX_SCHED_DIR)tests

# Host-owned data root, deliberately outside the Git worktree. Override with
# K8LABS_RESEARCH_DATA_ROOT to relocate every series' experiment data.
LINUX_SCHED_DATA_ROOT ?= $(if $(K8LABS_RESEARCH_DATA_ROOT),$(K8LABS_RESEARCH_DATA_ROOT)/linux-sched,$(PROJECT_ROOT)/build/research/linux-sched)
LINUX_SCHED_ANALYSIS_OUTPUT ?= $(LINUX_SCHED_DATA_ROOT)/analysis

# Resolve KUBECONFIG path (workload cluster).
KUBECONFIG := $(PROJECT_ROOT)/build/kubeconfig
export KUBECONFIG

# ---- Setup / validation ----

.PHONY: linux-sched-setup
linux-sched-setup: ## Verify linux-sched prerequisites (tools, cluster, worker VM access)
	@echo "=== linux-sched — prerequisites ==="
	@fail=0; \
	for cmd in kubectl jq python3 ssh; do \
		if command -v $$cmd >/dev/null 2>&1; then echo "  [OK] $$cmd"; else echo "  [MISSING] $$cmd"; fail=1; fi; \
	done; \
	if [ "$$fail" -ne 0 ]; then echo "ERROR: missing required tools" >&2; exit 1; fi; \
	if [ ! -f "$(KUBECONFIG)" ]; then echo "  [MISSING] workload kubeconfig $(KUBECONFIG)" >&2; exit 1; fi; \
	if kubectl --kubeconfig "$(KUBECONFIG)" get nodes --no-headers >/dev/null 2>&1; then \
		echo "  [OK] workload cluster reachable"; \
	else \
		echo "  [FAIL] cannot reach workload cluster" >&2; exit 1; \
	fi
	@echo "  Data root: $(LINUX_SCHED_DATA_ROOT)"
	@if [ -e "$(LINUX_SCHED_DATA_ROOT)" ]; then \
		case "$(LINUX_SCHED_DATA_ROOT)" in \
			$(PROJECT_ROOT)/*) echo "ERROR: data root must live outside the Git worktree" >&2; exit 1;; \
		esac; \
	fi
	@echo "=== prerequisites satisfied ==="

.PHONY: linux-sched-validate
linux-sched-validate: ## Validate linux-sched scenario configs (no cluster needed)
	@echo "Validating linux-sched scenario configs..."
	@$(LINUX_SCHED_RUNNER) --validate --configs-dir $(LINUX_SCHED_CONFIGS_DIR)
	@echo "All linux-sched configs validated."

.PHONY: linux-sched-list-scenarios
linux-sched-list-scenarios: ## List linux-sched workload scenarios
	@$(LINUX_SCHED_RUNNER) --list --configs-dir $(LINUX_SCHED_CONFIGS_DIR)

# ---- Experiments ----

.PHONY: linux-sched-pilot
linux-sched-pilot: ## Run a pilot block set for one scenario (SCENARIO=<name>)
	@if [ -z "$(SCENARIO)" ]; then echo "ERROR: set SCENARIO=<name> (see linux-sched-list-scenarios)" >&2; exit 1; fi
	$(LINUX_SCHED_RUNNER) run --scenario $(SCENARIO) --mode pilot --configs-dir $(LINUX_SCHED_CONFIGS_DIR) --data-root $(LINUX_SCHED_DATA_ROOT)

.PHONY: linux-sched-experiment
linux-sched-experiment: ## Run confirmatory paired blocks for one scenario (SCENARIO=<name>)
	@if [ -z "$(SCENARIO)" ]; then echo "ERROR: set SCENARIO=<name> (see linux-sched-list-scenarios)" >&2; exit 1; fi
	$(LINUX_SCHED_RUNNER) run --scenario $(SCENARIO) --mode confirmatory --configs-dir $(LINUX_SCHED_CONFIGS_DIR) --data-root $(LINUX_SCHED_DATA_ROOT)

.PHONY: linux-sched-overhead
linux-sched-overhead: ## Run the observer-overhead calibration for one scenario (SCENARIO=<name>)
	@if [ -z "$(SCENARIO)" ]; then echo "ERROR: set SCENARIO=<name>" >&2; exit 1; fi
	$(LINUX_SCHED_RUNNER) overhead --scenario $(SCENARIO) --configs-dir $(LINUX_SCHED_CONFIGS_DIR) --data-root $(LINUX_SCHED_DATA_ROOT)

# ---- Analysis ----

.PHONY: linux-sched-analyze
linux-sched-analyze: ## Analyze paired blocks into summary.csv/json with the frozen decision rule
	@if [ ! -f "$(LINUX_SCHED_DATA_ROOT)/blocks.csv" ]; then echo "No blocks.csv under $(LINUX_SCHED_DATA_ROOT); run an experiment first."; exit 0; fi
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/sched_analysis.py --blocks $(LINUX_SCHED_DATA_ROOT)/blocks.csv --output-dir $(LINUX_SCHED_ANALYSIS_OUTPUT)
	@echo "  Summary: $(LINUX_SCHED_ANALYSIS_OUTPUT)/summary.csv"

.PHONY: linux-sched-plots
linux-sched-plots: ## Generate linux-sched plots from analyzed data
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/sched_plots.py --data-root $(LINUX_SCHED_DATA_ROOT) --output-dir $(LINUX_SCHED_ANALYSIS_OUTPUT)/plots

.PHONY: linux-sched-report
linux-sched-report: linux-sched-analyze ## Print the linux-sched analysis report pointer
	@echo "linux-sched analysis written to $(LINUX_SCHED_ANALYSIS_OUTPUT)"
	@echo "  summary.csv  — decision rule verdicts"
	@echo "  summary.json — schema-versioned machine-readable verdicts"

# ---- Node-native observer tooling ----

.PHONY: linux-sched-observer-check
linux-sched-observer-check: ## Check the node image installs the native observer tooling (no cluster needed)
	@echo "Checking node-native observer tooling provisioning..."
	python3 $(LINUX_SCHED_EXPERIMENTS_DIR)/observer_check.py

# ---- Test / clean ----

.PHONY: linux-sched-test
linux-sched-test: ## Run the linux-sched test suite (pytest + bats)
	@set -e
	@echo "Running linux-sched test suite..."
	uv run pytest $(LINUX_SCHED_ANALYSIS_DIR)/tests/ $(LINUX_SCHED_EXPERIMENTS_DIR)/tests/
	@if command -v bats >/dev/null 2>&1; then bats $(LINUX_SCHED_TEST_DIR)/*.bats; else echo "  bats not installed; skipping shell tests"; fi

.PHONY: linux-sched-clean
linux-sched-clean: ## Remove linux-sched experiment data (host-owned data root)
	@echo "Removing linux-sched data under $(LINUX_SCHED_DATA_ROOT)..."
	@rm -rf "$(LINUX_SCHED_DATA_ROOT)"
	@echo "Clean complete."
