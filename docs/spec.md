# Recursive Codex Implemented Specification

- Status: authoritative for the current inference runtime
- Version: 1.0
- Date: 2026-09-01
- Target platforms: macOS and Linux

## 1. Scope and authority

This file is the single authoritative specification for implemented rcodex behavior. It covers
the direct and recursive strategies, controller protocol, limits, persistent sessions,
compaction, trusted tools, callbacks, artifacts, CLI, Python API, and security boundary.

Strict models in `src/rcodex/models.py`, the public Python API, CLI behavior, and tests are
executable conformance surfaces. A disagreement between one of those surfaces and this document
is a defect; an incidental implementation detail does not silently revise the contract.

rcodex is an inference runtime, not a drop-in copy of the upstream RLM package. It implements
RLM's inference semantics with Codex threads and controller-mediated actions. It deliberately
does not include training code, training dependencies, learned-policy workflows, or a training
CLI. Training is outside product scope.

## 2. RLM semantics and Codex adaptation

The source interpretation is pinned to the [RLM paper and `alexzhang13/rlm` commit
`854e688`](../references/rlm.md). rcodex preserves the inference loop while changing the
execution substrate:

| RLM behavior | rcodex implementation |
| --- | --- |
| Keep large context outside the root model window | Keep a caller-supplied directory on disk and give Codex a content-free manifest. |
| Root REPL loop | Retain one Codex thread and one persistent restricted Python worker for that recursive node. |
| Execute a root-produced program | Extract fenced `repl` blocks and execute them in the node's worker, never in the controller process. |
| `llm_query` | Make a synchronous worker-to-controller RPC that starts one fresh terminal Codex leaf and returns its answer string to the running Python block. |
| `rlm_query` | Make the same RPC to a fresh recursive Codex child with its own retained thread and REPL while depth permits. |
| Recursive call at terminal depth | Record the request as recursive but execute it as a terminal leaf. |
| Batched subcalls | `llm_query_batched` and `rlm_query_batched` execute bounded calls concurrently and return strings in request order. |
| Persistent environment | Keep Python variables across turns of one node; persist and resume only the root Codex thread across separate completions. |
| Compaction | Compact a retained recursive thread after a measured threshold and wait until the public SDK read surface reports the completed compaction item. |
| Custom root/sub tools | Expose controller-backed Python callables inside the REPL. |
| Trajectory logger | Persist strict node, iteration, call-result, result, and JSONL event artifacts. |

rcodex executes model-generated Python in a separate restricted worker process, never in its
controller process or a trusted tool handler. A Codex leaf remains a read-only agent turn rather
than a plain text-only LM request. All nodes may inspect the full authorized context directory;
manifest references are navigation and evidence anchors, not a confidentiality boundary between
nodes.

The upstream provider matrix, container environment matrix, visualizer application, and training
harness are not copied. Codex is the one agent substrate. rcodex supplies its own restricted local
REPL worker and keeps normalized JSON artifacts as the durable controller boundary. These are
intentional Codex-native adaptations.

## 3. Technology and Codex integration

| Area | Implemented decision |
| --- | --- |
| Language | Python 3.11+ |
| Codex SDK | Exactly `openai-codex==0.147.0`, using `AsyncCodex` |
| Codex runtime | External Codex CLI/App Server 0.151.0 selected with `CodexConfig.codex_bin` |
| Concurrency | Standard-library `asyncio`, tasks, semaphores, and monotonic deadlines |
| Validation | Pydantic v2 strict models with unknown fields rejected |
| CLI | Standard-library `argparse` |
| Persistence | Atomic JSON plus append-only JSONL |
| Packaging | `pyproject.toml`, `src/` layout, Hatchling, and committed `uv.lock` |
| Quality | pytest, pytest-asyncio, Ruff, and strict mypy |

rcodex resolves `codex` from `PATH`, or from the executable path in `RCODEX_CODEX_BIN`. It does
not select the SDK's packaged 0.147.0 fallback. After SDK startup, rcodex reads App Server
metadata and fails unless the reported runtime release is 0.151.0. The SDK owns authentication;
rcodex fails startup when no authenticated account is available and does not implement a
credential store.

Each opened thread requests:

- `Sandbox.read_only` and `ApprovalMode.deny_all`;
- built-in agents disabled;
- apps/connectors disabled;
- web search disabled;
- an empty MCP server mapping and no tool suggestions.

Leaves use fresh ephemeral threads. Recursive nodes retain a thread and own a separate Python
worker for their REPL loop. Non-persistent recursive threads are ephemeral; a persistent root
thread is not. Every Python worker is run-scoped and is closed at node completion, including for
a root Codex thread that will be resumed in a later invocation. Adapter, SDK, and live Python
objects never appear in persisted schemas.

