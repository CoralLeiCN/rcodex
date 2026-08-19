# Recursive Codex Deferred Features

Status: post-core backlog
Date: 2026-08-19

## 1. Purpose

This document contains features intentionally excluded from the [Core Spec](core-spec.md).

Deferred does not mean rejected. It means the feature is unnecessary to validate the first two-level Recursive Codex workflow, adds a significant dependency or failure mode, or needs evidence from core evaluations before its design can be finalized.

The complete design intent is documented in [Recursive Codex Runtime](recursive-codex-spec.md). This backlog determines when those capabilities should be implemented.

The [Technology Stack Decision](tech-stack.md) controls which dependencies and infrastructure may enter each phase.

## 2. Priority definitions

- **P1 — Next architecture:** required to move from fixed depth-1 decomposition to a genuinely model-directed recursive runtime.
- **P2 — Production hardening:** required before unattended, durable, or security-sensitive use at scale.
- **P3 — Product expansion:** useful after the runtime and evaluations are stable.
- **Research:** implement only after a separate experiment or product requirement justifies it.

No deferred feature enters core by convenience. Its entry criteria and new acceptance tests must be added first.

## 3. P1 — Dynamic recursive runtime

### 3.1 MCP delegation gateway

Add `delegate` and `delegate_batch` tools that a Codex node may call during its own turn.

Why deferred:

- Core can validate decomposition using controller-mediated phases.
- Per-node MCP configuration and caller binding need a capability spike.
- Blocking tool calls that launch other Codex turns add cancellation and process-lifecycle complexity.

Entry criteria:

- Two-level core beats or complements the direct baseline on a representative workload.
- The pinned Codex runtime supports isolated node-specific MCP configuration.
- A pre-bound stdio gateway identity can be established without exposing reusable credentials to the model or shell.

Required tests:

- The model cannot forge parent identity, depth, or remaining budget.
- Duplicate tool delivery creates at most one child.
- Batch results remain ordered with mixed success and failure.
- A parent can inspect one child result and make a later delegation whose task depends on that result.
- `mode="leaf"` never exposes delegation, while `mode="recursive"` exposes it only below the terminal depth.
- Cancelling a parent cancels a blocked delegation and its descendants.
- A parent waiting on a child never holds the scheduler permit that child needs, including with `max_concurrency=1`.

### 3.2 Arbitrary bounded depth

Allow recursive children to receive the delegation tools until `max_depth`.

Why deferred:

- Depth greater than one is not needed to validate external context plus focused Codex children.
- Deeper trees amplify token use, latency, cancellation, and partial-failure behavior.

Dependencies:

- MCP delegation gateway.
- Transactional run-wide ledger.
- Recursive cancellation.

Promotion rule:

Start with `max_depth=2`. Do not expose “unlimited” depth.

### 3.3 Transactional budget ledger

Replace core's in-memory counters with SQLite-backed reservations for:

- total nodes;
- children per node;
- active nodes;
- depth;
- wall-clock allowances;
- result bytes;
- artifact bytes per node and per run;
- optional input/output tokens and cost.

Why deferred:

- Core has one controller and one child layer, so semaphores and validated plan size are sufficient.
- Transactional reservations become necessary only when multiple descendants can request children concurrently.

Required properties:

- admission and reservation in one transaction;
- lifecycle changes and their event-outbox records commit in the same transaction;
- no branch receives a private copy of remaining budget;
- idempotent reconciliation after retry;
- explicit distinction between hard and observed usage limits.

### 3.4 Recursive cancellation and cleanup

Add tree-wide interrupt, grace periods, process reaping, and terminal-state reconciliation.

Why deferred:

- Core cancels one known leaf task set.
- Dynamic descendants require discovery and causality tracking.

Acceptance criterion:

A terminal run has no active descendant turn, MCP process, or node-owned command process.

### 3.5 Compact child-result artifacts

Store oversized child output as artifacts and return summaries plus references through the delegation tool.

Why deferred:

- Core enforces a simple byte limit before synthesis.
- Dynamic parents need an artifact protocol accessible from inside a turn.

## 4. P2 — Production hardening

### 4.1 Advanced Python SDK lifecycle integration

Adopt advanced lifecycle operations as they become available through the pinned Python SDK:

- streamed item events;
- incremental usage;
- explicit turn interrupt;
- thread fork, compact, archive, and descendant inspection;
- approval event handling.

Why deferred:

- Core needs only thread start, continuation, result capture, and bounded cleanup.
- Advanced lifecycle behavior varies with the Python SDK release.

Requirements:

- use `AsyncCodex` and Python SDK thread objects;
- pin the Python SDK exactly and use its bundled runtime;
- isolate SDK-specific handling behind `CodexAdapter`;
- keep an advanced feature deferred when the SDK does not expose the required operation.

The project does not implement a raw App Server JSON-RPC client under this backlog item. Doing so requires an explicit revision to the SDK-only decision.

### 4.2 Durable resume and crash recovery

Add persisted thread IDs, node reconciliation, idempotent child creation, and resume commands.

Why deferred:

- Core runs are short and explicitly non-resumable.
- Reliable resume depends on durable node state and deeper lifecycle APIs.

Possible user interface:

```bash
rcodex inspect <run-id>
rcodex resume <run-id>
rcodex cancel <run-id>
```

### 4.3 Queryable event projection

After P1 introduces the SQLite ledger and transactional event outbox, add query-oriented projections for usage, artifacts, traces, and reporting. Keep JSONL as the portable audit export.

Why deferred:

- Core artifacts are inspectable as files.
- A database becomes useful with large recursive trees, resume, and a UI.

Acceptance criterion:

Query projections can be deleted and rebuilt from committed events without changing authoritative live ledger state.

### 4.4 Codex thread compaction

Trigger and observe thread compaction for long-lived recursive nodes.

Why deferred:

- Core uses one planning turn and one synthesis turn on the root.
- Leaf nodes use one turn.

Compaction must preserve the root objective, constraints, evidence references, child status, and remaining budget.

### 4.5 Hard or observed token and cost budgets

Add aggregate usage limits after measuring which fields the pinned runtime reports and when.

Why deferred:

- Core records usage but uses hard structural and time limits only.
- A final response may overshoot a token or cost threshold before the controller can interrupt it.

Requirement:

The UI and result schema must label each limit `hard`, `observed`, or `unsupported`.

### 4.6 Retention, redaction, and privacy policy

Add configurable storage of prompts, outputs, command metadata, tool payloads, and usage.

Why deferred:

- Core stores only bounded results and may retain bounded invalid raw output when diagnostic retention is explicitly enabled.
- Full event capture can contain source code, personal data, or secrets.

### 4.7 External execution isolation

Support a container or restricted worker boundary outside the Codex filesystem sandbox.

Why deferred:

- Core is read-only and local.
- Security-sensitive, networked, or untrusted workloads may require stronger containment than local sandbox presets.

## 5. P3 — Product expansion

### 5.1 Write-capable coding workflows

Add per-node scratch directories or Git worktrees, change artifacts, review, and an explicit merge step.

Why deferred:

- Parallel writers introduce conflicts and a materially larger safety surface.
- The RLM-style long-context analysis hypothesis can be tested read-only.

Never allow parallel nodes to edit the same checkout.

### 5.2 Built-in Codex subagent adapter

Add `native-subagents` as an experimental execution strategy using Codex's own subagent tools and descendant threads.

Why deferred:

- It is more native to the interactive Codex experience but gives rcodex less direct admission control.
- Concurrency limits alone do not guarantee depth, total descendants, or aggregate budget.

Promotion rule:

Expose it only when the runtime can accurately observe descendants and clearly label limits that are best-effort.

### 5.3 Multiple context roots

Add single-file convenience and allow several directories, files, or mounted datasets in one context pack.

Why deferred:

- One resolved root covers the first repository and corpus evaluations.
- Multiple roots complicate naming, containment, trust, and duplicate content.

### 5.4 Content-addressed context store

Deduplicate immutable inputs by hash and reuse manifests across runs.

Why deferred:

- Core can scan one local root without copying bodies.
- A blob store matters when datasets are reused, remote, or expensive to prepare.

### 5.5 More document formats

Add explicit extraction adapters for PDF, DOCX, PPTX, spreadsheets, images, and archives.

Why deferred:

- Text, source, JSON, and line-oriented tables are enough for core evaluations.
- Extraction introduces separate libraries, layout fidelity issues, and untrusted parser risk.

Each adapter must preserve source-location mapping for evidence.

### 5.6 Web, connectors, and task-specific tools

Allow selected nodes to use web search, MCP services, or connected applications.

Why deferred:

- Core should isolate the effect of recursion over a fixed local corpus.
- External tools add mutable state, permissions, nondeterminism, and separate network policies.

### 5.7 Service API and daemon

Add background execution, HTTP API, queueing, authentication, and multi-user controls.

Why deferred:

- Core is a local CLI and library.
- A hosted service changes the threat, tenancy, retention, and operations model.

