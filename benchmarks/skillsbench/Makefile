.DEFAULT_GOAL := help
.NOTPARALLEL:

UV ?= uv
SKILLSBENCH ?= benchmarks/skillsbench/upstream
TASK ?= dialogue-parser
TIMEOUT ?=
MAX_ITERATIONS ?= 10
MAX_DEPTH ?= 1
SKILL_MODE ?= with-skill
REASONING_EFFORT ?= low
MODEL ?= $(if $(RCODEX_MODEL),$(RCODEX_MODEL),qwen3.8-27b-fp8)
BASE_URL ?= $(if $(RCODEX_PROVIDER_BASE_URL),$(RCODEX_PROVIDER_BASE_URL),http://192.168.1.220:30000/v1)
JOBS_DIR ?= benchmarks/skillsbench/jobs
RUN_ID := $(shell date -u +%Y%m%dT%H%M%S)-$(shell printf '%s' $$$$)
export OPENAI_API_KEY ?= $(if $(RCODEX_PROVIDER_API_KEY),$(RCODEX_PROVIDER_API_KEY),local-no-key)

BENCH = $(UV) run --locked --project benchmarks/skillsbench bench
TASKS_DIR = $(SKILLSBENCH)/tasks/$(TASK)
SELECTION = --expected-tasks 1
COMMON = --tasks-dir "$(TASKS_DIR)" $(SELECTION) --sandbox docker --concurrency 1 \
	--retry-attempts 0 --skill-mode "$(SKILL_MODE)" --agent-idle-timeout 0 \
	$(if $(strip $(TIMEOUT)),--config-override '{"agent":{"timeout_sec":$(TIMEOUT)}}')
PROVIDER = --model "vllm/$(MODEL)" \
	--agent-env "BENCHFLOW_PROVIDER_BASE_URL=$(BASE_URL)"

.PHONY: help bench-install bench-test bench-check bench bench-rcodex bench-codex bench-oracle \
	bench-all bench-all-rcodex bench-all-codex bench-all-oracle

help:
	@printf '%s\n' \
	  'make -f benchmarks/skillsbench/Makefile bench-install     Install BenchFlow and the local rcodex agent plugin' \
	  'make -f benchmarks/skillsbench/Makefile bench-test        Run offline rcodex adapter checks' \
	  'make -f benchmarks/skillsbench/Makefile bench-check       Run the official task validator on TASK' \
	  'make -f benchmarks/skillsbench/Makefile bench             Run rcodex and built-in Codex on TASK' \
	  'make -f benchmarks/skillsbench/Makefile bench-rcodex      Run rcodex on TASK' \
	  'make -f benchmarks/skillsbench/Makefile bench-codex       Run built-in Codex on TASK' \
	  'make -f benchmarks/skillsbench/Makefile bench-oracle      Run the reference solution on TASK' \
	  'make -f benchmarks/skillsbench/Makefile bench-all         Run both agents on the full suite' \
	  'make -f benchmarks/skillsbench/Makefile bench-all-oracle  Validate all reference solutions' \
	  '' \
	  'Defaults: TASK=dialogue-parser, task-defined time budgets, MAX_ITERATIONS=10 MAX_DEPTH=1' \
	  'TIMEOUT=seconds explicitly overrides each task agent budget (dialogue-parser: 900s).' \
	  'Overrides: MODEL=... BASE_URL=... SKILL_MODE=no-skill REASONING_EFFORT=low' \
	  'Each invocation gets fresh results under JOBS_DIR. Plain make shows this help.'

bench-install:
	$(UV) sync --locked --project benchmarks/skillsbench

bench-test:
	$(UV) run --locked --project benchmarks/skillsbench pytest -c benchmarks/skillsbench/pyproject.toml benchmarks/skillsbench/tests

bench-check:
	$(BENCH) tasks check "$(SKILLSBENCH)/tasks/$(TASK)"

bench:
	$(MAKE) -f benchmarks/skillsbench/Makefile --keep-going bench-rcodex bench-codex RUN_ID="$(RUN_ID)"

bench-all:
	$(MAKE) -f benchmarks/skillsbench/Makefile --keep-going bench-all-rcodex bench-all-codex RUN_ID="$(RUN_ID)"

bench-all-rcodex bench-all-codex bench-all-oracle: TASKS_DIR = $(SKILLSBENCH)/tasks
bench-all-rcodex bench-all-codex bench-all-oracle: SELECTION =
bench-all-rcodex bench-all-codex bench-all-oracle: MAX_ITERATIONS = 60
bench-all-rcodex bench-all-codex bench-all-oracle: MAX_DEPTH = 2

bench-codex bench-all-codex:
	$(BENCH) eval run $(COMMON) $(PROVIDER) --reasoning-effort "$(REASONING_EFFORT)" \
	  --agent codex --jobs-dir "$(JOBS_DIR)/$(RUN_ID)/$@"

bench-rcodex bench-all-rcodex:
	$(BENCH) eval run $(COMMON) $(PROVIDER) --agent rcodex --jobs-dir "$(JOBS_DIR)/$(RUN_ID)/$@" \
	  --agent-env "RCODEX_BENCH_MAX_ITERATIONS=$(MAX_ITERATIONS)" \
	  --agent-env "RCODEX_BENCH_MAX_DEPTH=$(MAX_DEPTH)" \
	  --agent-env "RCODEX_BENCH_REASONING_EFFORT=$(REASONING_EFFORT)"

bench-oracle bench-all-oracle:
	$(BENCH) eval run $(COMMON) --agent oracle --jobs-dir "$(JOBS_DIR)/$(RUN_ID)/$@"