## 4. Inputs and external context

The common operation is:

```text
(task, context directory, strategy, controller policy) -> strict result plus run artifacts
```

The task is trimmed, must contain 1 to 16,384 characters, and is the real caller-supplied task.
`root_prompt`, when supplied, is separately visible only to the root recursive node and has the
same validation; it is rejected when direct or `max_depth=0` execution would have no retained
root. Python completion prompts may be strings, mappings, or sequences; non-string
values are normalized to deterministic JSON text.

### 4.1 Path contract

- The context must resolve to one existing local directory.
- The state directory must not equal, contain, or be contained by the context directory.
- An output file must be outside both the context and state directory.
- An existing regular output file is atomically replaced after the run result is persisted.
- Symlink aliases are resolved before containment checks.

When no state directory is supplied, rcodex derives a stable context-keyed sibling path:

```text
<resolved-context-parent>/.rcodex-state/<context-name>-<path-sha256-first-12-hex>
```

For example, `/work/repo` uses `/work/.rcodex-state/repo-<digest>`. This keeps state outside the
context while preventing different context roots with the same name from sharing state.
`--state-dir` or `state_directory` overrides the derived path and remains subject to the overlap
rules above.

Controller state directories are created with owner-only `0700` permissions and state files,
event logs, session records, and lease files with `0600` permissions on POSIX. A new explicit
output file is also `0600`; atomically replacing an existing explicit output preserves that
file's mode and does not change its parent directory permissions. An existing explicit state
directory—and any other existing controller state directory—must already deny all group and
other access; otherwise rcodex rejects it. It does not repair an existing directory with
`chmod`.

### 4.2 Content-free manifest

Before Codex starts, the controller creates `context-manifest.json`. Each entry contains a
stable `file_000001` identifier, normalized relative path, byte count, SHA-256, media type, and
line count. It contains no file body. Initial construction and the final integrity rescan run in
a cancellable controller worker rather than on the asyncio event loop. The run deadline and a
cooperative cancellation signal apply while traversing and streaming files.

Manifest construction:

- includes regular UTF-8 source/text files with a supported extension, plus `Dockerfile`,
  `LICENSE`, and `Makefile`;
- skips NUL-containing or invalid-UTF-8 files, non-UTF-8 relative paths, symlinks, special files,
  unreadable files, and unsupported extensions;
- always skips `.git`, `.rcodex`, `.venv`, `venv`, `__pycache__`, and `node_modules` directories;
- applies repeatable case-sensitive `fnmatch` include/exclude patterns to POSIX relative paths;
  includes form an allowlist and excludes take precedence;
- sorts paths before assigning identifiers and fails when no supported file remains;
- enforces manifest-entry and aggregate-context-byte limits while streaming file hashes.

`include` and `exclude` each accept at most 64 non-blank patterns of at most 4,096 characters.

Codex receives the resolved context and manifest paths, not a prompt containing the corpus.
Include/exclude patterns control the manifest, evidence namespace, and integrity check; they do
not prevent the read-only Codex process from opening another file below the context root.

Before reporting success, rcodex rebuilds the manifest. Any addition, removal, or difference in
the recorded fields of the included supported-file set fails context integrity. Success
establishes exact equality between the start and end manifests; it does not provide snapshot
isolation during the run. A file may change and return to its original bytes between the two
scans, and Codex reads do not all observe one immutable filesystem version.

### 4.3 Final payload

Every successful leaf or recursive node returns schema version `1.0` with:

- `answer`: 1 to 100,000 characters;
- `evidence`: zero to 256 manifest-backed items;
- `uncertainties`: zero to 64 bounded strings.

An evidence item must pair an existing manifest ID with its exact relative path and a valid
one-based line range within that file. The controller checks direct-leaf raw UTF-8 output size
and every final payload's canonical strict JSON size. Recursive Codex response size is checked
separately as REPL code. Unknown fields, wrong types, invalid evidence, empty output, or oversized
output fail the boundary.

## 5. Execution strategies and REPL protocol

`RunStrategy` has exactly two values: `direct` and `recursive`.

### 5.1 Direct

Direct execution creates one depth-0 terminal leaf, performs one schema-constrained Codex turn,
validates its final payload, closes the SDK client, checks context integrity, and returns. It
does not provide a repair turn, custom controller tools, persistence, or delegation.

