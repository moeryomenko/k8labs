# k8labs CPU Throttling Research — Makefile
# Targets for running experiments and generating reports
#
# Usage:
#   make setup                      — verify prerequisites
#   make experiment-baseline        — run throttling-baseline experiment
#   make experiment-limits          — run throttling-limits experiment
#   make experiment-request-limit   — run throttling-request-limit experiment
#   make experiment-colocated       — run co-located experiment
#   make all-experiments            — run all experiments sequentially
#   make clean                      — remove all experiment data
#   make report                     — analyze results (placeholder)

SHELL := /bin/bash
.ONESHELL:

# Project root (auto-detected)
PROJECT_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)
EXPERIMENTS_DIR := $(PROJECT_ROOT)/research/experiments
CONFIGS_DIR := $(EXPERIMENTS_DIR)/configs
RUNNER := $(EXPERIMENTS_DIR)/run-experiment.sh

# Resolve KUBECONFIG path
KUBECONFIG := $(PROJECT_ROOT)/kubeconfig
export KUBECONFIG

# ---- Help ----
.DEFAULT_GOAL := help

.PHONY: help
help: ## Prints this help message
	@echo "Commands:"
	@grep -F -h '##' $(MAKEFILE_LIST) \
		| grep -F -v fgrep \
		| sort \
		| grep -E '^[a-zA-Z_/.-]+:.*?## .*$$' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'

# ---- Config Validation ----

