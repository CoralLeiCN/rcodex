# Harbor development benchmarks

Harbor owns task download, containers, execution orchestration, verification, and results.
This harness adds the [rcodex adapter and container terminal tool](agents.py). The optional
[Codex baseline](baseline.py) uses Harbor's native Codex agent, with local-provider settings
and a [Docker Desktop forwarder](network.py).

The harness is an independent development project: its [pyproject.toml](pyproject.toml),
[lockfile](uv.lock), and `.venv` live here. Harbor requires Python 3.12+; rcodex core keeps
Python 3.11+, its own dependencies and lockfile, and no Harbor imports. The harness depends
on an editable checkout of rcodex. It is excluded from core wheel and source distributions.

Store local result notes under `benchmarks/harbor/results/`, which is ignored by Git.
The local index is `results/README.md`; `results/known-passing-tasks.md` records task IDs,
token usage, completion outcomes, and rerun commands when those notes are present.
The [reusable configuration](known-passing-tasks.yaml) remains available in the repository.
Nginx is the recommended first task because both agents passed verification and completed
normally in the recorded development run.

## Run one task

Run these commands from the repository root, with Docker running:

```bash
make bench-setup
RCODEX_MODEL=qwen3.8-27b-fp8 make bench-rcodex
```

Set `RCODEX_PROVIDER_BASE_URL` to your server's API base URL (including `/v1`) in the
untracked root `.env`. Both benchmark presets read it through the shared settings loader.
Use a model ID returned by that server's `/v1/models`. Explicit CLI settings take precedence
over environment variables, which take precedence over `.env`. If the server needs a key,
set `RCODEX_PROVIDER_API_KEY`.

You can also use Harbor directly, without the Makefile or YAML:

```bash
uv run --locked --project benchmarks/harbor python -m harbor.cli.main run \
  -d terminal-bench/terminal-bench-2-1 \
  -a benchmarks.harbor.agents:RcodexAgent \
  -m qwen3.8-27b-fp8 \
  --ak reasoning_effort=low \
  --agent-timeout-multiplier 2 \
  --include-task-name terminal-bench/openssl-selfsigned-cert \
  -e docker -k 1 -n 1 -r 0
```

The `uv` prefix selects the independent environment; `python -m` keeps the repository's
adapter importable. Harbor's remaining arguments are standard. This runs one selected task,
one attempt, and no retries. Uploading results is optional and is not enabled.

## Compare with Codex

```bash
RCODEX_MODEL=qwen3.8-27b-fp8 make bench        # Both agents, sequentially
RCODEX_MODEL=qwen3.8-27b-fp8 make bench-codex  # Baseline only
```

The [comparison configuration](terminal-bench-2.1.yaml) selects only
`terminal-bench/openssl-selfsigned-cert`: one attempt per agent, fresh containers, no retries.
Harbor deletes containers after verification. Both request low reasoning effort and use a
1,800-second agent timeout: the job's agent-only multiplier doubles this task's standard
900 seconds. rcodex's internal run/node deadlines are also 1,800 seconds, with depth 1,
concurrency 1, 12 REPL iterations, and a 200,000-token cumulative budget. The timeout override
is for development experiments; verifier and setup deadlines retain their task defaults.

The baseline is headless `codex exec` 0.153.4 with native prompts and tools. Harbor installs
and runs it in the container and parses its usage. The baseline module configures the same
local Responses provider without importing the host's OpenAI login. Web search, apps, and
built-in multi-agent tools are disabled for both agents; each uses a private Codex home.

On this development Mac, containers cannot reach the LAN API directly. The baseline's
`provider_gateway_host: host.docker.internal` starts a temporary loopback TCP forwarder
to that exact HTTP endpoint. It streams bytes and closes after the trial. It is optional
on hosts with direct connectivity and is not used by rcodex. For direct access, remove
that setting from the YAML or pass `HARBOR_ARGS="--ak provider_gateway_host=null"` to Make.
HTTPS providers require direct container connectivity.

Pass additional Harbor options through Make:

```bash
make bench HARBOR_ARGS="--job-name comparison-001"
make bench-rcodex HARBOR_ARGS="--ak max_tokens=300000"
```

All targets use the adapter defaults described above; pass changed agent settings with
`--ak`. Harbor's `--agent` option replaces YAML agent entries, so single-agent targets do
not inherit custom YAML agent kwargs. Job-level settings apply to all Make run targets.

## Adapter boundary and results

The rcodex adapter supplies Harbor's instruction to `RecursiveRunner` and registers one
trusted controller tool, `terminal`, that calls `BaseEnvironment.exec`. The model's local
context contains only the instruction. Commands act on the container as the task's configured
user; they use fresh shells with an optional working directory and a 1–120 second timeout.
Full command output is logged, while output returned to the model is bounded.

The adapter maps rcodex usage and failures into Harbor's result context so verification can
still run after a timeout or partial result. Logs are under `jobs/<job-name>/`: trial
`result.json`, verifier results, `agent/terminal.jsonl`, and controller state under
`agent/rcodex-state/runs/<run-id>/`, including its `result.json` and iteration artifacts.
Baseline logs include CLI events, sessions, and Harbor's trajectory.
Private Codex caches stay outside the logs copied into the container.

```bash
make view
make bench-test
make bench-check
```

The offline tests exercise container routing, usage/error reporting, baseline configuration,
and forwarding without model calls. Core tests and checks remain `make test check`.

The generic core fixes discovered during integration remain independently tested in core:
REPL tool-call guidance, per-process Codex homes, and recording rejected calls without
overflowing iteration artifacts. They introduce no Harbor dependency or benchmark-specific
task behavior.

Save each experiment's results, token usage, and evidence references in the local `results/`
directory. Both these notes and raw `jobs/` artifacts are excluded from Git so repeated
experiments do not accumulate in the repository. Record verifier outcomes and runner
completion separately, compare execution time separately from installation and verification,
and account for the agents' stopping limits and shared model-server load.

See [Harbor's agent interface](https://www.harborframework.com/docs/agents) and the
[Terminal-Bench 2.1 repository](https://github.com/harbor-framework/terminal-bench-2-1).
