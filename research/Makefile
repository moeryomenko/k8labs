# research/ Makefile — pure aggregator
#
# Every target lives in a series makefile (<series>/<series>.mk, e.g.
# cpu-sched/cpu-sched.mk) and is namespaced per series (cpu-sched-*). This
# file defines no experiment/analysis/setup/clean targets of its own; it only
# includes the series makefiles and owns the help listing.

SHELL := /bin/bash
.ONESHELL:

include $(wildcard */*.mk)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Prints this help message
	@echo "Commands:"
	@grep -F -h '##' $(MAKEFILE_LIST) \
		| grep -F -v fgrep \
		| sort \
		| grep -E '^[a-zA-Z_/.-]+:.*?## .*$$' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
