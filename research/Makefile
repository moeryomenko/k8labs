# research/ Makefile — pure aggregator
#
# Every target lives in a series makefile (<series>/<series>.mk, e.g.
# linux-sched/linux-sched.mk) and is namespaced per series (linux-sched-*). This
# file defines no experiment/analysis/setup/clean targets of its own; it only
# includes the series makefiles and owns the help listing.

SHELL := /bin/bash
.ONESHELL:
# With .ONESHELL the whole recipe runs in one shell, and without -e only the
# *last* command's status decides whether the target failed. Every earlier
# failure — a metric that could not be computed, a query that errored — is then
# reported as success, which is how a failed sample-size step let a run continue
# to the next phase with no frozen counts.
.SHELLFLAGS := -eu -o pipefail -c

include $(wildcard */*.mk)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Prints this help message
	@echo "Commands:"
	@grep -F -h '##' $(MAKEFILE_LIST) \
		| grep -F -v fgrep \
		| sort \
		| grep -E '^[a-zA-Z0-9_/.-]+:.*?## .*$$' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
