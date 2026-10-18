# k8labs Linux CPU-scheduler Research — linux-sched series Makefile
#
# Compares the default EEVDF scheduler against sched_ext implementations
# (scx_bpfland, scx_lavd) on a pinned worker VM using a
# randomized, seeded paired-block crossover. See README.md for the protocol and
# the approved plan for the frozen decision rule. scx_mavd is deferred pending a
# kernel with BPF_F_ARENA_SCALAR; see GATES.md.
#
# All targets are namespaced `linux-sched-*` and are discovered by the
# research/ aggregator through `include $(wildcard */*.mk)`.

SHELL := /bin/bash
.ONESHELL:
# See the note in ../Makefile: without -e, .ONESHELL hides every failure before
# the last command of a recipe.
.SHELLFLAGS := -eu -o pipefail -c

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
# The data directory name carries the measurement protocol: a 180-second-window
# campaign and a 30-second one are different studies and must not share a root.
LINUX_SCHED_DATA_DIR ?= linux-sched
LINUX_SCHED_DATA_ROOT ?= $(if $(K8LABS_RESEARCH_DATA_ROOT),$(K8LABS_RESEARCH_DATA_ROOT)/$(LINUX_SCHED_DATA_DIR),$(PROJECT_ROOT)/build/research/$(LINUX_SCHED_DATA_DIR))
LINUX_SCHED_ANALYSIS_OUTPUT ?= $(LINUX_SCHED_DATA_ROOT)/analysis
# Analysis and plots are built from confirmatory results only. Pilot and smoke
# data must never reach a verdict, and after the phases were separated those live
# in their own trees — pointing either tool at the raw root finds no blocks.csv
# there and fails.
LINUX_SCHED_CONFIRMATORY_DIR ?= $(LINUX_SCHED_DATA_ROOT)/confirmatory

# Worker nodes are named after the cluster's MachineDeployment. The nodes carry
# no role labels on this lab, so `!node-role.kubernetes.io/control-plane` matches
# the control-plane node as well and would put an observer on it.
LINUX_SCHED_WORKER_PATTERN ?= -md-

# Resolve KUBECONFIG path (workload cluster).
KUBECONFIG := $(PROJECT_ROOT)/build/kubeconfig
export KUBECONFIG

# ---- Setup / validation ----

.PHONY: linux-sched-setup
linux-sched-setup: ## Verify linux-sched prerequisites (tools, cluster, worker VM access)
	@echo "=== linux-sched — prerequisites ==="
	@fail=0; \
	for cmd in kubectl jq python3; do \
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

.PHONY: linux-sched-smoke
linux-sched-smoke: ## Pipeline check for one scenario: one block into the smoke tree (SCENARIO=<name>)
	@if [ -z "$(SCENARIO)" ]; then echo "ERROR: set SCENARIO=<name> (see linux-sched-list-scenarios)" >&2; exit 1; fi
	$(LINUX_SCHED_RUNNER) run --scenario $(SCENARIO) --mode smoke --configs-dir $(LINUX_SCHED_CONFIGS_DIR) --data-root $(LINUX_SCHED_DATA_ROOT)

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

.PHONY: linux-sched-calibrate
linux-sched-calibrate: ## Freeze a scenario's offered rate on the control arm (SCENARIO=<name>)
	@if [ -z "$(SCENARIO)" ]; then echo "ERROR: set SCENARIO=<name>" >&2; exit 1; fi
	$(LINUX_SCHED_RUNNER) calibrate --scenario $(SCENARIO) --configs-dir $(LINUX_SCHED_CONFIGS_DIR) --data-root $(LINUX_SCHED_DATA_ROOT)

# ---- Analysis ----

.PHONY: linux-sched-sample-size
linux-sched-sample-size: ## Freeze each scenario's confirmatory block count from its pilot variance
	@if [ ! -f "$(LINUX_SCHED_DATA_ROOT)/pilot/blocks.csv" ]; then echo "No pilot blocks.csv under $(LINUX_SCHED_DATA_ROOT)/pilot; run the pilots first."; exit 1; fi
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/sample_size.py --pilot-blocks $(LINUX_SCHED_DATA_ROOT)/pilot/blocks.csv --output $(LINUX_SCHED_DATA_ROOT)/sample-size.json
	@echo "  Frozen counts: $(LINUX_SCHED_DATA_ROOT)/sample-size.json"