.PHONY: validate-configs
validate-configs: ## Validate all experiment config files
	@echo "Validating experiment configs..."
	@for cfg in $(CONFIGS_DIR)/*.yaml; do \
		echo "  Checking: $$(basename $$cfg)"; \
		$(RUNNER) "$$cfg" --dry-run 2>&1 | head -20; \
		echo "  ---"; \
	done
	@echo "All configs validated."

# ---- Setup ----

.PHONY: setup
setup: ## Verify prerequisites (kubectl access, tools, tofu state)
	@echo "=== k8labs Experiment Framework — Prerequisites ==="
	@echo ""
	@echo "--- Required Tools ---"
	fail=0
	for cmd in kubectl jq python3 sed grep timeout; do
		if command -v $$cmd &>/dev/null; then
			echo "  [OK] $$cmd"
		else
			echo "  [MISSING] $$cmd"
			fail=1
		fi
	done
	if command -v tofu &>/dev/null; then
		echo "  [OK] tofu (OpenTofu)"
	elif command -v terraform &>/dev/null; then
		echo "  [OK] terraform"
	else
		echo "  [MISSING] tofu or terraform"
		fail=1
	fi
	if [ "$$fail" -ne 0 ]; then
		echo ""
		echo "ERROR: One or more required tools are missing." >&2
		exit 1
	fi
	echo ""
	echo "--- Cluster Access ---"
	if [ ! -f "$(KUBECONFIG)" ]; then
		echo "  WARNING: kubeconfig not found at $(KUBECONFIG)"
		echo "  Run 'make kubeconfig' from project root first." >&2
		exit 1
	fi
	export KUBECONFIG=$(KUBECONFIG)
	if kubectl cluster-info --request-timeout=5s 2>/dev/null | head -3; then
		echo "  [OK] Cluster reachable"
	else
		echo "  [FAIL] Cannot reach cluster" >&2
		exit 1
	fi
	echo ""
	echo "--- Worker Nodes ---"
	nodes=$$(kubectl get nodes --no-headers 2>/dev/null | awk '{print $$1" "$$2}')
	if [ -z "$$nodes" ]; then
		echo "  [FAIL] No nodes found" >&2
		exit 1
	fi
	echo "$$nodes" | while read name status; do
		if [ "$$status" = "Ready" ]; then
			echo "  [OK] $$name (Ready)"
		else
			echo "  [WARN] $$name ($$status)"
		fi
	done
	echo ""
	echo "--- Terraform State ---"
	if command -v tofu &>/dev/null; then
		tofu -chdir=$(PROJECT_ROOT)/terraform output worker_ips 2>/dev/null | head -1 || echo "  [WARN] Could not read Terraform state"
	elif command -v terraform &>/dev/null; then
		terraform -chdir=$(PROJECT_ROOT)/terraform output worker_ips 2>/dev/null | head -1 || echo "  [WARN] Could not read Terraform state"
	fi
	echo ""
	echo "=== Setup complete ==="

# ---- Individual Experiments ----

.PHONY: experiment-baseline
experiment-baseline: ## Run throttling-baseline experiment
	@echo "Running throttling-baseline experiment..."
	$(RUNNER) $(CONFIGS_DIR)/throttling-baseline.yaml

.PHONY: experiment-limits
experiment-limits: ## Run throttling-limits experiment
	@echo "Running throttling-limits experiment..."
	$(RUNNER) $(CONFIGS_DIR)/throttling-limits.yaml

.PHONY: experiment-request-limit
experiment-request-limit: ## Run throttling-request-limit experiment
	@echo "Running throttling-request-limit experiment..."
	$(RUNNER) $(CONFIGS_DIR)/throttling-request-limit.yaml

.PHONY: experiment-colocated
experiment-colocated: ## Run co-located experiment
	@echo "Running co-located experiment..."
	$(RUNNER) $(CONFIGS_DIR)/co-located.yaml

# ---- All Experiments ----

.PHONY: all-experiments
all-experiments: ## Run all experiments sequentially
	@echo "=== Running all experiments ==="
	@echo ""
	@echo "=== 1/4: Baseline ==="
	$(RUNNER) $(CONFIGS_DIR)/throttling-baseline.yaml
	@echo ""
	@echo "=== 2/4: Limits ==="
	$(RUNNER) $(CONFIGS_DIR)/throttling-limits.yaml
	@echo ""
	@echo "=== 3/4: Request-Limit ==="
	$(RUNNER) $(CONFIGS_DIR)/throttling-request-limit.yaml
	@echo ""
	@echo "=== 4/4: Co-located ==="
	$(RUNNER) $(CONFIGS_DIR)/co-located.yaml
	@echo ""
	@echo "=== All experiments complete ==="

# ---- Report ----

.PHONY: report
report: ## Generate analysis report from experiment data
	@echo 'Generating analysis report...'
	@if [ ! -d $(EXPERIMENTS_DIR)/data ] || [ -z "$$(find $(EXPERIMENTS_DIR)/data -name 'summary.csv' 2>/dev/null)" ]; then \
		echo "  No experiment data found. Run experiments first with 'make experiment-*'"; \
		exit 0; \
	fi
	analysis/analyze-throttling.sh $(EXPERIMENTS_DIR)/data --output-dir analysis/output
	python3 analysis/plot-throttling.py analysis/output/aggregates.csv --output-dir analysis/output
	@echo '  Report generated in analysis/output/'
	@echo '  Files: aggregates.csv, summary.json, *.png'

.PHONY: report-view
report-view: report ## Generate report and open plots (if display available)
	@if command -v xdg-open &>/dev/null; then \
		for f in analysis/output/*.png; do \
			xdg-open "$$f" 2>/dev/null || true; \
		done; \
	elif command -v open &>/dev/null; then \
		for f in analysis/output/*.png; do \
			open "$$f" 2>/dev/null || true; \
		done; \
	else \
		echo "  No image viewer found. Open analysis/output/*.png manually."; \
	fi

# ---- Clean ----

.PHONY: clean
clean: ## Remove all experiment data (prompts for confirmation)
	@echo "WARNING: This will remove all experiment data directories and summary CSVs."
	@read -t 30 -r -p "Are you sure? [y/N] " confirm; \
	case "$$confirm" in \
		[yY]|[yY][eE][sS]) ;; \
		*) echo "  Aborted."; exit 1 ;; \
	esac
	@echo "Removing experiment data..."
	@if [ -d "$(EXPERIMENTS_DIR)/data" ]; then \
		rm -rf $(EXPERIMENTS_DIR)/data; \
		echo "  Removed: $(EXPERIMENTS_DIR)/data"; \
	else \
		echo "  No data directory found."; \
	fi
	@echo "Clean complete."
