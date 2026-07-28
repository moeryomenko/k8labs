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
PERFETTO_BIN_DIR := $(PROJECT_ROOT)/research/perfetto/bin
PERFETTO_CONFIGS_DIR := $(PROJECT_ROOT)/research/perfetto/configs
PERFETTO_ANALYSIS_DIR := $(PROJECT_ROOT)/research/analysis

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

# ---- EEVDF Experiments ----

.PHONY: experiment-eevdf-metrics
experiment-eevdf-metrics: ## Run EEVDF metrics experiment (cpu-burner, 9 runs)
	@echo "Running EEVDF metrics experiment..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/eevdf-metrics.yaml

.PHONY: experiment-eevdf-stress
experiment-eevdf-stress: ## Run EEVDF metrics experiment with stress-ng (12 runs, ~30min)
	@echo "Running EEVDF stress-ng metrics experiment..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/eevdf-metrics-stress.yaml

.PHONY: experiment-eevdf-stress-perfetto
experiment-eevdf-stress-perfetto: ## Run EEVDF stress-ng with perfetto tracing
	@echo "Running EEVDF stress-ng with perfetto..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/eevdf-metrics-stress.yaml --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-eevdf-api-baseline
experiment-eevdf-api-baseline: ## Run EEVDF API server baseline experiment
	@echo "Running EEVDF API server baseline..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/eevdf-api-baseline.yaml

.PHONY: experiment-eevdf-db-baseline
experiment-eevdf-db-baseline: ## Run EEVDF database baseline experiment
	@echo "Running EEVDF database baseline..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/eevdf-db-baseline.yaml

.PHONY: eevdf-analyze
eevdf-analyze: ## Run EEVDF analysis pipeline on experiment data
	@echo "Running EEVDF analysis..."
	@if ls $(EXPERIMENTS_DIR)/data/eevdf-metrics-stress/*/summary.csv 2>/dev/null | head -1 >/dev/null; then \
		latest=$$(ls -1t $(EXPERIMENTS_DIR)/data/eevdf-metrics-stress/ | head -1); \
		cd $(PERFETTO_ANALYSIS_DIR) && python3 eevdf-analyze.py --csv-dir $(EXPERIMENTS_DIR)/data/eevdf-metrics-stress/$$latest --output-dir ./output/eevdf-metrics-stress; \
	else \
		echo "No experiment data found. Run experiment-eevdf-stress first."; \
	fi

.PHONY: eevdf-plots
eevdf-plots: ## Generate EEVDF plots from analysis
	@echo "Generating EEVDF plots..."
	@if [ -d $(PERFETTO_ANALYSIS_DIR)/output/eevdf-metrics-stress ]; then \
		cd $(PERFETTO_ANALYSIS_DIR) && python3 eevdf-plot.py --csv-dir ./output/eevdf-metrics-stress --output-dir ./output/eevdf-metrics-stress/plots; \
	else \
		echo "No analysis data. Run eevdf-analyze first."; \
	fi

.PHONY: eevdf-report
eevdf-report: ## Generate EEVDF deep-dive report
	@echo "EEVDF deep-dive report: research/EEVDF-DEEP-DIVE.md"
	@echo "To regenerate, re-run experiments and analysis pipeline."

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
	@echo "=== 5/5: EEVDF metrics ==="
	$(RUNNER) $(CONFIGS_DIR)/eevdf-metrics.yaml
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
	@echo ''
	@echo '--- Perfetto analysis ---'
	@if find "$(EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -type f 2>/dev/null | head -1 | grep -q .; then \
		echo "  Perfetto traces found. Run 'make perfetto-analyze' and 'make perfetto-plots' for trace visualization."; \
	else \
		echo "  No Perfetto traces found."; \
	fi

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

# ---- Perfetto Tracing ----

.PHONY: perfetto-sysext
perfetto-sysext: ## Build and deploy Perfetto sysext (requires cluster)
	@echo 'Building Perfetto sysext...'
	$(MAKE) -C $(PROJECT_ROOT) sysext/perfetto
	@echo ''
	@echo 'To deploy: copy extensions/release/perfetto.raw to each node and run:'
	@echo '  systemd-sysext merge'
	@echo '  systemd-sysext list'

.PHONY: perfetto-capture
perfetto-capture: ## Capture a Perfetto trace from a node (interactive)
	@echo '=== Perfetto Trace Capture ==='
	@echo ''
	@echo 'Available configs:'
	@ls -1 $(PERFETTO_CONFIGS_DIR)/*.cfg 2>/dev/null | sed 's|.*/||;s|\.cfg$$||' | while read c; do echo "  $$c"; done
	@echo ''
	@read -p 'Node IP: ' node_ip; \
	read -p 'Config name (default: scheduling): ' config_name; \
	config_name=$${config_name:-scheduling}; \
	read -p 'Duration (seconds, default: 30): ' duration; \
	duration=$${duration:-30}; \
	output_dir=$${PWD}/perfetto-traces; \
	echo "  Capturing trace from $$node_ip (config: $$config_name, duration: $$duration)..."; \
	$(PERFETTO_BIN_DIR)/perfetto-capture.sh $$node_ip $$config_name --duration $$duration --output-dir $$output_dir; \
	echo "  Trace saved."