Selecting non-persistent `recursive` with `max_depth=0` also executes the root as a terminal
leaf. The result still records the requested recursive strategy and requested/executed node
modes. Persistent configuration with `max_depth=0` is rejected because there would be no
retained recursive root to resume.

### 5.2 Recursive node loop

A recursive node owns one retained Codex thread and performs up to `max_iterations` ordinary
REPL turns. Each Codex turn returns one or more fenced `repl` blocks. The controller extracts the
blocks and sends them to that node's restricted Python worker in order. The namespace survives
across the node's later turns, so a node can retain and transform results programmatically.

The injected interface is:

```python
llm_query(prompt, model=None) -> str
llm_query_batched(prompts, model=None) -> list[str]
rlm_query(prompt, model=None) -> str
rlm_query_batched(prompts, model=None) -> list[str]
SHOW_VARS() -> str
submit_answer(answer, evidence=None, uncertainties=None) -> None
answer  # RLM-compatible dictionary with content and ready keys
```

For example, the leaf response is available to the still-running Python block:

```repl
raw = llm_query("Return only the integer found in the parser configuration")
adjusted = int(raw) + 1
submit_answer(f"The adjusted value is {adjusted}.")
```

Each query function sends a hidden worker-to-controller request. The controller builds typed
internal requests, admits them under the call/node/depth/model limits, runs existing leaf or
recursive machinery, and returns answer strings to the worker. The batched variants run their
admitted calls concurrently and preserve input order. A rejected or failed query returns a safe
`Error (...)` string, which Python may inspect and handle. Custom controller tools are injected
as Python callables and return strict-JSON values in the same way.

The JSON-lines worker protocol, typed `CallResult` values, final payload validation, and JSON
artifacts are controller implementation boundaries; they are not a JSON programming language
that the root Codex must generate. The root programs with Python and signals completion by
calling `submit_answer(...)` or by assigning a strict result object to `answer["content"]` and
setting `answer["ready"] = True`.

Every retained recursive thread occupies a live-session slot for its whole node lifetime. Depth
zero has one slot; every deeper retained depth has `max_concurrency` slots. Same-depth calls queue
within their node/run deadlines instead of being rejected merely because ancestors retain
sessions. Parent Codex turns release their active-turn permit before their Python blocks run and
while children execute. The depth partition prevents a parent/child capacity cycle, so
`max_concurrency=1` still permits a recursive chain. Terminal leaves do not retain a session slot.

The maximum retained-session count is `1 + max_concurrency * (max_depth - 1)` when
`max_depth > 0`. Including concurrently active leaves, the published live Codex process upper
bound is the smaller of `max_total_nodes` and `1 + max_concurrency * max_depth`. Each logical
session currently owns a Codex process; high depth/concurrency settings must therefore be chosen
with host capacity in mind.

### 5.3 Depth semantics

Root depth is zero. `max_depth` is the depth at which recursive requests become terminal:

- a requested `leaf` always runs as a fresh terminal leaf;
- a requested `recursive` child runs recursively only when its depth is less than `max_depth`;
- when the new child depth is greater than or equal to `max_depth`, rcodex records
  `requested_mode="recursive"` and `executed_mode="leaf"` and runs one terminal leaf;
- when `max_depth=0`, the root itself is a leaf.

The conversion is observable in node and call records; it is not a rejection. Every delegated
Codex child receives fresh reasoning state. Recursive children are never aliases of the parent
thread.

### 5.4 Repair and forced finalization

An invalid or oversized recursive response consumes an iteration and produces a bounded repair
prompt containing an error code, a raw-output SHA-256, and validation counts rather than raw
invalid output. A response is invalid when it has no non-empty fenced REPL block or has more than
32 blocks. Calls do not execute when block extraction itself fails. A Python exception is captured
as bounded `stderr`; it does not crash the controller or erase the persistent namespace.

`max_errors`, when configured, counts consecutive invalid responses, REPL executions with stderr,
or iterations containing at least one unsuccessful call. An error-free iteration resets the
count. Reaching the threshold ends the node with an error. Completed stdout from the latest
execution is retained as a possible partial answer.

If all ordinary iterations are consumed, rcodex sends exactly one finalization turn with
queries and tools disabled. That turn must return a fenced REPL block that signals a final
payload. It is recorded at iteration index `max_iterations` with phase `finalization`.

### 5.5 Model routing and orchestration controls

The root uses `model` and `reasoning_effort`. Children default to `sub_model` and
`sub_reasoning_effort`, each falling back to the root setting. A query may request another model
only when it is admitted by `allowed_models`; configured root/sub models must themselves belong
to a non-empty allowlist. The allowlist accepts at most 64 unique non-blank names. Query functions
do not expose a per-call reasoning-effort parameter.

