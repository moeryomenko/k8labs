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
# eevdf-metrics-db.yaml is REFERENCE ONLY (see the config header comment): the
# combined api-server + db-simulator deployment requires a manual setup that
# run-experiment.sh's single-workload loop cannot express, so the file has no
# workload block and cannot dry-run. It is skipped below on purpose; every
# other config must dry-run cleanly.
validate-configs: ## Validate all experiment config files
	@echo "Validating experiment configs..."
	@for cfg in $(CONFIGS_DIR)/*.yaml; do \
		base=$$(basename "$$cfg"); \
		if [ "$$base" = "eevdf-metrics-db.yaml" ]; then \
			echo "  SKIP: $$base (reference-only, no workload block)"; \
			echo "  ---"; \
			continue; \
		fi; \
		echo "  Checking: $$base"; \
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
	echo "--- DHCP Leases ---"
	LEASE_FILE=/var/lib/misc/dnsmasq/k8sbr0.leases; \
	if [ -f "$$LEASE_FILE" ]; then \
		echo "  [OK] DHCP lease file: $$LEASE_FILE ($$(wc -l < $$LEASE_FILE) leases)"; \
		cat "$$LEASE_FILE" | awk '{printf "  %s → %s (%s)\n", $$2, $$3, $$4}'; \
	else \
		echo "  [WARN] DHCP lease file not found at $$LEASE_FILE (are VMs deployed?)"; \
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

.PHONY: experiment-weight-share
experiment-weight-share: ## Run weight-share experiment (3 stress-ng pods, request-only weight matrix, EEVDF metrics)
	@echo "Running weight-share experiment..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/weight-share.yaml --eevdf

.PHONY: experiment-request-limit-matrix
experiment-request-limit-matrix: ## Run request-limit-matrix experiment (15 request x limit cells, EEVDF metrics)
	@echo "Running request-limit-matrix experiment..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/request-limit-matrix.yaml --eevdf

.PHONY: experiment-qos-hierarchy
experiment-qos-hierarchy: ## Run qos-hierarchy experiment (3 QoS-class pods: guaranteed/burstable/besteffort, EEVDF metrics)
	@echo "Running qos-hierarchy experiment..."
	# Family C: QoS hierarchy competition. Requires --eevdf (EEVDF weight-share
	# metrics). Remember the manual cgroup-hierarchy-snapshot.sh capture per
	# cell documented in the config comment (analyzer contract).
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/qos-hierarchy.yaml --eevdf

.PHONY: experiment-latency-interference
experiment-latency-interference: ## Run latency-interference experiment (api-server LS + stress-ng batch, latency_load, no EEVDF by default)
	@echo "Running latency-interference experiment..."
	# Family D: latency interference. Default is lighter (no --eevdf). For
	# scheduler tracing, run instead:
	#   $(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/latency-interference.yaml --perfetto --perfetto-config eevdf-deep
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/latency-interference.yaml

.PHONY: experiment-cpu-burst
experiment-cpu-burst: ## Run cpu-burst experiment (db-simulator 250m limit, with/without cpu.max.burst, no EEVDF by default)
	@echo "Running cpu-burst experiment..."
	# Family E: CPU burst at a low limit. Default is lighter (no --eevdf). Burst
	# application is MANUAL via helper: after the pod starts, write
	# cpu.max.burst on the node via SSH (path pattern in the config comment:
	# <container-cgroup>/cpu.max.burst) and restore to 0 after each burst cell.
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/cpu-burst.yaml

.PHONY: experiment-tunables-contention
experiment-tunables-contention: ## Run tunables-contention experiment (api-server LS + stress-ng batch, scheduler tunable sweep, no EEVDF by default)
	@echo "Running tunables-contention experiment..."
	# Family F: scheduler tunable sweep under contention. Default is lighter
	# (no --eevdf). Apply tunable sets manually per cell:
	#   research/scripts/tunable-sweep.sh apply <tunables> && ... cell ... && research/scripts/tunable-sweep.sh restore
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/tunables-contention.yaml

# ---- EEVDF Execution-Distribution Experiments ----
#
# Families A/B/C run with full capture: --eevdf snapshots plus
# a Perfetto eevdf-deep trace on every cell (each target passes
# --eevdf --perfetto --perfetto-config eevdf-deep). The dist-*.yaml configs
# are the family deliverables; each target forwards its matching config to
# the shared runner. Analysis targets (dist-analyze/plots/gif/steps/report)
# are deterministic and run from staged data (no cluster, no network).
# NOTE: the dist-plots target invokes dist-plot.py (singular);
# the target name is intentionally plural.

.PHONY: experiment-dist-api
experiment-dist-api: ## Run dist-api-server experiment (Family A api-server, full EEVDF + Perfetto capture)
	@echo "Running dist-api-server experiment (Family A)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-api-server.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-dist-db
experiment-dist-db: ## Run dist-db-simulator experiment (Family A db-simulator, full EEVDF + Perfetto capture)
	@echo "Running dist-db-simulator experiment (Family A)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-db-simulator.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-dist-burner
experiment-dist-burner: ## Run dist-cpu-burner experiment (Family A cpu-burner, full EEVDF + Perfetto capture)
	@echo "Running dist-cpu-burner experiment (Family A)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-cpu-burner.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-dist-stress
experiment-dist-stress: ## Run dist-stress-ng experiment (Family A stress-ng saturating control, full EEVDF + Perfetto capture)
	@echo "Running dist-stress-ng experiment (Family A)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-stress-ng.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-dist-weight
experiment-dist-weight: ## Run dist-weight-share experiment (Family B weight-share, full EEVDF + Perfetto capture)
	@echo "Running dist-weight-share experiment (Family B)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-weight-share.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: experiment-dist-qos
experiment-dist-qos: ## Run dist-qos-hierarchy experiment (Family C QoS hierarchy, full EEVDF + Perfetto capture)
	@echo "Running dist-qos-hierarchy experiment (Family C)..."
	$(EXPERIMENTS_DIR)/run-experiment.sh $(CONFIGS_DIR)/dist-qos-hierarchy.yaml --eevdf --perfetto --perfetto-config eevdf-deep

# ---- EEVDF Execution-Distribution analysis (wired to canonical data) ----
#
# The analysis targets consume the CANONICAL run data under
# research/experiments/data/dist-*/<ts>/ (newest timestamp per family) and
# write under research/analysis/output/distribution/. They are
# deterministic and need no cluster/network.
#
# dist-analyze/dist-plots/dist-gif iterate the six dist families
# (one script invocation per family, matching the pinned per-family CLIs).
# dist-steps is a single run on the Family A stress-ng matrix: it pins
# exactly one global visuals/ set (step-3 = cells[0] no-limit cell, step-4 =
# cells[3] 500m/500m quota cell — Family-A indexes that weight-share/qos cell
# labels do not satisfy and qos's 3 cells would index out of range).
# dist-report is a single run covering every family under distribution/.