.PHONY: perfetto-analyze
perfetto-analyze: ## Analyze Perfetto traces in experiments/data or specified directory
	@echo 'Analyzing Perfetto traces...'
	@trace_dir=""; \
	if [ -d "$(EXPERIMENTS_DIR)/data" ]; then \
		trace_dir="$(EXPERIMENTS_DIR)/data"; \
		echo "  Searching for traces in: $$trace_dir"; \
	else \
		echo "  No experiment data found. Run experiments first."; \
		exit 0; \
	fi; \
	find "$$trace_dir" -name '*.perfetto-trace' -type f 2>/dev/null | head -5; \
	echo "Found $$(find "$$trace_dir" -name '*.perfetto-trace' -type f 2>/dev/null | wc -l) trace files"; \
	cd $(PERFETTO_ANALYSIS_DIR) && python3 perfetto-analyze.py --trace-dir "$$trace_dir" --output-dir ./perfetto-analysis; \
	echo "Analysis complete in: $(PERFETTO_ANALYSIS_DIR)/perfetto-analysis/"

.PHONY: perfetto-plots
perfetto-plots: ## Generate CPU execution time plots from Perfetto analysis
	@echo 'Generating Perfetto CPU execution time plots...'
	@if [ ! -f "$(PERFETTO_ANALYSIS_DIR)/plot-perfetto-cpu.py" ]; then \
		echo "  plot-perfetto-cpu.py not yet created"; \
		exit 0; \
	fi; \
	analysis_dir=$(PERFETTO_ANALYSIS_DIR)/perfetto-analysis; \
	if [ ! -d "$$analysis_dir" ]; then \
		echo "  No analysis data found. Run 'make perfetto-analyze' first."; \
		exit 0; \
	fi; \
	cd $(PERFETTO_ANALYSIS_DIR) && python3 plot-perfetto-cpu.py --csv-dir "$$analysis_dir" --output-dir ./perfetto-plots; \
	echo "Plots generated in: $(PERFETTO_ANALYSIS_DIR)/perfetto-plots/"

.PHONY: perfetto-clean
perfetto-clean: ## Remove all Perfetto trace data from experiment data
	@echo 'Removing Perfetto trace data from experiments...'
	@find "$(EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -delete 2>/dev/null || true
	@rm -rf "$(PERFETTO_ANALYSIS_DIR)/perfetto-analysis" "$(PERFETTO_ANALYSIS_DIR)/perfetto-plots"
	@echo '  Clean complete.'

.PHONY: perfetto
perfetto: help ## Alias for perfetto help

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
