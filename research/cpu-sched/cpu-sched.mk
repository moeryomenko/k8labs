# k8labs CPU Scheduling Research — cpu-sched series Makefile
# Targets for running cpu-sched experiments and generating reports
#
# Usage:
#   make cpu-sched-setup                      — verify prerequisites
#   make cpu-sched-experiment-baseline        — run throttling-baseline experiment
#   make cpu-sched-experiment-limits          — run throttling-limits experiment
#   make cpu-sched-experiment-request-limit   — run throttling-request-limit experiment
#   make cpu-sched-experiment-colocated       — run co-located experiment
#   make cpu-sched-all-experiments            — run all experiments sequentially
#   make cpu-sched-clean                      — remove all experiment data
#   make cpu-sched-report                     — analyze results (placeholder)

SHELL := /bin/bash
.ONESHELL:

# Project root (auto-detected)
PROJECT_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)

# Series root: the directory holding this makefile (trailing slash). Every
# series path is derived from it so the makefile works when included by the
# research/ aggregator or invoked directly.
CPU_SCHED_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
CPU_SCHED_EXPERIMENTS_DIR := $(CPU_SCHED_DIR)experiments
CPU_SCHED_CONFIGS_DIR := $(CPU_SCHED_EXPERIMENTS_DIR)/configs
CPU_SCHED_RUNNER := $(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh
CPU_SCHED_PERFETTO_BIN_DIR := $(CPU_SCHED_DIR)perfetto/bin
CPU_SCHED_PERFETTO_CONFIGS_DIR := $(CPU_SCHED_DIR)perfetto/configs
CPU_SCHED_ANALYSIS_DIR := $(CPU_SCHED_DIR)analysis
CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR := $(CPU_SCHED_ANALYSIS_DIR)/output
CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR := $(CPU_SCHED_ANALYSIS_DIR)/output/interaction-report

# Resolve KUBECONFIG path
KUBECONFIG := $(PROJECT_ROOT)/kubeconfig
export KUBECONFIG

# ---- Config Validation ----

.PHONY: cpu-sched-validate-configs
# eevdf-metrics-db.yaml is REFERENCE ONLY (see the config header comment): the
# combined api-server + db-simulator deployment requires a manual setup that
# run-experiment.sh's single-workload loop cannot express, so the file has no
# workload block and cannot dry-run. It is skipped below on purpose; every
# other config must dry-run cleanly.
cpu-sched-validate-configs: ## Validate all experiment config files
	@echo "Validating experiment configs..."
	@for cfg in $(CPU_SCHED_CONFIGS_DIR)/*.yaml; do \
		base=$$(basename "$$cfg"); \
		if [ "$$base" = "eevdf-metrics-db.yaml" ]; then \
			echo "  SKIP: $$base (reference-only, no workload block)"; \
			echo "  ---"; \
			continue; \
		fi; \
		echo "  Checking: $$base"; \
		$(CPU_SCHED_RUNNER) "$$cfg" --dry-run 2>&1 | head -20; \
		echo "  ---"; \
	done
	@echo "All configs validated."

# ---- Setup ----

.PHONY: cpu-sched-setup
cpu-sched-setup: ## Verify prerequisites (kubectl access, tools, tofu state)
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

.PHONY: cpu-sched-experiment-baseline
cpu-sched-experiment-baseline: ## Run throttling-baseline experiment
	@echo "Running throttling-baseline experiment..."
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-baseline.yaml

.PHONY: cpu-sched-experiment-limits
cpu-sched-experiment-limits: ## Run throttling-limits experiment
	@echo "Running throttling-limits experiment..."
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-limits.yaml

.PHONY: cpu-sched-experiment-request-limit
cpu-sched-experiment-request-limit: ## Run throttling-request-limit experiment
	@echo "Running throttling-request-limit experiment..."
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-request-limit.yaml

.PHONY: cpu-sched-experiment-colocated
cpu-sched-experiment-colocated: ## Run co-located experiment
	@echo "Running co-located experiment..."
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/co-located.yaml

# ---- EEVDF Experiments ----

.PHONY: cpu-sched-experiment-eevdf-metrics
cpu-sched-experiment-eevdf-metrics: ## Run EEVDF metrics experiment (cpu-burner, 9 runs)
	@echo "Running EEVDF metrics experiment..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/eevdf-metrics.yaml

.PHONY: cpu-sched-experiment-eevdf-stress
cpu-sched-experiment-eevdf-stress: ## Run EEVDF metrics experiment with stress-ng (12 runs, ~30min)
	@echo "Running EEVDF stress-ng metrics experiment..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/eevdf-metrics-stress.yaml

.PHONY: cpu-sched-experiment-eevdf-stress-perfetto
cpu-sched-experiment-eevdf-stress-perfetto: ## Run EEVDF stress-ng with perfetto tracing
	@echo "Running EEVDF stress-ng with perfetto..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/eevdf-metrics-stress.yaml --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-eevdf-api-baseline
cpu-sched-experiment-eevdf-api-baseline: ## Run EEVDF API server baseline experiment
	@echo "Running EEVDF API server baseline..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/eevdf-api-baseline.yaml

.PHONY: cpu-sched-experiment-eevdf-db-baseline
cpu-sched-experiment-eevdf-db-baseline: ## Run EEVDF database baseline experiment
	@echo "Running EEVDF database baseline..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/eevdf-db-baseline.yaml

.PHONY: cpu-sched-experiment-weight-share
cpu-sched-experiment-weight-share: ## Run weight-share experiment (3 stress-ng pods, request-only weight matrix, EEVDF metrics)
	@echo "Running weight-share experiment..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/weight-share.yaml --eevdf

.PHONY: cpu-sched-experiment-request-limit-matrix
cpu-sched-experiment-request-limit-matrix: ## Run request-limit-matrix experiment (15 request x limit cells, EEVDF metrics)
	@echo "Running request-limit-matrix experiment..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/request-limit-matrix.yaml --eevdf

.PHONY: cpu-sched-experiment-qos-hierarchy
cpu-sched-experiment-qos-hierarchy: ## Run qos-hierarchy experiment (3 QoS-class pods: guaranteed/burstable/besteffort, EEVDF metrics)
	@echo "Running qos-hierarchy experiment..."
	# Family C: QoS hierarchy competition. Requires --eevdf (EEVDF weight-share
	# metrics). Remember the manual cgroup-hierarchy-snapshot.sh capture per
	# cell documented in the config comment (analyzer contract).
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/qos-hierarchy.yaml --eevdf

.PHONY: cpu-sched-experiment-latency-interference
cpu-sched-experiment-latency-interference: ## Run latency-interference experiment (api-server LS + stress-ng batch, latency_load, no EEVDF by default)
	@echo "Running latency-interference experiment..."
	# Family D: latency interference. Default is lighter (no --eevdf). For
	# scheduler tracing, run instead:
	#   $(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/latency-interference.yaml --perfetto --perfetto-config eevdf-deep
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/latency-interference.yaml

.PHONY: cpu-sched-experiment-cpu-burst
cpu-sched-experiment-cpu-burst: ## Run cpu-burst experiment (db-simulator 250m limit, with/without cpu.max.burst, no EEVDF by default)
	@echo "Running cpu-burst experiment..."
	# Family E: CPU burst at a low limit. Default is lighter (no --eevdf). Burst
	# application is MANUAL via helper: after the pod starts, write
	# cpu.max.burst on the node via SSH (path pattern in the config comment:
	# <container-cgroup>/cpu.max.burst) and restore to 0 after each burst cell.
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/cpu-burst.yaml

.PHONY: cpu-sched-experiment-tunables-contention
cpu-sched-experiment-tunables-contention: ## Run tunables-contention experiment (api-server LS + stress-ng batch, scheduler tunable sweep, no EEVDF by default)
	@echo "Running tunables-contention experiment..."
	# Family F: scheduler tunable sweep under contention. Default is lighter
	# (no --eevdf). Apply tunable sets manually per cell:
	#   research/cpu-sched/scripts/tunable-sweep.sh apply <tunables> && ... cell ... && research/cpu-sched/scripts/tunable-sweep.sh restore
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/tunables-contention.yaml

# ---- EEVDF Execution-Distribution Experiments ----
#
# Families A/B/C run with full capture: --eevdf snapshots plus
# a Perfetto eevdf-deep trace on every cell (each target passes
# --eevdf --perfetto --perfetto-config eevdf-deep). The dist-*.yaml configs
# are the family deliverables; each target forwards its matching config to
# the shared runner. Analysis targets (cpu-sched-dist-analyze,
# cpu-sched-dist-plots, cpu-sched-dist-gif, cpu-sched-dist-steps,
# cpu-sched-dist-report) are deterministic and run from staged data
# (no cluster, no network).
# NOTE: the cpu-sched-dist-plots target invokes dist-plot.py (singular);
# the target name is intentionally plural.

.PHONY: cpu-sched-experiment-dist-api
cpu-sched-experiment-dist-api: ## Run dist-api-server experiment (Family A api-server, full EEVDF + Perfetto capture)
	@echo "Running dist-api-server experiment (Family A)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-api-server.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-dist-db
cpu-sched-experiment-dist-db: ## Run dist-db-simulator experiment (Family A db-simulator, full EEVDF + Perfetto capture)
	@echo "Running dist-db-simulator experiment (Family A)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-db-simulator.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-dist-burner
cpu-sched-experiment-dist-burner: ## Run dist-cpu-burner experiment (Family A cpu-burner, full EEVDF + Perfetto capture)
	@echo "Running dist-cpu-burner experiment (Family A)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-cpu-burner.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-dist-stress
cpu-sched-experiment-dist-stress: ## Run dist-stress-ng experiment (Family A stress-ng saturating control, full EEVDF + Perfetto capture)
	@echo "Running dist-stress-ng experiment (Family A)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-stress-ng.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-dist-weight
cpu-sched-experiment-dist-weight: ## Run dist-weight-share experiment (Family B weight-share, full EEVDF + Perfetto capture)
	@echo "Running dist-weight-share experiment (Family B)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-weight-share.yaml --eevdf --perfetto --perfetto-config eevdf-deep

.PHONY: cpu-sched-experiment-dist-qos
cpu-sched-experiment-dist-qos: ## Run dist-qos-hierarchy experiment (Family C QoS hierarchy, full EEVDF + Perfetto capture)
	@echo "Running dist-qos-hierarchy experiment (Family C)..."
	$(CPU_SCHED_EXPERIMENTS_DIR)/run-experiment.sh $(CPU_SCHED_CONFIGS_DIR)/dist-qos-hierarchy.yaml --eevdf --perfetto --perfetto-config eevdf-deep

# ---- EEVDF Execution-Distribution analysis (wired to canonical data) ----
#
# The analysis targets consume the CANONICAL run data under
# research/cpu-sched/experiments/data/dist-*/<ts>/ (newest timestamp per family) and
# write under research/cpu-sched/analysis/output/distribution/. They are
# deterministic and need no cluster/network.
#
# cpu-sched-dist-analyze/cpu-sched-dist-plots/cpu-sched-dist-gif iterate the six dist families
# (one script invocation per family, matching the pinned per-family CLIs).
# cpu-sched-dist-steps is a single run on the Family A stress-ng matrix: it pins
# exactly one global visuals/ set (step-3 = cells[0] no-limit cell, step-4 =
# cells[3] 500m/500m quota cell — Family-A indexes that weight-share/qos cell
# labels do not satisfy and qos's 3 cells would index out of range).
# cpu-sched-dist-report is a single run covering every family under distribution/.

# The six dist families in spec order (Family A, Family B, Family C).
CPU_SCHED_DIST_FAMILIES := dist-api-server dist-db-simulator dist-cpu-burner dist-stress-ng dist-weight-share dist-qos-hierarchy

# Output root for all dist analysis (outputs under .../output/distribution/).

# --workload per family for dist-analyze (sanity gate). Only the four pinned
# choices are valid; weight-share/qos have no throttle facts and pass no flag.
cpu_sched_dist_wl = $(if $(findstring $(1),dist-stress-ng),--workload stress-ng,$(if $(findstring $(1),dist-api-server),--workload api-server,$(if $(findstring $(1),dist-db-simulator),--workload db-simulator,$(if $(findstring $(1),dist-cpu-burner),--workload cpu-burner,))))

# Comma-separated cell labels for a family config, in config (pinned) order —
# same label transformation as run-experiment.sh::clean_cell_label.
cpu_sched_dist_cells = $(shell awk '/^matrix:/{f=1;next} f && /^[[:space:]]*- /{ sub(/^[[:space:]]*- /,""); gsub(/"/,""); gsub(/[[:space:]]*:[[:space:]]*/,"="); gsub(/;/,"-"); gsub(/[[:space:]]/,""); printf "%s%s", sep, $$0; sep="," } END{ print "" }' $(CPU_SCHED_CONFIGS_DIR)/$(1).yaml)

# NOTE: the per-family recipe bodies below are single-line (';'-terminated) on
# purpose — with .ONESHELL a multi-line $(foreach)/$(call) expansion is joined
# with tabs, collapsing the commands into one. Semicolons keep every family a
# separate statement in the same shell.

define cpu_sched_dist_analyze_cmd
latest="$$(ls -1dt $(CPU_SCHED_EXPERIMENTS_DIR)/data/$(1)/*/ 2>/dev/null | grep -v '/_archive/' | head -1 | sed 's:/$$::')"; if [ -n "$$latest" ]; then python3 $(CPU_SCHED_ANALYSIS_DIR)/dist-analyze.py --data-dir "$$latest" --output-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --family $(1) $(call cpu_sched_dist_wl,$(1)) --duration 90 --chunk-s 5; else echo "  WARN: no run data for $(1)"; fi;
endef

define cpu_sched_dist_plot_cmd
cells="$(call cpu_sched_dist_cells,$(1))"; python3 $(CPU_SCHED_ANALYSIS_DIR)/dist-plot.py --data-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --family $(1) --cells "$$cells";
endef

define cpu_sched_dist_gif_cmd
python3 $(CPU_SCHED_ANALYSIS_DIR)/dist-gif.py --data-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --family $(1);
endef

.PHONY: cpu-sched-dist-analyze
cpu-sched-dist-analyze: ## Run dist-analyze.py slice-level extraction pipeline
	@echo "Running dist-analyze slice-level extraction..."
	$(foreach fam,$(CPU_SCHED_DIST_FAMILIES),$(call cpu_sched_dist_analyze_cmd,$(fam)))

.PHONY: cpu-sched-dist-plots
cpu-sched-dist-plots: ## Generate static distribution images via dist-plot.py
	@echo "Generating static distribution plots..."
	$(foreach fam,$(CPU_SCHED_DIST_FAMILIES),$(call cpu_sched_dist_plot_cmd,$(fam)))

.PHONY: cpu-sched-dist-gif
cpu-sched-dist-gif: ## Generate animated distribution GIFs via dist-gif.py
	@echo "Generating animated distribution GIFs..."
	$(foreach fam,$(CPU_SCHED_DIST_FAMILIES),$(call cpu_sched_dist_gif_cmd,$(fam)))

.PHONY: cpu-sched-dist-steps
cpu-sched-dist-steps: ## Generate six step-by-step distribution images via dist-steps.py
	@echo "Generating step-by-step distribution images..."
	@cells="$(call cpu_sched_dist_cells,dist-stress-ng)"
	python3 $(CPU_SCHED_ANALYSIS_DIR)/dist-steps.py --data-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --output-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --family dist-stress-ng --cells "$$cells"

.PHONY: cpu-sched-dist-report
cpu-sched-dist-report: ## Regenerate DEEP-DIVE-EEVDF-EXEC.md via dist-report.py
	@echo "Regenerating deep-dive report..."
	python3 $(CPU_SCHED_ANALYSIS_DIR)/dist-report.py --data-dir $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR) --output-file $(CPU_SCHED_DIST_ANALYSIS_OUTPUT_DIR)/DEEP-DIVE-EEVDF-EXEC.md