# The six dist families in spec order (Family A, Family B, Family C).
DIST_FAMILIES := dist-api-server dist-db-simulator dist-cpu-burner dist-stress-ng dist-weight-share dist-qos-hierarchy

# Output root for all dist analysis (outputs under .../output/distribution/).
DIST_ANALYSIS_OUTPUT_DIR := $(PERFETTO_ANALYSIS_DIR)/output

# --workload per family for dist-analyze (sanity gate). Only the four pinned
# choices are valid; weight-share/qos have no throttle facts and pass no flag.
dist_wl = $(if $(findstring $(1),dist-stress-ng),--workload stress-ng,$(if $(findstring $(1),dist-api-server),--workload api-server,$(if $(findstring $(1),dist-db-simulator),--workload db-simulator,$(if $(findstring $(1),dist-cpu-burner),--workload cpu-burner,))))

# Comma-separated cell labels for a family config, in config (pinned) order —
# same label transformation as run-experiment.sh::clean_cell_label.
dist_cells = $(shell awk '/^matrix:/{f=1;next} f && /^[[:space:]]*- /{ sub(/^[[:space:]]*- /,""); gsub(/"/,""); gsub(/[[:space:]]*:[[:space:]]*/,"="); gsub(/;/,"-"); gsub(/[[:space:]]/,""); printf "%s%s", sep, $$0; sep="," } END{ print "" }' $(CONFIGS_DIR)/$(1).yaml)