.PHONY: linux-sched-analyze
linux-sched-analyze: ## Analyze paired blocks into summary.csv/json with the frozen decision rule
	@if [ ! -f "$(LINUX_SCHED_CONFIRMATORY_DIR)/blocks.csv" ]; then echo "No confirmatory blocks.csv under $(LINUX_SCHED_CONFIRMATORY_DIR); pilot data is deliberately not analysed."; exit 1; fi
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/sched_analysis.py --blocks $(LINUX_SCHED_CONFIRMATORY_DIR)/blocks.csv --output-dir $(LINUX_SCHED_ANALYSIS_OUTPUT)
	@echo "  Summary: $(LINUX_SCHED_ANALYSIS_OUTPUT)/summary.csv"

.PHONY: linux-sched-plots
linux-sched-plots: ## Generate linux-sched plots from analyzed data
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/sched_plots.py --data-root $(LINUX_SCHED_CONFIRMATORY_DIR) --output-dir $(LINUX_SCHED_ANALYSIS_OUTPUT)
	@echo "  Plots: $(LINUX_SCHED_ANALYSIS_OUTPUT)/plots"

.PHONY: linux-sched-data-usage
linux-sched-data-usage: ## Report data-root usage and what a prune would remove
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/prune_data.py --data-root $(LINUX_SCHED_DATA_ROOT)

.PHONY: linux-sched-prune
linux-sched-prune: ## Drop large raw evidence from blocks older than KEEP_BLOCKS (default 5)
	python3 $(LINUX_SCHED_ANALYSIS_DIR)/prune_data.py --data-root $(LINUX_SCHED_DATA_ROOT) --keep-blocks $(or $(KEEP_BLOCKS),5) --apply

.PHONY: linux-sched-report
linux-sched-report: linux-sched-analyze ## Print the linux-sched analysis report pointer
	@echo "linux-sched analysis written to $(LINUX_SCHED_ANALYSIS_OUTPUT)"
	@echo "  summary.csv  — decision rule verdicts"
	@echo "  summary.json — schema-versioned machine-readable verdicts"

# ---- Node-native observer tooling ----
.PHONY: linux-sched-scx-stage
linux-sched-scx-stage: ## Stage the built sched_ext schedulers onto the research workers
	$(LINUX_SCHED_EXPERIMENTS_DIR)/stage-scx.sh

.PHONY: linux-sched-observer-check
linux-sched-observer-check: ## Check the node image installs the native observer tooling (no cluster needed)
	@echo "Checking node-native observer tooling provisioning..."
	python3 $(LINUX_SCHED_EXPERIMENTS_DIR)/observer_check.py

# ---- Observer DaemonSet ----

.PHONY: linux-sched-observers-apply
linux-sched-observers-apply: ## Validate and apply the capability-scoped observer DaemonSet
	$(LINUX_SCHED_DIR)observers/apply.sh apply

.PHONY: linux-sched-workers-label
linux-sched-workers-label: ## Label the worker nodes so the observer DaemonSet schedules on them
	@echo "Labelling nodes matching '$(LINUX_SCHED_WORKER_PATTERN)' as research workers..."
	@kubectl --kubeconfig $(KUBECONFIG) get nodes --no-headers \
		-o custom-columns=':metadata.name' \
		| grep -- '$(LINUX_SCHED_WORKER_PATTERN)' \
		| xargs -r -I{} kubectl --kubeconfig $(KUBECONFIG) label node {} \
			linux-sched/research=observer --overwrite
	@count=$$(kubectl --kubeconfig $(KUBECONFIG) get nodes \
		-l linux-sched/research=observer --no-headers | wc -l); \
	if [ "$$count" -eq 0 ]; then \
		echo "ERROR: no node matched '$(LINUX_SCHED_WORKER_PATTERN)'; set LINUX_SCHED_WORKER_PATTERN" >&2; \
		exit 1; \
	fi; \
	echo "  observer nodes: $$count"

.PHONY: linux-sched-observers-delete
linux-sched-observers-delete: ## Delete the observer DaemonSet
	$(LINUX_SCHED_DIR)observers/apply.sh delete

.PHONY: linux-sched-observers-test
linux-sched-observers-test: ## Validate the observer DaemonSet manifests offline
	$(LINUX_SCHED_DIR)observers/apply.sh test

# ---- OTel profiling campaign ----
#
# A separate campaign: it profiles all five workloads under all three arms to
# explain the comparison's results, and it must never overlap a comparison block.
# Pyroscope and its 10 GiB store are host-owned because no cluster node has room
# for them; the profiling DaemonSet runs on the workers because that is where the
# workloads are. See otel/README.md.