.PHONY: cpu-sched-dist-all
cpu-sched-dist-all: cpu-sched-experiment-dist-api cpu-sched-experiment-dist-db cpu-sched-experiment-dist-burner cpu-sched-experiment-dist-stress cpu-sched-experiment-dist-weight cpu-sched-experiment-dist-qos ## Run all six dist experiment families sequentially
	@echo "=== All dist experiment families complete ==="

# ---- Interaction Report ----

# Regenerates interaction-report.md from staged CSVs under cpu-sched/experiments/data/
# (one dir per family). No cluster, no network, no timestamps: rerunning with
# the same staged data produces byte-identical output.

.PHONY: cpu-sched-interaction-report
cpu-sched-interaction-report: ## Regenerate interaction-report.md from staged data (5 analyzers + report generator; no cluster, no network)
	@echo "Regenerating interaction-report from staged experiment data..."
	@mkdir -p $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/weight-share-analyze.py --data-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/weight-share --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/interaction-heatmap.py --data-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/request-limit-matrix --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/qos-analyze.py --data-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/qos-hierarchy --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/latency-analyze.py --data-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/latency-interference --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/tunables-analyze.py --data-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/tunables-contention --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	python3 $(CPU_SCHED_ANALYSIS_DIR)/generate-report.py --input-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR) --output-dir $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)
	@echo "interaction-report.md written to $(CPU_SCHED_INTERACTION_REPORT_OUTPUT_DIR)"

