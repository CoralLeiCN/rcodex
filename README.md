# rcodex

A bounded, Codex-native Recursive Language Model runtime. rcodex keeps a large local context
outside the model prompt, lets recursive Codex nodes request focused child work between turns,
and returns a strict result with a durable JSON/JSONL execution trajectory.

The runtime is inference-only. It does not include or require RLM training code.

For recursive runs, each retained Codex node programs a persistent, restricted Python REPL. The
injected `llm_query*` and `rlm_query*` functions synchronously return child answer strings, so
Python can inspect, transform, branch on, aggregate, and make dependent calls before submitting
the final answer. Strict JSON remains the internal RPC/artifact/result boundary; it is not the
root Codex programming language.

Implementation baseline: Python 3.11+, `openai-codex==0.147.0`, `AsyncCodex`, and an external
Codex 0.151.0 executable selected through the SDK's supported `codex_bin` configuration.

## Setup

```bash
codex --version  # must report codex-cli 0.151.0
uv sync --locked
uv run pytest     # offline suite; live tests are excluded by default
```

rcodex resolves `codex` from `PATH`. Set `RCODEX_CODEX_BIN` to an executable path when the
required runtime is installed elsewhere. Startup fails if App Server reports another version.
Authenticated tests require both explicit gates and consume Codex usage:
`RCODEX_LIVE_TESTS=1 uv run pytest -m live`.

## Run

Recursive inference is the default strategy:

```bash
uv run rcodex run \
  --task "Review this repository and explain the architecture with evidence" \
  --context . \
  --max-depth 2 \
  --max-concurrency 4 \
  --output ../recursive-result.json
```

Run one terminal Codex leaf without recursive delegation:

```bash
uv run rcodex run \
  --task "Summarize the architecture with evidence" \
  --context ./docs \
  --strategy direct
```

Use a Nebius Token Factory model through its OpenAI-compatible Responses endpoint. The optional
key is read from the fixed `RCODEX_PROVIDER_API_KEY` environment variable and is never stored in
run artifacts:

```bash
export RCODEX_PROVIDER_API_KEY="your-key"

uv run rcodex run \
  --task "Summarize the architecture with evidence" \
  --context . \
  --model "your-nebius-model-id" \
  --provider-base-url "https://api.tokenfactory.nebius.com/v1"
```

The CLI automatically loads `.env` from its working directory without replacing variables that
are already present in the process environment. The model and provider can therefore be stored as:

```dotenv
RCODEX_MODEL=your-nebius-model-id
RCODEX_PROVIDER_BASE_URL=https://api.tokenfactory.nebius.com/v1
RCODEX_PROVIDER_API_KEY=your-key
```

Then the equivalent command needs no provider flags:

```bash
uv run rcodex run \
  --task "Summarize the architecture with evidence" \
  --context .
```

`RCODEX_SUB_MODEL` optionally sets the default child model. Explicit CLI options take precedence
over values loaded from `.env`. Keep `.env` out of version control because it can contain the
provider credential.

For an unauthenticated local server, leave `RCODEX_PROVIDER_API_KEY` unset:

```bash
uv run rcodex run \
  --task "Summarize the architecture with evidence" \
  --context . \
  --model "served-model-name" \
  --provider-base-url "http://127.0.0.1:8000/v1"
```

The endpoint must implement the OpenAI Responses API at `/v1/responses`, including streaming,
function tools, and structured JSON output used by Codex. A server that only implements
`/v1/chat/completions` is not compatible.

Start a persistent recursive session, then resume it with the ID printed to stderr:

```bash
uv run rcodex run \
  --task "Review this repository and remember the main risks" \
  --context . \
  --persistent

uv run rcodex run \
  --task "Now identify the highest-risk part" \
  --context . \
  --persistent \
  --session session_0123456789abcdef0123456789abcdef
```

The task and context directory supplied by the caller are the real inputs; rcodex does not
generate synthetic test inputs for ordinary runs. The command writes the complete `RunResult`
to stdout unless `--output` is supplied. By default, state goes to a stable context-keyed sibling
such as `/work/.rcodex-state/repo-<digest>` for context `/work/repo`; use `--state-dir` to
override it with another non-overlapping location. On POSIX, rcodex creates new state directories
owner-only (`0700`) and state files owner-readable/writable (`0600`). Every existing state
directory, including an explicitly selected one, must already deny group and other access;
rcodex rejects an insecure directory rather than changing its permissions.

## Python API

```python
from pathlib import Path

from rcodex import RLM, RunConfig

config = RunConfig(max_depth=2, max_concurrency=4, max_tokens=100_000)

with RLM(context=Path("."), config=config) as rlm:
    completion = rlm.completion("Review this repository and explain its architecture.")
    print(completion.response)
    print(completion.run.nodes)
```

`AsyncRLM` provides the same `completion`, `direct_completion`, and `completion_batched`
operations for async callers. The Python API also accepts trusted controller tools and
iteration/subcall callbacks. With `RunConfig(persistent=True)`, the facade preallocates and
exposes `rlm.session_id`; only one process may own that session while a completion is active.
See the [implemented specification](docs/spec.md#9-python-api).

## Security boundary

Codex runs with a read-only sandbox, denied approvals, and requested disabling of built-in
delegation, apps, web search, and MCP. rcodex does not support MCP, but it cannot guarantee that
Codex starts without MCP servers inherited from ambient Codex configuration. Every run prints a
warning before Codex work begins. Review or isolate the host MCP configuration before running;
continuing accepts the residual risk that ambient tools can receive data or take external
actions with configured credentials.

Trusted Python tools registered through the API are controller calls, not MCP tools. They run in
the rcodex process and therefore belong inside the caller's trust boundary.

Model-authored Python never runs in the controller process. It runs in a separate `python -I -S`
worker with restricted syntax and builtins, no imports or file APIs, bounded output and resources,
and a private temporary working directory. This is defense in depth rather than a portable
hostile-code container; the configured address-space limit is enforced on Linux and reported as
unsupported on macOS.

## Documentation

- [Implemented specification](docs/spec.md) — the single authoritative runtime, protocol, API,
  limits, persistence, artifacts, and security contract.
- [RLM, rcodex, and Codex comparison](docs/rlm-rcodex-codex-comparison.md) — how roots, leaves,
  recursive children, parallelism, depth boundaries, and tree recording differ.
- [References](references/README.md) — pinned RLM, Codex, and technology sources.

## Acknowledgements

rcodex is inspired by the [*Recursive Language Models* paper](https://arxiv.org/abs/2512.24601v3)
by Alex L. Zhang, Tim Kraska, and Omar Khattab, and the
[`alexzhang13/rlm` reference implementation](https://github.com/alexzhang13/rlm). See the
[RLM source references](references/rlm.md) for the pinned implementation revision.