`custom_system_prompt` is installed as SDK developer guidance when a leaf or retained recursive
thread is opened. rcodex's own developer instructions remain authoritative: read-only operation,
untrusted context, leaf schema-only output, recursive fenced-REPL output, controller-mediated
queries, and the web, connector, MCP, escalation, and built-in-agent restrictions cannot be relaxed or overridden by caller
guidance. `user_prologue` is instead encoded as ordinary prompt data for every node.
`custom_system_prompt` and `user_prologue` must be non-blank and each fits both a 16,384-character
and 65,536-byte UTF-8 bound. `root_prompt` is ordinary root-only prompt data.
`orchestrator=False` tells the node not to query children and rejects any query function calls;
the local Python computation and registered custom tools remain available.

## 6. Batches, scheduling, and limits

Within a node, calls made by one batched query function are started together; separate scalar
calls execute synchronously in Python program order. Separate run-wide semaphores, each bounded
by `max_concurrency`, limit active Codex turns and active controller-tool calls. Waiting parents
hold neither permit. Per-depth retained-session quotas are described in section 5.2. A lock
serializes run-wide node reservation. Node IDs therefore express admission order, while child
completion order may differ. Batched returned strings and artifact call results preserve request
order.

`RecursiveRunner.run_batched` runs independent top-level tasks concurrently under a batch
semaphore and returns results in input order. `max_concurrency` bounds admitted top-level runs
in that batch, while each admitted run owns another semaphore with the same configured bound;
the setting is not a process-wide aggregate cap across all batched runs.
`RLM.completion_batched` and `AsyncRLM.completion_batched` expose recursive top-level batches.
All scheduled slots complete, remain in input order, and do not discard successful siblings. An
ordinary run failure remains a complete failed `RunResult`. A slot that fails during task
normalization or preparation, before a `RunResult` exists, is represented by an ordered
`BatchFailure` containing a bounded task preview, full task SHA-256 when a string exists, safe
controller error, and duration. Both forms project to an
`RLMChatCompletion` whose response is `Error: <safe controller message>` and whose attached
`run` preserves the complete `RunResult` or `BatchFailure`; the facade does not raise for either
failed slot. A projected `BatchFailure` has unavailable usage because no run ledger exists.
Invocation-wide validation may still reject the whole batch before scheduling.
Persistent configuration is rejected for top-level batches because the tasks cannot share one
retained root session. A batch accepts at most `max_batch_size` slots, and a bare string is not a
batch sequence.

### 6.1 Configuration defaults and bounds

| Setting | Default | Schema bound | Enforcement |
| --- | ---: | ---: | --- |
| `run_timeout_seconds` | 900 s | `>0` to 86,400 s | Hard monotonic run deadline |
| `cleanup_timeout_seconds` | 5 s | `>0` to 60 s | Hard SDK cleanup wait |
| `node_timeout_seconds` | 600 s | `>0` to 86,400 s | Hard recursive-node deadline, capped by run deadline |
| `leaf_timeout_seconds` | 300 s | `>0` to 86,400 s | Hard leaf deadline, capped by run deadline |
| `tool_timeout_seconds` | 30 s | `>0` to 3,600 s | Hard async tool wait, capped by node/run deadline |
| `max_manifest_entries` | 100,000 | 1 to 999,999 | Hard before Codex starts |
| `max_context_bytes` | 10 GiB | 1 B to 1 TiB | Hard while scanning |
| `max_final_result_bytes` | 128 KiB | 1 KiB to 1 MiB | Hard direct raw and all canonical final-payload bound |
| `max_repl_code_bytes` | 128 KiB | 1 KiB to 1 MiB | Hard raw recursive Codex-response bound |
| `max_repl_output_bytes` | 256 KiB | 1 KiB to 16 MiB | Hard per-stream capture and encoded feedback bound |
| `repl_memory_bytes` | 512 MiB | 64 MiB to 16 GiB | Linux worker address-space limit; unsupported on macOS |
| `repl_cpu_seconds` | 120 s | 1 to 86,400 s | Hard worker CPU limit |
| `max_tool_result_bytes` | 64 KiB | 256 B to 1 MiB | Hard JSON serialization bound |
| `max_depth` | 1 | 0 to 16 | Hard terminal-depth conversion |
| `max_iterations` | 30 | 1 to 100 | Hard ordinary turns plus one finalization turn |
| `max_calls_per_iteration` | 8 | 1 to 32 | Hard admitted RPC calls per REPL turn |
| `max_calls_per_node` | 64 | 1 to 1,024 | Hard cumulative admitted calls |
| `max_total_nodes` | 128 | 1 to 4,096 | Hard run-wide reservation |
| `max_concurrency` | 4 | 1 to 64 | Hard batch-run, per-depth session, active-turn, and tool caps |
| `max_batch_size` | 256 | 1 to 4,096 | Hard top-level Python batch admission |
| `max_errors` | unset | unset or 1 to 100 | Hard consecutive node-error threshold |
| `max_tokens` | unset | unset or at least 1 | Observed after completed SDK turns |
| `compaction_threshold` | 0.85 | 0.5 to 0.95 | Trigger ratio, not a usage limit |

