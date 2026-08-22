# Recursive Codex Specification Review

Status: reviewed and source-aligned with documented deviations
Date: 2026-08-19

## 1. Review scope

This review covers:

- [Core Spec](core-spec.md);
- [Deferred Features](deferred-features.md);
- [Full Target Architecture](recursive-codex-spec.md);
- [Design Rationale](design-rationale.md);
- [RLM Implementation Alignment](rlm-alignment.md);
- [Technology Stack Decision](tech-stack.md);
- the source-only [`references/`](../references/README.md) catalog.

The review checks scope separation, terminology, SDK assumptions, concurrency, validation, persistence, security, reproducibility, testing, and evaluation gates.

For RLM behavior, the executable baseline is `alexzhang13/rlm` commit `caf0bffa1acec17c062559433b4cd4ed92eee3d6`, verified as the repository's `main` head on the review date. The paper supplies the paradigm; the pinned code supplies implementation semantics.

## 2. Outcome

The specification set is aligned around one delivery model:

```text
C0 SDK capability spike
  -> C1 direct baseline
  -> C2 fixed two-level planned decomposition
  -> C3 comparative evaluation gate
  -> P1 controlled dynamic recursion
  -> P2 production hardening
  -> P3 product expansion
```

Core remains deliberately small: one context directory, read-only Codex threads, `direct` and `two-level` strategies, bounded concurrency, strict schemas, JSON/JSONL artifacts, and no MCP server, database, daemon, UI, or write workflow.

The source-alignment audit also establishes that core is RLM-inspired but not behaviorally equivalent. The target `recursive` strategy is the semantic analogue because it permits context inspection, on-demand direct or recursive child calls, result inspection, and further delegation before finalization.

## 3. Resolved alignment issues

| Issue found | Resolution |
| --- | --- |
| Core called its first multi-node strategy `two-level`, while the full spec called it deterministic `map-reduce`. | `two-level` is now canonical. It uses a model-generated validated plan. Deterministic `partitioned-map-reduce` is explicitly deferred and evaluation-driven. |
| Stack choices were duplicated across specifications and could drift. | [Technology Stack Decision](tech-stack.md) is authoritative for implementation choices; feature scope remains in core/deferred specs. |
| App Server capabilities could be mistaken for Python SDK capabilities. | C0 must prove behavior through the exactly pinned Python SDK. Features exposed only elsewhere remain deferred. |
| The local read-only sandbox was described too much like a confidentiality boundary. | The specs now distinguish write prevention from host-read isolation and require external isolation for confidential workloads. |
| JSONL and SQLite were both described as authoritative while lifecycle transitions were claimed to be atomic. | Core stays file-only. P1 uses SQLite as the live transactional ledger with an outbox; JSONL is an idempotent portable audit export. |
| A recursive parent could wait for a child while holding the only concurrency permit. | The target scheduler distinguishes runnable from open nodes and requires a parent to release its runnable permit before queuing a child. P1 must test the `max_concurrency=1` case. |
| Persisted contracts lacked a consistent evolution rule. | JSON artifacts, JSONL events, and prompts now carry explicit schema/template versions; run metadata records runtime and model configuration. |
| Model output requested an `analysis_summary`. | The core plan uses a concise `coverage_summary`, avoiding a requirement for private chain-of-thought. |
| Oversized child output could silently create an unplanned summarizer node. | Core rejects an oversized leaf result. The full recursive target may store accepted payloads as artifacts and return deterministic bounded markers, but never creates an unplanned summarizer. |
| Core accepted a file or directory while also treating the input as the Codex working directory. | Core now accepts one directory. Single-file convenience and multiple roots are deferred. |
| Run artifacts could be stored inside the context and then read as input by Codex. | Core requires resolved state and output paths outside the context root and rejects overlap before starting Codex. |
| Evaluation promotion could rest on one anecdotal run. | Metrics and thresholds must be declared before evaluation, configurations must match, and repeated trials are required when nondeterminism could change the conclusion. |
| The core depth-1 wording could imply parity with RLM. | Core now claims topology-only similarity. RLM's iterative inspect/delegate loop is required only in the target `recursive` strategy. |
| `llm_query` and `rlm_query` were mapped without stating their different source semantics. | The mapping now distinguishes a plain one-shot RLM call from a recursively capable child environment and notes that a Codex leaf is intentionally richer than `llm_query`. |
| rcodex and RLM used different terminal-depth rules without a direct comparison. | The specs now state that RLM falls back to a plain LM, while rcodex admits a delegation-disabled terminal Codex node and reports denied requests explicitly. |
| RLM persistence was mapped to resume/compaction even though they solve different problems. | Multi-call environment reuse is now a distinct deferred feature; resume and compaction remain within-run lifecycle features. |
| The RLM limit description could imply one transactional token pool. | The source review now distinguishes remaining timeout/dollar-budget propagation from a copied child token cap and post-iteration checks. rcodex's run-wide ledger is an intentional strengthening. |

## 4. Best-practice checks now required

### API and schema boundaries

- Strict Pydantic v2 models reject unknown fields.
- Raw and serialized byte limits are enforced.
- Context references are validated but explicitly treated as hints, not permissions.
- Lifecycle status is controller-owned and never parsed from free-form prose.

### Concurrency and cancellation

- Python 3.11 `TaskGroup`, semaphores, and monotonic deadlines are the default primitives.
- Expected leaf failures become structured results so siblings continue.
- Unexpected controller failures cancel the task group.
- Cancellation cleanup re-raises `CancelledError` and verifies process exit.

### Persistence and reproducibility

- Core JSON writes are atomic and JSONL state transitions are flushed.
- The exact Codex SDK and complete dependency graph are locked.
- Run metadata includes software, runtime, model, prompt, sandbox, and effective-limit information.
- Offline fake-adapter tests are separate from authenticated live SDK tests.

### Security

- Core is read-only and delegation-disabled. Failure to disable built-in delegation or unrelated MCP tools blocks release; weaker host-read or network isolation is disclosed and requires an external boundary for sensitive workloads.
- No ambient credentials are stored in prompts or artifacts.
- Context is untrusted data, and prompt instructions are not treated as the security boundary.
- Unsupported isolation is disclosed in run metadata rather than implied.

## 5. Remaining C0 decisions

These are intentionally unresolved until tested against the pinned SDK:

1. exact `openai-codex` release to pin;
2. safe concurrent sharing model for `AsyncCodex`;
3. exact per-thread configuration for disabling unrelated subagents, MCP servers, connectors, and web access;
4. interruption and cleanup behavior on deadline and `SIGINT`;
5. token-usage fields and whether any limit can be enforced before overshoot;
6. direct structured-output support versus prompt-plus-Pydantic validation;
7. exposed bundled-runtime version and other reproducibility metadata.

An unsupported capability is a reason to defer it, block release when it protects a hard structural invariant, or weaken-and-disclose only where the specification explicitly permits that mode. It is never a reason to bypass the SDK silently.

## 6. Review conclusion

The documents are ready to guide the C0 capability spike. They are aligned with the pinned RLM implementation through an explicit preserve/adapt/defer/exclude matrix, not through a source-compatibility claim. They intentionally do not claim that core already provides RLM's dynamic iterative delegation, multi-call persistence, hard token budgets, durable resume, or confidential host isolation.

Review the specification set again after C0 records actual SDK behavior and before C2 freezes public schemas.