.PHONY: cpu-sched-eevdf-analyze
cpu-sched-eevdf-analyze: ## Run EEVDF analysis pipeline on experiment data
	@echo "Running EEVDF analysis..."
	@if ls $(CPU_SCHED_EXPERIMENTS_DIR)/data/eevdf-metrics-stress/*/summary.csv 2>/dev/null | head -1 >/dev/null; then \
		latest=$$(ls -1t $(CPU_SCHED_EXPERIMENTS_DIR)/data/eevdf-metrics-stress/ | head -1); \
		cd $(CPU_SCHED_ANALYSIS_DIR) && python3 eevdf-analyze.py --csv-dir $(CPU_SCHED_EXPERIMENTS_DIR)/data/eevdf-metrics-stress/$$latest --output-dir ./output/eevdf-metrics-stress; \
	else \
		echo "No experiment data found. Run cpu-sched-experiment-eevdf-stress first."; \
	fi

.PHONY: cpu-sched-eevdf-plots
cpu-sched-eevdf-plots: ## Generate EEVDF plots from analysis
	@echo "Generating EEVDF plots..."
	@if [ -d $(CPU_SCHED_ANALYSIS_DIR)/output/eevdf-metrics-stress ]; then \
		cd $(CPU_SCHED_ANALYSIS_DIR) && python3 eevdf-plot.py --csv-dir ./output/eevdf-metrics-stress --output-dir ./output/eevdf-metrics-stress/plots; \
	else \
		echo "No analysis data. Run cpu-sched-eevdf-analyze first."; \
	fi

.PHONY: cpu-sched-eevdf-report
cpu-sched-eevdf-report: ## Generate EEVDF deep-dive report
	@echo "EEVDF deep-dive report: research/cpu-sched/analysis/output/DEEP-DIVE-EEVDF-EXEC.md"
	@echo "To regenerate, re-run experiments and analysis pipeline."

# ---- All Experiments ----

.PHONY: cpu-sched-all-experiments
cpu-sched-all-experiments: ## Run all experiments sequentially
	@echo "=== Running all experiments ==="
	@echo ""
	@echo "=== 1/4: Baseline ==="
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-baseline.yaml
	@echo ""
	@echo "=== 2/4: Limits ==="
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-limits.yaml
	@echo ""
	@echo "=== 3/4: Request-Limit ==="
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/throttling-request-limit.yaml
	@echo ""
	@echo "=== 4/4: Co-located ==="
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/co-located.yaml
	@echo ""
	@echo "=== 5/5: EEVDF metrics ==="
	$(CPU_SCHED_RUNNER) $(CPU_SCHED_CONFIGS_DIR)/eevdf-metrics.yaml
	@echo ""
	@echo "=== All experiments complete ==="

# ---- Report ----

.PHONY: cpu-sched-report
cpu-sched-report: ## Generate analysis report from experiment data
	@echo 'Generating analysis report...'
	@if [ ! -d $(CPU_SCHED_EXPERIMENTS_DIR)/data ] || [ -z "$$(find $(CPU_SCHED_EXPERIMENTS_DIR)/data -name 'summary.csv' 2>/dev/null)" ]; then \
		echo "  No experiment data found. Run experiments first with 'make cpu-sched-experiment-*'"; \
		exit 0; \
	fi
	$(CPU_SCHED_ANALYSIS_DIR)/analyze-throttling.sh $(CPU_SCHED_EXPERIMENTS_DIR)/data --output-dir $(CPU_SCHED_ANALYSIS_DIR)/output
	python3 $(CPU_SCHED_ANALYSIS_DIR)/plot-throttling.py $(CPU_SCHED_ANALYSIS_DIR)/output/aggregates.csv --output-dir $(CPU_SCHED_ANALYSIS_DIR)/output
	@echo '  Report generated in $(CPU_SCHED_ANALYSIS_DIR)/output/'
	@echo '  Files: aggregates.csv, summary.json, *.png'
	@echo ''
	@echo '--- Perfetto analysis ---'
	@if find "$(CPU_SCHED_EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -type f 2>/dev/null | head -1 | grep -q .; then \
		echo "  Perfetto traces found. Run 'make cpu-sched-perfetto-view' to open in the Perfetto UI, or 'make cpu-sched-perfetto-analyze' and 'make cpu-sched-perfetto-plots' for CSV/PNG analysis."; \
	else \
		echo "  No Perfetto traces found."; \
	fi

.PHONY: cpu-sched-report-view
cpu-sched-report-view: cpu-sched-report ## Generate report and open plots (if display available)
	@if command -v xdg-open &>/dev/null; then \
		for f in $(CPU_SCHED_ANALYSIS_DIR)/output/*.png; do \
			xdg-open "$$f" 2>/dev/null || true; \
		done; \
	elif command -v open &>/dev/null; then \
		for f in $(CPU_SCHED_ANALYSIS_DIR)/output/*.png; do \
			open "$$f" 2>/dev/null || true; \
		done; \
	else \
		echo "  No image viewer found. Open $(CPU_SCHED_ANALYSIS_DIR)/output/*.png manually."; \
	fi

# ---- Perfetto Tracing ----

.PHONY: cpu-sched-perfetto-sysext
cpu-sched-perfetto-sysext: ## Build and deploy Perfetto sysext (requires cluster)
	@echo 'Building Perfetto sysext...'
	$(MAKE) -C $(PROJECT_ROOT) sysext/perfetto
	@echo ''
	@echo 'To deploy: copy extensions/release/perfetto.raw to each node and run:'
	@echo '  systemd-sysext merge'
	@echo '  systemd-sysext list'

.PHONY: cpu-sched-perfetto-capture
cpu-sched-perfetto-capture: ## Capture a Perfetto trace from a node (interactive)
	@echo '=== Perfetto Trace Capture ==='
	@echo ''
	@echo 'Available configs:'
	@ls -1 $(CPU_SCHED_PERFETTO_CONFIGS_DIR)/*.cfg 2>/dev/null | sed 's|.*/||;s|\.cfg$$||' | while read c; do echo "  $$c"; done
	@echo ''
	@read -p 'Node IP: ' node_ip; \
	read -p 'Config name (default: scheduling): ' config_name; \
	config_name=$${config_name:-scheduling}; \
	read -p 'Duration (seconds, default: 30): ' duration; \
	duration=$${duration:-30}; \
	output_dir=$${PWD}/perfetto-traces; \
	echo "  Capturing trace from $$node_ip (config: $$config_name, duration: $$duration)..."; \
	$(CPU_SCHED_PERFETTO_BIN_DIR)/perfetto-capture.sh $$node_ip $$config_name --duration $$duration --output-dir $$output_dir; \
	echo "  Trace saved."