An async custom tool is cancelled when its timeout expires. A synchronous handler is dispatched
through `asyncio.to_thread`, so it does not block the event loop; timing out stops waiting but
cannot forcibly terminate the underlying Python thread. A timed-out synchronous handler may
therefore continue running or producing side effects. Callers must register only bounded,
trusted synchronous work or an async handler with cooperative cancellation.

### 6.2 Published enforcement labels

Every `RunResult.runtime.limit_report` labels enforcement instead of implying unsupported
guarantees:

- `hard`: run/node/leaf timeouts, depth, iteration, REPL code/output/CPU, per-iteration calls,
  per-node calls, total nodes, batch size, concurrency, retained sessions, and the live-process
  upper bound;
- `observed`: `max_tokens`, checked from SDK usage after each completed turn, so one turn can
  overshoot;
- `unsupported`: `max_budget_usd`, because the SDK does not expose per-turn monetary cost, and
  `repl_memory_bytes` on macOS because that platform rejects a practical `RLIMIT_AS` below
  Python's initial virtual mappings.

The byte, manifest, tool, cleanup, and error limits in the preceding table are also controller
checks, although the compact published report currently lists only the named structural/time
limits. Token totals are deduplicated per thread. Persistent session state stores the latest
observed cumulative root-thread total, so a later process resumes from that baseline; ephemeral
child threads start fresh.

If any participating thread omits usage, aggregate `RunUsage` is marked unavailable rather than
presenting a partial total as complete. The controller still sums every known thread total as a
lower bound and raises the token limit once that known lower bound exceeds `max_tokens`. While
the lower bound remains below the limit, unknown usage prevents a complete observed total and
the report's observed value remains null. rcodex provides no hard dollar budget.

## 7. Persistent sessions and compaction

### 7.1 Session persistence

`persistent=True` is valid only for recursive execution. On the first completion, rcodex creates
`<state-dir>/sessions/<session-id>.json` and starts a non-ephemeral root Codex thread. The record
contains the exact resolved context root, root thread ID, latest observed cumulative root usage,
context/completion count, timestamps, and at most 1,000 history entries. Each entry records the
run ID, context version, status, a task preview of at most 2,000 characters plus its SHA-256, and
a final/best-partial answer preview of at most 4,000 characters plus its SHA-256. Full tasks and
answers remain in their individual run artifacts; the bounded session index is not a second full
history copy. The oldest index entry is dropped when the 1,001st completion is appended.

A later completion resumes the saved root thread through the SDK. It must use the same state
directory and exact resolved context root. An explicit unknown session ID fails; a session ID
without `persistent=True` fails. Recursive children remain fresh and ephemeral on every run.

When persistent configuration has no supplied ID, `AsyncRLM` (and therefore `RLM`) preallocates
its `session_<32-hex>` ID during construction and exposes it through `session_id`; its first
completion is the only operation authorized to create that missing record. Repeated
`completion()` calls reuse the ID. The CLI prints its created ID to stderr; later CLI invocations
pass it with `--persistent --session ID`.

One persistent session has one active owner. The Python facade rejects overlapping persistent
completions on the same object. Before preparing persistent state, the runner resolves or
allocates the session ID and acquires an exclusive
`<state-dir>/sessions/<session-id>.lock` lease for every persistent run, including an
automatically created session. A second process or runner that tries to own the same session
concurrently fails before creation or resume, preventing concurrent thread and session-record
updates. The lease is released after terminal persistence or cancellation.

The saved root thread is invalidated (its ID and usage baseline are cleared) when the end
manifest fails context integrity or any recursive-session cleanup is unsafe. The failed run may
remain in bounded history, but a later completion starts a fresh root thread rather than
resuming reasoning that may contain transient context or uncertain process state.

