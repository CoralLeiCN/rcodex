.DEFAULT_GOAL := help

# Keep experiments sequential even when make is invoked with -j.
.NOTPARALLEL:
.PHONY: help setup bench-setup bench bench-codex bench-rcodex view test check bench-test bench-check

UV ?= uv
BENCH_CONFIG ?= benchmarks/harbor/terminal-bench-2.1.yaml
HARBOR_ARGS ?=
RESULTS ?= jobs

BENCH_RUN = $(UV) run --locked --project benchmarks/harbor
# Module execution makes the repository's benchmark package importable without installing it.
HARBOR = $(BENCH_RUN) python -m harbor.cli.main

help:
	@printf '%s\n' \
	  'make setup         Install locked core development dependencies' \
	  'make bench-setup   Install the independent benchmarks/harbor project' \
	  'make bench         Compare Codex and rcodex on the one configured task' \
	  'make bench-codex   Run only the native Codex baseline' \
	  'make bench-rcodex  Run only rcodex' \
	  'make view         Open the Harbor results viewer (RESULTS=jobs)' \
	  'make test         Run core offline tests without Harbor' \
	  'make check        Run core Ruff and mypy checks' \
	  'make bench-test   Run Harbor integration tests without model calls' \
	  'make bench-check  Run benchmark Ruff and mypy checks' \
	  '' \
	  'Model and provider settings come from .env; start Docker before benchmarking.' \
	  'Append Harbor options with HARBOR_ARGS="--job-name my-experiment".' \
	  'See benchmarks/harbor/README.md for limits and provider options.'

setup:
	$(UV) sync --locked

bench-setup:
	$(UV) sync --locked --project benchmarks/harbor

bench:
	$(HARBOR) run -c "$(BENCH_CONFIG)" $(HARBOR_ARGS)

bench-codex:
	$(HARBOR) run -c "$(BENCH_CONFIG)" --agent benchmarks.harbor.baseline:CodexBaselineAgent --ak provider_gateway_host=host.docker.internal $(HARBOR_ARGS)

bench-rcodex:
	$(HARBOR) run -c "$(BENCH_CONFIG)" --agent benchmarks.harbor.agents:RcodexAgent $(HARBOR_ARGS)

view:
	$(HARBOR) view "$(RESULTS)"

test:
	$(UV) run --locked pytest

check:
	$(UV) run --locked ruff check src tests
	$(UV) run --locked mypy src tests

bench-test:
	$(BENCH_RUN) python -m pytest -c benchmarks/harbor/pyproject.toml benchmarks/harbor/tests

bench-check:
	$(BENCH_RUN) ruff check benchmarks
	$(BENCH_RUN) mypy --config-file benchmarks/harbor/pyproject.toml