# NOTE: the per-family recipe bodies below are single-line (';'-terminated) on
# purpose — with .ONESHELL a multi-line $(foreach)/$(call) expansion is joined
# with tabs, collapsing the commands into one. Semicolons keep every family a
# separate statement in the same shell.

define dist_analyze_cmd
latest="$$(ls -1dt $(EXPERIMENTS_DIR)/data/$(1)/*/ 2>/dev/null | grep -v '/_archive/' | head -1 | sed 's:/$$::')"; if [ -n "$$latest" ]; then python3 $(PERFETTO_ANALYSIS_DIR)/dist-analyze.py --data-dir "$$latest" --output-dir $(DIST_ANALYSIS_OUTPUT_DIR) --family $(1) $(call dist_wl,$(1)) --duration 90 --chunk-s 5; else echo "  WARN: no run data for $(1)"; fi;
endef

define dist_plot_cmd
cells="$(call dist_cells,$(1))"; python3 $(PERFETTO_ANALYSIS_DIR)/dist-plot.py --data-dir $(DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(DIST_ANALYSIS_OUTPUT_DIR) --family $(1) --cells "$$cells";
endef

define dist_gif_cmd
python3 $(PERFETTO_ANALYSIS_DIR)/dist-gif.py --data-dir $(DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(DIST_ANALYSIS_OUTPUT_DIR) --family $(1);
endef

.PHONY: dist-analyze
dist-analyze: ## Run dist-analyze.py slice-level extraction pipeline
	@echo "Running dist-analyze slice-level extraction..."
	$(foreach fam,$(DIST_FAMILIES),$(call dist_analyze_cmd,$(fam)))

.PHONY: dist-plots
dist-plots: ## Generate static distribution images via dist-plot.py
	@echo "Generating static distribution plots..."
	$(foreach fam,$(DIST_FAMILIES),$(call dist_plot_cmd,$(fam)))

.PHONY: dist-gif
dist-gif: ## Generate animated distribution GIFs via dist-gif.py
	@echo "Generating animated distribution GIFs..."
	$(foreach fam,$(DIST_FAMILIES),$(call dist_gif_cmd,$(fam)))

.PHONY: dist-steps
dist-steps: ## Generate six step-by-step distribution images via dist-steps.py
	@echo "Generating step-by-step distribution images..."
	@cells="$(call dist_cells,dist-stress-ng)"
	python3 $(PERFETTO_ANALYSIS_DIR)/dist-steps.py --data-dir $(DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(DIST_ANALYSIS_OUTPUT_DIR) --family dist-stress-ng --cells "$$cells"

.PHONY: dist-report
dist-report: ## Regenerate DEEP-DIVE-EEVDF-EXEC.md via dist-report.py
	@echo "Regenerating deep-dive report..."
	python3 $(PERFETTO_ANALYSIS_DIR)/dist-report.py --data-dir $(DIST_ANALYSIS_OUTPUT_DIR) --output-file $(PROJECT_ROOT)/research/DEEP-DIVE-EEVDF-EXEC.md

.PHONY: dist-all
dist-all: experiment-dist-api experiment-dist-db experiment-dist-burner experiment-dist-stress experiment-dist-weight experiment-dist-qos ## Run all six dist experiment families sequentially
	@echo "=== All dist experiment families complete ==="

# ---- Interaction Report ----

# Regenerates interaction-report.md from staged CSVs under experiments/data/
# (one dir per family). No cluster, no network, no timestamps: rerunning with
# the same staged data produces byte-identical output.
INTERACTION_REPORT_OUTPUT_DIR := $(PERFETTO_ANALYSIS_DIR)/output/interaction-report

.PHONY: interaction-report
interaction-report: ## Regenerate interaction-report.md from staged data (5 analyzers + report generator; no cluster, no network)
	@echo "Regenerating interaction-report from staged experiment data..."
	@mkdir -p $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/weight-share-analyze.py --data-dir $(EXPERIMENTS_DIR)/data/weight-share --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/interaction-heatmap.py --data-dir $(EXPERIMENTS_DIR)/data/request-limit-matrix --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/qos-analyze.py --data-dir $(EXPERIMENTS_DIR)/data/qos-hierarchy --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/latency-analyze.py --data-dir $(EXPERIMENTS_DIR)/data/latency-interference --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/tunables-analyze.py --data-dir $(EXPERIMENTS_DIR)/data/tunables-contention --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(PERFETTO_ANALYSIS_DIR)/generate-report.py --input-dir $(INTERACTION_REPORT_OUTPUT_DIR) --output-dir $(INTERACTION_REPORT_OUTPUT_DIR)
	@echo "interaction-report.md written to $(INTERACTION_REPORT_OUTPUT_DIR)"

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
		echo "  Perfetto traces found. Run 'make perfetto-view' to open in the Perfetto UI, or 'make perfetto-analyze' and 'make perfetto-plots' for CSV/PNG analysis."; \
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
	shopt -s nullglob; \
	subdirs=("$$analysis_dir"/*/); \
	shopt -u nullglob; \
	if [ $${#subdirs[@]} -eq 0 ]; then \
		echo "  No analysis data found. Run 'make perfetto-analyze' first."; \
		exit 0; \
	fi; \
	cd $(PERFETTO_ANALYSIS_DIR) || exit 1; \
	for subdir in "$${subdirs[@]}"; do \
		trace_name=$$(basename "$$subdir"); \
		echo "  Plotting trace: $$trace_name"; \
		python3 plot-perfetto-cpu.py "$$subdir" --output-dir "./perfetto-plots/$$trace_name"; \
	done; \
	echo "Plots generated in: $(PERFETTO_ANALYSIS_DIR)/perfetto-plots/"

.PHONY: perfetto-view
perfetto-view: ## Serve the newest Perfetto trace with the CORS static server and open the Perfetto UI
	@trace="$$(find "$(EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2- || true)"; \
	if [[ -z "$${trace}" ]]; then \
		echo "  No Perfetto traces found. Run an experiment with --perfetto first."; \
		exit 0; \
	fi; \
	trace_dir="$$(dirname -- "$${trace}")"; \
	trace_name="$$(basename -- "$${trace}")"; \
	serve="$(PERFETTO_BIN_DIR)/perfetto-serve.py"; \
	if [[ ! -f "$${serve}" ]]; then \
		echo "  perfetto-serve.py not found: $${serve}" >&2; \
		exit 1; \
	fi; \
	port="$${PERFETTO_VIEW_PORT:-9001}"; \
	echo "  Serving newest trace: $${trace}"; \
	python3 "$${serve}" --port "$${port}" --dir "$${trace_dir}" & \
	serve_pid=$$!; \
	trap 'kill "$${serve_pid}" 2>/dev/null || true' EXIT INT TERM; \
	if command -v curl &>/dev/null; then \
		deadline=$$((SECONDS + 10)); \
		until curl -fsSI -o /dev/null "http://127.0.0.1:$${port}/$${trace_name}" 2>/dev/null; do \
			if (( SECONDS > deadline )); then \
				echo "  perfetto-serve did not accept connections on port $${port} within 10s" >&2; \
				exit 1; \
			fi; \
			sleep 0.2; \
		done; \
	else \
		sleep 1; \
	fi; \
	url="https://ui.perfetto.dev/#!/?url=http://127.0.0.1:$${port}/$${trace_name}"; \
	echo "  $${url}"; \
	if command -v xdg-open &>/dev/null; then \
		xdg-open "$${url}" 2>/dev/null & \
	fi; \
	echo "  Press Ctrl-C to stop the trace server."; \
	wait "$${serve_pid}"

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