Persistence means conversation continuity across completed calls. It is not checkpoint/resume
for an unfinished run, a background daemon, or crash recovery. Each call gets a new run ID,
manifest, integrity check, trace, limits, and result. Direct completion and top-level batches do
not support persistent sessions. The Python namespace is deliberately not serialized: resuming a
persistent root thread starts a fresh REPL worker and the initial prompt discloses that boundary.

### 7.2 Compaction guarantee

Compaction is disabled by default. With `compaction=True`, rcodex checks after every valid
ordinary REPL turn. It also performs this final maintenance after the forced-finalization turn
only for the persistent root; a non-persistent node is about to close and is not compacted after
forced finalization. Each check computes:

```text
last turn input tokens / SDK-reported model context window
```

Using the just-completed recursive turn's usage, when data is available and the ratio is at
least `compaction_threshold`, rcodex invokes the
pinned SDK's `AsyncThread.compact()` within the smaller of the cleanup timeout and remaining
node/run time. This applies to retained recursive nodes, including recursive children; terminal
leaves are not compacted.

Through public SDK surfaces, rcodex first reads the thread with turns included and remembers the
existing completed `contextCompaction` item IDs, invokes `AsyncThread.compact()`, then polls
`AsyncThread.read(include_turns=True)` until a completed turn contains a new such item. Only then
does it set `IterationRecord.compaction_completed=true` and append the
`compaction.completed` event; `compaction.requested` precedes the request. Failure to observe the
new item within the bounded deadline fails compaction rather than reporting completion.

This confirms an App Server compaction item completed. It does not validate the generated
summary's content or promise that a later token count is smaller. If usage or context-window
data is unavailable, no request is made. App Server owns summary content and history rewriting.
A run-limit breach during compaction aborts the run. Any other compaction failure is recorded on
the iteration and as `compaction.failed`; the REPL loop may continue and an already validated
answer is not discarded.

## 8. Trusted tools, callbacks, and trace

### 8.1 Controller tools

Python callers may pass `custom_tools` for the root and `custom_sub_tools` for recursive child
nodes. When `custom_sub_tools` is omitted, children inherit the root mapping; passing an empty
mapping disables tools in children.

A custom value may be:

- a callable accepting one argument dictionary;
- an async callable accepting one argument dictionary;
- an immutable constant accepted by strict-JSON normalization; or
- `{"tool": value, "description": "..."}` to provide explicit model-facing guidance;
- a `rcodex.tools.ToolSpec` with an optional Pydantic `input_model` for strict argument
  validation and a generated model-facing JSON Schema.

Names must be Python-like identifiers, may not duplicate, and may not use reserved REPL
names such as `llm_query`, `rlm_query`, `delegate`, or `answer`. A registry accepts at most 64
tools and one encoded model-facing definition may not exceed 32 KiB. Model-authored arguments,
registered constants, and handler results all cross a recursive strict-JSON boundary: only null,
strings, booleans, integers, finite floating-point numbers, arrays, and string-keyed objects are
accepted, with nesting bounded to 64 levels. Strings and object keys must encode as UTF-8.
Cycles, non-finite numbers, non-string keys, tuples, bytes, sets, and arbitrary Python objects are
rejected. Results must also fit `max_tool_result_bytes`. Unknown tools, invalid input, timeouts,
serialization failures, and handler failures are normalized into failed call results and safe
error strings. Successful values return directly to the running Python block and are also
included in iteration artifacts.

These handlers are trusted host code executed inside the rcodex process. They are not sandboxed,
are not MCP, and may have the full authority of the caller. CLI runs cannot register tools.

### 8.2 Callbacks

`AsyncRLM`, `RLM`, and `RecursiveRunner` accept four synchronous observational callbacks:

```text
on_subcall_start(depth, model, task_prefix)
on_subcall_complete(depth, model, elapsed_seconds, error_or_none)
on_iteration_start(depth, iteration_index)
on_iteration_complete(depth, iteration_index, elapsed_seconds)
```

Subcall callbacks cover query-created child calls, not controller tools. Iteration callbacks
cover valid and invalid ordinary REPL iterations and the forced finalization phase. Callback exceptions
are swallowed so telemetry cannot change controller outcomes. Callbacks must be fast and
non-blocking.

### 8.3 Durable trace

Each run creates:

```text
<state-dir>/runs/<run-id>/
  request.json
  context-manifest.json
  nodes/
    node_000001.json
    ...
  iterations/
    node_000001/000.json
    ...
  result.json
  events.jsonl
```