.PHONY: cpu-sched-perfetto-analyze
cpu-sched-perfetto-analyze: ## Analyze Perfetto traces in experiments/data or specified directory
	@echo 'Analyzing Perfetto traces...'
	@trace_dir=""; \
	if [ -d "$(CPU_SCHED_EXPERIMENTS_DIR)/data" ]; then \
		trace_dir="$(CPU_SCHED_EXPERIMENTS_DIR)/data"; \
		echo "  Searching for traces in: $$trace_dir"; \
	else \
		echo "  No experiment data found. Run experiments first."; \
		exit 0; \
	fi; \
	find "$$trace_dir" -name '*.perfetto-trace' -type f 2>/dev/null | head -5; \
	echo "Found $$(find "$$trace_dir" -name '*.perfetto-trace' -type f 2>/dev/null | wc -l) trace files"; \
	cd $(CPU_SCHED_ANALYSIS_DIR) && python3 perfetto-analyze.py --trace-dir "$$trace_dir" --output-dir ./perfetto-analysis; \
	echo "Analysis complete in: $(CPU_SCHED_ANALYSIS_DIR)/perfetto-analysis/"

.PHONY: cpu-sched-perfetto-plots
cpu-sched-perfetto-plots: ## Generate CPU execution time plots from Perfetto analysis
	@echo 'Generating Perfetto CPU execution time plots...'
	@if [ ! -f "$(CPU_SCHED_ANALYSIS_DIR)/plot-perfetto-cpu.py" ]; then \
		echo "  plot-perfetto-cpu.py not yet created"; \
		exit 0; \
	fi; \
	analysis_dir=$(CPU_SCHED_ANALYSIS_DIR)/perfetto-analysis; \
	if [ ! -d "$$analysis_dir" ]; then \
		echo "  No analysis data found. Run 'make cpu-sched-perfetto-analyze' first."; \
		exit 0; \
	fi; \
	shopt -s nullglob; \
	subdirs=("$$analysis_dir"/*/); \
	shopt -u nullglob; \
	if [ $${#subdirs[@]} -eq 0 ]; then \
		echo "  No analysis data found. Run 'make cpu-sched-perfetto-analyze' first."; \
		exit 0; \
	fi; \
	cd $(CPU_SCHED_ANALYSIS_DIR) || exit 1; \
	for subdir in "$${subdirs[@]}"; do \
		trace_name=$$(basename "$$subdir"); \
		echo "  Plotting trace: $$trace_name"; \
		python3 plot-perfetto-cpu.py "$$subdir" --output-dir "./perfetto-plots/$$trace_name"; \
	done; \
	echo "Plots generated in: $(CPU_SCHED_ANALYSIS_DIR)/perfetto-plots/"

.PHONY: cpu-sched-perfetto-view
cpu-sched-perfetto-view: ## Serve the newest Perfetto trace with the CORS static server and open the Perfetto UI
	@trace="$$(find "$(CPU_SCHED_EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2- || true)"; \
	if [[ -z "$${trace}" ]]; then \
		echo "  No Perfetto traces found. Run an experiment with --perfetto first."; \
		exit 0; \
	fi; \
	trace_dir="$$(dirname -- "$${trace}")"; \
	trace_name="$$(basename -- "$${trace}")"; \
	serve="$(CPU_SCHED_PERFETTO_BIN_DIR)/perfetto-serve.py"; \
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

.PHONY: cpu-sched-perfetto-clean
cpu-sched-perfetto-clean: ## Remove all Perfetto trace data from experiment data
	@echo 'Removing Perfetto trace data from experiments...'
	@find "$(CPU_SCHED_EXPERIMENTS_DIR)/data" -name '*.perfetto-trace' -delete 2>/dev/null || true
	@rm -rf "$(CPU_SCHED_ANALYSIS_DIR)/perfetto-analysis" "$(CPU_SCHED_ANALYSIS_DIR)/perfetto-plots"
	@echo '  Clean complete.'

.PHONY: cpu-sched-perfetto
cpu-sched-perfetto: help ## Alias for perfetto help

# ---- Test Suite ----

.PHONY: cpu-sched-test
cpu-sched-test: ## Run the cpu-sched test suite (bats experiments, bats perfetto, pytest analysis)
	@set -e
	@echo "Running cpu-sched test suite..."
	bats $(CPU_SCHED_EXPERIMENTS_DIR)/tests/*.bats
	bats $(CPU_SCHED_DIR)perfetto/tests/*.bats
	python3 -m pytest $(CPU_SCHED_ANALYSIS_DIR)/tests/

# ---- Clean ----

.PHONY: cpu-sched-clean
cpu-sched-clean: ## Remove all experiment data (prompts for confirmation)
	@echo "WARNING: This will remove all experiment data directories and summary CSVs."
	@read -t 30 -r -p "Are you sure? [y/N] " confirm; \
	case "$$confirm" in \
		[yY]|[yY][eE][sS]) ;; \
		*) echo "  Aborted."; exit 1 ;; \
	esac
	@echo "Removing experiment data..."
	@if [ -d "$(CPU_SCHED_EXPERIMENTS_DIR)/data" ]; then \
		rm -rf $(CPU_SCHED_EXPERIMENTS_DIR)/data; \
		echo "  Removed: $(CPU_SCHED_EXPERIMENTS_DIR)/data"; \
	else \
		echo "  No data directory found."; \
	fi
	@echo "Clean complete."