LINUX_SCHED_OTEL_DIR := $(LINUX_SCHED_DIR)otel
PROFILE_VOLUME ?= $(PROJECT_ROOT)/../profile-data/pyroscope
PYROSCOPE_URL ?= http://127.0.0.1:4040

.PHONY: linux-sched-otel-up
linux-sched-otel-up: ## Start the host-side Pyroscope profile store
	# A directory on a 745 GiB filesystem is not the plan's 10 GiB volume, and the
	# 9 GiB stop threshold only means something against a filesystem that fills.
	PROFILE_VOLUME=$(PROFILE_VOLUME) $(LINUX_SCHED_OTEL_DIR)/host/provision-volume.sh
	cd $(LINUX_SCHED_OTEL_DIR)/host && PROFILE_VOLUME=$(PROFILE_VOLUME) docker compose up -d
	@echo "  Pyroscope: $(PYROSCOPE_URL) (volume $(PROFILE_VOLUME))"

.PHONY: linux-sched-otel-apply
linux-sched-otel-apply: ## Deploy the Alloy profiling DaemonSet on the research workers
	@if [ -z "$(LINUX_SCHED_TARGET_NODE)" ]; then \
		echo "ERROR: set LINUX_SCHED_TARGET_NODE to the fixed target worker; profiling every worker would mislabel samples" >&2; \
		exit 1; \
	fi
	kubectl apply -f $(LINUX_SCHED_OTEL_DIR)/k8s/alloy-configmap.yaml
	sed -e "s|__TARGET_NODE__|$(LINUX_SCHED_TARGET_NODE)|g" $(LINUX_SCHED_OTEL_DIR)/k8s/alloy-daemonset.yaml \
		| kubectl apply -f -
	kubectl -n linux-sched rollout status daemonset/linux-sched-otel-alloy --timeout=300s

.PHONY: linux-sched-otel-profiles
linux-sched-otel-profiles: ## Run the fifteen labelled profiling sessions (never beside a comparison block)
	PROFILE_VOLUME=$(PROFILE_VOLUME) PYROSCOPE_URL=$(PYROSCOPE_URL) \
		K8LABS_RESEARCH_DATA_ROOT=$(K8LABS_RESEARCH_DATA_ROOT) \
		$(LINUX_SCHED_OTEL_DIR)/profiles.sh

.PHONY: linux-sched-otel-check
linux-sched-otel-check: ## Verify the OTel campaign: queries, health, counters, volume, retention, stop threshold
	PROFILE_VOLUME=$(PROFILE_VOLUME) PYROSCOPE_URL=$(PYROSCOPE_URL) \
		K8LABS_RESEARCH_DATA_ROOT=$(K8LABS_RESEARCH_DATA_ROOT) \
		$(LINUX_SCHED_OTEL_DIR)/check.sh

.PHONY: linux-sched-otel-down
linux-sched-otel-down: ## Remove the profiling DaemonSet and stop the host-side store
	-kubectl -n linux-sched delete -f $(LINUX_SCHED_OTEL_DIR)/k8s/alloy-daemonset.yaml --ignore-not-found
	-cd $(LINUX_SCHED_OTEL_DIR)/host && PROFILE_VOLUME=$(PROFILE_VOLUME) docker compose down

.PHONY: linux-sched-otel-validate
linux-sched-otel-validate: ## Validate the OTel manifests and configs offline
	python3 $(LINUX_SCHED_DIR)otel/validate_campaign.py

# ---- Test / clean ----

.PHONY: linux-sched-test
linux-sched-test: ## Run the linux-sched test suite (pytest + bats)
	@set -e
	@echo "Running linux-sched test suite..."
	uv run pytest $(LINUX_SCHED_ANALYSIS_DIR)/tests/ $(LINUX_SCHED_EXPERIMENTS_DIR)/tests/ $(LINUX_SCHED_DIR)observers/tests/ $(LINUX_SCHED_DIR)tests/ $(LINUX_SCHED_DIR)otel/tests/
	@if command -v bats >/dev/null 2>&1; then bats $(LINUX_SCHED_TEST_DIR)/*.bats; else echo "  bats not installed; skipping shell tests"; fi

.PHONY: linux-sched-clean
linux-sched-clean: ## Remove linux-sched experiment data (host-owned data root)
	@echo "Removing linux-sched data under $(LINUX_SCHED_DATA_ROOT)..."
	@rm -rf "$(LINUX_SCHED_DATA_ROOT)"
	@echo "Clean complete."
