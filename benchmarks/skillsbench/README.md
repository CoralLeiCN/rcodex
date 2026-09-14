# SkillsBench with the official BenchFlow workflow

This setup follows [SkillsBench's Quick Start](https://github.com/benchflow-ai/skillsbench#quick-start):
validate with `bench tasks check`, run the reference solution, then evaluate agents with
`bench eval run --sandbox docker`. The dedicated `benchmarks/skillsbench/Makefile` is a shortcut for those commands;
the root Makefile selects Harbor.
**Development selects only `dialogue-parser`, one attempt, concurrency 1.**

Codex uses BenchFlow's **built-in `codex` agent** (`codex-acp`), including its standard
installation, prompts, provider configuration, skill loading, and trajectory collection.
This is a headless Codex agent baseline, not an interactive desktop UI session.

rcodex uses the documented [agent-package entry point](https://github.com/benchflow-ai/benchflow/blob/main/docs/external-agents.md#4-plugin-packages-entry-points).
Its small adapter connects BenchFlow's task prompt to `RecursiveRunner` and exposes one
container shell tool for reading inputs/skills and writing deliverables. The ACP SDK owns
the transport. No custom evaluation loop, provider registry, Codex baseline, scoring,
summary format, model instruction file, or native-shell feature overrides are used.
Task files and verifiers are unchanged.

## Install

Requirements: Docker running, `uv`, Python 3.12+, and network access for initial container
installation. Both the host and Docker must reach the local model server.

From the rcodex repository root:

```bash
make -f benchmarks/skillsbench/Makefile bench-install
# Clone only if this checkout does not already exist.
git clone https://github.com/benchflow-ai/skillsbench.git benchmarks/skillsbench/upstream
git -C benchmarks/skillsbench/upstream checkout 9a1f4dd5f7659f75707435da3ce854b6e48321d1
docker info
curl http://192.168.1.220:30000/v1/models
make -f benchmarks/skillsbench/Makefile bench-check
make -f benchmarks/skillsbench/Makefile bench-oracle
```

The official docs recommend installing the latest BenchFlow CLI with `uv tool install
--python 3.12 benchflow`. Here, `make -f benchmarks/skillsbench/Makefile bench-install` installs the same CLI into a separate,
locked project with the local rcodex plugin. This checkout pins **BenchFlow 0.7.6**;
`uv run --locked --project benchmarks/skillsbench bench --version` shows the active version.
It does not replace a global BenchFlow installation or change your Codex account.

Keep the dataset checkout at the recorded revision for comparable experiments. The standard
CLI records task content digests; it does not enforce our recommended Git revision when
using `--tasks-dir`. To evaluate a published dataset release instead, use the official
[`--dataset` workflow](https://github.com/benchflow-ai/skillsbench/blob/main/docs/dataset-versioning.md)
and obey that release's BenchFlow version requirements.

## Local provider

The server currently advertises `qwen3.8-27b-fp8`. These are the Makefile defaults:

```bash
make -f benchmarks/skillsbench/Makefile bench MODEL=qwen3.8-27b-fp8 BASE_URL=http://192.168.1.220:30000/v1
```

The built-in `vllm/` provider prefix is BenchFlow's route for a user-supplied
OpenAI-compatible endpoint; our server is SGLang. Both agents use Responses requests.
The URL ends in `/v1`. No real key is required here; `OPENAI_API_KEY=local-no-key` supplies
the nonempty value expected by the provider integration. The Makefile supplies it when
neither `OPENAI_API_KEY` nor `RCODEX_PROVIDER_API_KEY` is set. For an authenticated server,
export the appropriate key rather than putting it in the command line or source files.
`MODEL`/`BASE_URL` also inherit `RCODEX_MODEL`/`RCODEX_PROVIDER_BASE_URL` when exported.
The Makefile does not load `.env` files.

## Rerun one task

```bash
make -f benchmarks/skillsbench/Makefile bench             # rcodex and built-in Codex, separate fresh task containers
make -f benchmarks/skillsbench/Makefile bench-rcodex
make -f benchmarks/skillsbench/Makefile bench-codex
make -f benchmarks/skillsbench/Makefile bench-oracle      # reference solution; no model requests
make -f benchmarks/skillsbench/Makefile bench-check       # structural validation; no Docker or model requests
```

`make -f benchmarks/skillsbench/Makefile bench` runs both CLI evaluations even if one exits with an error. It produces
standard BenchFlow summaries for each agent. Results from earlier invocations are preserved.

The direct official CLI command for the Codex baseline is:

```bash
export OPENAI_API_KEY=local-no-key
uv run --locked --project benchmarks/skillsbench bench eval run \
  --tasks-dir benchmarks/skillsbench/upstream/tasks/dialogue-parser \
  --expected-tasks 1 --agent codex --model vllm/qwen3.8-27b-fp8 \
  --agent-env BENCHFLOW_PROVIDER_BASE_URL=http://192.168.1.220:30000/v1 \
  --sandbox docker --concurrency 1 --retry-attempts 0 --skill-mode with-skill \
  --reasoning-effort low --agent-idle-timeout 0 \
  --jobs-dir "benchmarks/skillsbench/jobs/codex-$(date -u +%Y%m%dT%H%M%S)-$$"
```

`make -f benchmarks/skillsbench/Makefile bench-rcodex` uses the same CLI with `--agent rcodex` and environment variables
for the recursive controller's limits. Use `make -f benchmarks/skillsbench/Makefile -n bench-rcodex` to inspect the command.
No time budget override is supplied by default: BenchFlow reads each task's standard
agent and verifier budgets from `task.md`. `--expected-tasks 1` prevents a mistyped
development path from silently selecting the full collection. Every run needs a fresh jobs directory:
BenchFlow resumes existing directories and can skip completed tasks.

For `dialogue-parser`, the [official task definition](https://github.com/benchflow-ai/skillsbench/blob/9a1f4dd5f7659f75707435da3ce854b6e48321d1/tasks/dialogue-parser/task.md)
sets **900 seconds per agent** and **900 seconds for the verifier**.
Other defaults are supplied skills, requested `low` reasoning effort,
one attempt, and task concurrency 1. rcodex additionally uses 10 REPL iterations,
depth 1, and model concurrency 1. Codex uses its standard native tool loop. These are
time-matched development runs, not matching internal agent architectures or a reproduction
of the paper's settings.

BenchFlow enforces the task deadline for both agents. The rcodex adapter responds to ACP
cancellation; its required internal run/node/leaf deadlines use rcodex's maximum of 24 hours
so they do not shorten normal task budgets. This is a fallback ceiling, not the task's
allocated runtime. Shell commands retain their separate 120-second maximum. To explicitly
override the agent budget, set `TIMEOUT`; the Makefile passes it through the official
`--config-override` option. The verifier keeps its task-defined budget.

```bash
make -f benchmarks/skillsbench/Makefile bench TIMEOUT=600 MAX_ITERATIONS=20
make -f benchmarks/skillsbench/Makefile bench SKILL_MODE=no-skill
make -f benchmarks/skillsbench/Makefile bench TASK=dialogue-parser REASONING_EFFORT=low
```

Other overrides: `MAX_DEPTH`, `SKILLSBENCH`, `JOBS_DIR`, and `UV`. `make -f benchmarks/skillsbench/Makefile` shows help.
The server can interpret or ignore reasoning effort according to its implementation.
Built-in Codex and rcodex may use
different Codex runtime versions: rcodex requires **0.153.4 / Python SDK 0.147.0**;
BenchFlow owns the baseline's **codex-acp 1.6.0** installation and its bundled runtime.

## Full benchmark later

These targets explicitly select every task under the pinned `tasks/` directory,
excluding `tasks-extra/`. They use each task's standard agent and verifier budgets,
concurrency 1, no retries, and rcodex limits of 60 iterations and depth 2. They were not run
during development.

```bash
make -f benchmarks/skillsbench/Makefile bench-all-oracle
make -f benchmarks/skillsbench/Makefile bench-all
make -f benchmarks/skillsbench/Makefile bench-all SKILL_MODE=no-skill
# Optional individual agents:
make -f benchmarks/skillsbench/Makefile bench-all-rcodex
make -f benchmarks/skillsbench/Makefile bench-all-codex
```

The direct CLI equivalent uses `--tasks-dir benchmarks/skillsbench/upstream/tasks`
without `--expected-tasks 1`. Keep the model, endpoint, dataset revision, skill mode,
time budget, and machine consistent across agents. Repeat runs for independent attempts.

## What remains custom

All integration code lives outside the core package in `benchmarks/skillsbench/`:

- `skillsbench_agents/__init__.py`: official plugin registration and a source-only bundle
  of the current checkout for container installation. It includes no task answers, tests,
  credentials, or previous results. This allows development with uncommitted rcodex changes
  without publishing a package or modifying task Dockerfiles.
- `skillsbench_agents/agent.py`: the task-to-rcodex adapter and bounded shell tool.
- `install.sh`: installs rcodex's required runtime in an isolated container interpreter.

The adapter uses rcodex's ordinary prompts and runtime configuration. Its short user
prologue identifies the task workspace, `/skills`, and the shell tool. Both agents run as
BenchFlow's unprivileged task user; BenchFlow manages Docker, skill deployment, model
routing/usage capture, verification, retries, and exported artifacts.

```bash
make -f benchmarks/skillsbench/Makefile bench-test          # offline adapter/plugin checks, no Docker or inference
uv run --locked pytest   # core rcodex tests
```

## Results

Make targets write to `benchmarks/skillsbench/jobs/<run-id>/<target>/`, with a timestamped
BenchFlow job directory beneath it. Inspect the standard `summary.json` and each task's
`result.json`, `trajectory/`, `agent/`, and `verifier/`. `bench eval metrics JOB_DIRECTORY`
provides the standard aggregate report. No custom result schema is maintained.

An actual reward below 1 is a scored task failure. A timeout can still have a verifier
reward; report both the reward and the error. `rewards: null` means no score was obtained,
not an implicit zero. Check each task's tool calls, provider tokens, and verifier output
before interpreting a result; CLI exit status alone is not an accuracy score. Interrupted
requests can have incomplete token telemetry, and these errored runs have aggregate token
totals of zero despite recorded usage in the task's `agent_result`.

Generated validation reports belong in `benchmarks/skillsbench/reports/`, which is ignored
along with `jobs/` and the downloaded `upstream/` checkout. These files stay local and are
not included in a PR or a Git merge. Copy reports and any required evidence separately
when moving between worktrees.

The upstream `experiments/sanity-tasks/hello-world` task is a quick integration check. It
has no bundled skills and verifies one text file. To run it with both agents under the
same 900-second cap:

```bash
make -f benchmarks/skillsbench/Makefile bench \
  TASKS_DIR=benchmarks/skillsbench/upstream/experiments/sanity-tasks/hello-world \
  SKILL_MODE=no-skill TIMEOUT=900
```

A passing sanity check validates basic execution, not task accuracy or skill use. Native
Codex shell calls can still fail inside Docker when bubblewrap is missing or nested user
namespaces are unavailable. Inspect trajectories for recovered errors as well as the final
verifier result.

References: [SkillsBench Quick Start](https://github.com/benchflow-ai/skillsbench#quick-start),
[BenchFlow CLI reference](https://github.com/benchflow-ai/benchflow/blob/main/docs/reference/cli.md),
[external agent plugins](https://github.com/benchflow-ai/benchflow/blob/main/docs/external-agents.md),
and [official one-task validation guide](https://github.com/benchflow-ai/skillsbench/blob/main/docs/agent-quickstart.md).