Node records capture parent/call relationships, depth, requested/executed modes, model, status,
thread ID, usage, payload/partial answer, safe error, and the SHA-256 of that node's initial
prompt. Every REPL and finalization iteration records the SHA-256 of the exact prompt used,
executed code blocks, captured stdout/stderr, visible variable names and types, any typed final
payload, ordered call results, usage, timing, validation error, phase, and compaction-completion
flag.
`events.jsonl` is append-only, sequence-numbered, fsynced, and bounded to 16 KiB per event.
The request artifact stores hashes, rather than raw values, for system/prologue guidance and
root/sub tool definitions. Runtime metadata retains a compact prompt-template/hash summary, but
the node and iteration fields are the complete per-turn mapping. Invalid raw model output is not
stored; its hash and bounded validation counts are.

Artifacts do contain caller tasks, model answers, child tasks/results, and custom-tool values,
which may reproduce context data. rcodex enforces the POSIX owner-only modes in section 4.1; the
operator must still protect the account and state location accordingly.
`verbose=True`/`--verbose` additionally prints `[rcodex]` event summaries to stderr, leaving the
stdout result stream machine-readable.

## 9. Python API

The package exports `RLM`, `AsyncRLM`, `RunConfig`, `RLMChatCompletion`, `ToolRegistry`,
`ToolSpec`, and typed completion errors.

Synchronous use:

```python
from pathlib import Path

from rcodex import RLM, RunConfig

config = RunConfig(
    model=None,
    sub_model=None,
    max_depth=2,
    max_concurrency=4,
    compaction=True,
)

with RLM(
    context=Path("."),
    state_directory=Path("../rcodex-state"),
    config=config,
) as rlm:
    result = rlm.completion("Review the architecture and cite the key files.")
    print(result.response)
    print(result.usage_summary)
    print(result.run)
```

Async use with a root-only tool:

```python
from pathlib import Path

from rcodex import AsyncRLM, RunConfig


async def policy_lookup(arguments: dict[str, object]) -> dict[str, object]:
    return {"topic": arguments.get("topic"), "status": "approved"}


async with AsyncRLM(
    context=Path("."),
    state_directory=Path("../rcodex-state"),
    config=RunConfig(max_depth=2),
    custom_tools={
        "policy_lookup": {
            "tool": policy_lookup,
            "description": "Look up one approved local policy topic.",
        }
    },
    custom_sub_tools={},
) as rlm:
    completion = await rlm.completion("Review policy compliance.")
```

Operations:

- `completion(prompt, root_prompt=None)`: recursive completion;
- `direct_completion(prompt)`: one terminal leaf, rejected for persistent config;
- `completion_batched(prompts)`: ordered independent recursive completions, rejected for
  persistent config;
- `close()`: prevents further calls on that facade.

`RLMChatCompletion` exposes the original prompt, answer string, usage summary, execution time in
seconds, and its attached `RunResult` or batch-only `BatchFailure`; `to_dict()` produces a
JSON-ready projection. When a facade single completion has no final payload, it raises
`TimeoutExceededError`,
`TokenLimitExceededError`, `ErrorThresholdExceededError`, or the base `RLMError`. Each carries
the strict controller error and any best partial answer. rcodex exports no separate cancellation
exception; caller cancellation is persisted and then re-raised as `asyncio.CancelledError`.
Batched completion instead materializes failed slots with an
`Error: ...` response as described in section 6. Lower-level `RecursiveRunner.run()` returns the
terminal `RunResult` for ordinary failures; caller cancellation is persisted and then re-raised.

## 10. CLI

The only command is `rcodex run`. `--task` and `--context` are required;
`--strategy recursive` is the default and `--strategy direct` selects one leaf.

```bash
uv run rcodex run \
  --task "Compare the implementation with its specification" \
  --context . \
  --strategy recursive \
  --max-depth 2 \
  --max-iterations 20 \
  --max-calls-per-iteration 6 \
  --max-calls-per-node 40 \
  --max-total-nodes 80 \
  --max-concurrency 4 \
  --state-dir ../rcodex-state \
  --output ../result.json
```

CLI options expose root/sub models and reasoning, a repeatable model allowlist, all single-run
limits,
persistence/session ID, compaction and threshold, orchestration guidance, root/system/prologue
prompts, include/exclude patterns, verbosity, state path, and output path. Durations accept bare
seconds or `s`, `m`, and `h` suffixes and may not exceed 24 hours.

Omitting `--state-dir` uses the context-keyed sibling state path from section 4.1.

The ambient-MCP warning and persistent-session ID go to stderr. The complete pretty JSON result
goes to stdout unless `--output` is supplied; then the result is atomically written to that file
and a confirmation goes to stderr.

Exit codes:

| Outcome | Code |
| --- | ---: |
| Succeeded | 0 |
| Failed/runtime error | 1 |
| Invalid invocation/configuration | 2 |
| Partial | 3 |
| Timed out | 124 |
| Cancelled/keyboard interrupt | 130 |

## 11. Result lifecycle and failures

`RunResult.status` is one of `succeeded`, `partial`, `failed`, `timed_out`, or `cancelled`.

- `succeeded` requires a validated payload and forbids a run error.
- `partial` requires a safe error and either a payload or recorded best partial answer.
- every other status requires a safe error and forbids a payload.

The controller owns status, IDs, timestamps, retry classification, paths, usage, runtime
metadata, limits, and capability claims. The model owns REPL code, query prompts, final content,
evidence, uncertainties, and tool arguments.

Failures use bounded codes covering authentication, SDK startup/runtime, timeout, cancellation,
model output, context integrity, manifest/storage/cleanup, node/call/iteration/token/error limits,
tools, unsupported requests, and unexpected controller errors. SDK runtime failures are marked
transient where appropriate; model prose cannot set retryability. Unexpected exceptions persist
only their type name, not an uncontrolled message or traceback.

Cancellation interrupts an active Codex turn where possible, signals an active manifest worker,
closes SDK clients within the cleanup boundary, writes node/run terminal state and events, then
propagates cancellation to the caller.

Every Codex cleanup failure is global, including failure to close a recursive child session. The
run cannot report success when process termination is unconfirmed: its terminal error is
`RunErrorCode.cleanup`, the affected node terminates with cleanup failure, and a persistent
resume thread is invalidated. If another turn, node, or limit failure happened first, that
earlier error remains where already written in the iteration or call trace while the terminal
node and run still surface cleanup; a retained-session close failure is also recorded as
`node.cleanup_failed` and is never swallowed.

## 12. MCP and trust boundary

rcodex has no positive MCP support. It neither exposes its query protocol as an MCP server nor
intentionally invokes an MCP tool. Recursive work and custom tools use the restricted REPL and
hidden controller RPC in section 5. Positive MCP integration remains deferred backlog and is not
part of this implemented contract.

The process and every thread request an empty MCP mapping and disable unrelated tool classes,
but rcodex cannot prove that user, project, or host MCP servers were not inherited by Codex.
Ambient server tools may remain model-visible, receive task or context data, or perform external
actions with configured credentials. A read-only filesystem sandbox and prompt instruction do
not constrain those remote side effects.

After invocation/path validation and before run state or a Codex client is created, every run
prints a non-interactive warning. There is no acknowledgement flag, unsafe override, or claim of
effective isolation. Continuing makes the operator responsible for reviewing or isolating the
ambient Codex MCP configuration. With the exact built-in adapter, runtime metadata uses
`adapter_kind="pinned-sdk"` and records requested configuration only:
`mcp_requested_disabled=true` and analogous requested-disable fields do not assert that the host
honored isolation. A caller-injected adapter is marked `adapter_kind="custom"` with
`requested_capabilities=null`; rcodex does not attribute the pinned adapter's requests to unknown
code.

Context files are always untrusted data, not instructions. Codex is told not to write, delegate
through built-in agents, browse, use connectors/MCP, or request escalation. The controller alone
writes state/output artifacts outside the context. Model-authored Python runs in a separate
`python -I -S` worker with a scrubbed environment, private temporary working directory,
restricted AST and builtins, no imports or file APIs, bounded output,
process/file-descriptor/CPU limits, and an address-space limit on Linux. This is defense in
depth, not a general hostile-code container; macOS does not enforce the configured address-space
cap. Trusted Python tools are an explicit caller extension of this boundary and may perform
actions according to their handler authority.

## 13. Verification and non-goals

Offline conformance checks:

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

Live tests are opt-in because they use an authenticated Codex account and consume usage. Test
fixtures may be generated for isolated verification, but ordinary `rcodex run` and Python API
calls operate only on caller-provided tasks and context.

```bash
RCODEX_LIVE_TESTS=1 uv run pytest -m live
```

The default pytest configuration excludes the `live` marker, and selected live tests still skip
unless `RCODEX_LIVE_TESTS=1`. The explicit command above crosses both gates.

The current contract does not claim:

- effective zero-MCP startup on an arbitrarily configured host;
- a hard dollar budget or pre-admission token reservation;
- provider-neutral backends or upstream container environments;
- a portable hostile-code sandbox or a persisted Python namespace across invocations;
- an interactive trace visualizer, hosted service, distributed workers, or background daemon;
- unfinished-run checkpoint recovery;
- training, fine-tuning, reward generation, or any training dependency.