### 5.8 Trace viewer

Render the run tree, turns, usage, artifacts, and failures in a local UI.

Why deferred:

- JSON artifacts and JSONL are sufficient to debug the first implementation.
- The event model should stabilize before a UI depends on it.

### 5.9 Deterministic partitioned map/reduce

Add a `partitioned-map-reduce` strategy that partitions a context pack by a deterministic policy instead of asking a root model to plan subtasks.

Why deferred:

- Core must compare direct Codex with the RLM-inspired, model-planned `two-level` topology.
- A deterministic partitioner is workload-specific and should be justified by evaluation evidence, such as poor coverage or unstable plans.

Promotion rule:

Define the partition policy, coverage metric, and synthesis contract before adding the strategy to the public CLI.

### 5.10 Reusable multi-run sessions

Allow a caller to retain a named Codex session across separate rcodex `run()` calls, add versioned context inputs, and inspect prior validated results.

Why deferred:

- RLM `persistent=True` reuses an environment across separate completions, but core persistence is artifact storage and the P2 resume design concerns recovery of one run.
- Cross-run sessions need explicit retention, authorization, compaction, context-versioning, and invalidation rules.

Promotion rule:

Define whether session state consists of Codex thread history, rcodex artifacts, versioned context packs, or all three. Never label crash resume as multi-call persistence.

### 5.11 Per-node model and tool profiles

Allow an admitted child to select from controller-approved Codex model, reasoning-effort, and tool profiles.

Why deferred:

- RLM supports root/depth-1 backend routing, model overrides, and different custom root/subcall tools.
- Core must use comparable model and tool settings so decomposition evaluations are interpretable.
- Dynamic profiles require allowlists, usage accounting, capability binding, and evaluation of routing policy.

Promotion rule:

The model may select only a named controller-defined profile. It may not supply arbitrary provider credentials, MCP configuration, or permission expansion.

## 6. Research-only features

### 6.1 Learned recursive policies

Train or fine-tune models to decompose, delegate, and synthesize more effectively.

Prerequisite:

A stable runtime and evaluation suite must first produce trustworthy trajectories and rewards.

### 6.2 Continual harness self-improvement

Allow the system to propose and persist prompt, skill, or delegation-policy changes from prior runs.

Risk:

Self-modifying instructions make regressions, provenance, and rollback substantially harder. Treat proposed changes as reviewable artifacts, never automatic mutations of immutable base policy.

### 6.3 Distributed and remote workers

Run child Codex nodes on multiple machines or remote sandboxes.

Prerequisite:

Local scheduling, capability binding, idempotency, artifact transfer, and cancellation must already be correct.

### 6.4 Alternative orchestration surfaces

Evaluate Codex-as-MCP under the Agents SDK or hosted multi-agent APIs for broader workflows.

This is not the default rcodex architecture because it may introduce another reasoning agent above Codex. Use it only when the broader workflow requires non-coding specialists or hosted orchestration.

## 7. Feature allocation summary

| Capability | Core | Deferred priority |
| --- | :---: | --- |
| Direct Codex baseline | Yes | — |
| External context manifest | Yes | Expand in P3 |
| Fixed root → leaves → synthesis | Yes | — |
| One child layer | Yes | Arbitrary depth P1 |
| Bounded leaf concurrency | Yes | Tree-wide scheduler P1 |
| JSON file artifacts and JSONL | Yes | SQLite ledger P1; query/UI P2–P3 |
| Read-only sandbox | Yes | Write worktrees P3 |
| Model-directed MCP delegation | No | P1 |
| Transactional aggregate budgets | No | P1 |
| Durable resume | No | P2 |
| Thread compaction | No | P2 |
| Hard token/cost budget | No | P2, subject to runtime support |
| Built-in Codex subagents | No | P3 experimental |
| Deterministic partitioned map/reduce | No | P3, evaluation-driven |
| Multiple roots and binary documents | No | P3 |
| Reusable sessions across runs | No | P3 |
| Per-node model/tool profiles | No | P3 |
| Web, connectors, remote workers | No | P3/Research |
| Training and self-improvement | No | Research |

## 8. Backlog governance

For every deferred feature:

1. Link the evaluation or user requirement that justifies it.
2. Define its security and lifecycle impact.
3. Add acceptance tests before implementation.
4. State whether it changes the CLI or persisted schema.
5. Preserve compatibility with completed core run artifacts where practical.
6. Update the [reference catalog](../references/README.md) when new external design evidence is used.

The preferred next step after core is P1 dynamic delegation—not a UI, hosted service, or broad document support.
