# Recursive Codex Design Rationale

Status: supporting rationale
Date: 2026-08-19

## 1. Purpose

This document contains rcodex interpretations, inferences, comparisons, and design reasoning derived from the source catalog. The [`references/`](../references/README.md) folder remains limited to source metadata, direct links, revision information, and descriptions of source contents.

The normative implementation requirements remain in the [Core Spec](core-spec.md), [Deferred Features](deferred-features.md), and [Full Target Architecture](recursive-codex-spec.md).

The detailed source-to-spec comparison is in [RLM Implementation Alignment](rlm-alignment.md).

## 2. Evidence-to-decision map

| Design area | Source evidence | rcodex reasoning and decision |
| --- | --- | --- |
| External context | The [RLM paper and implementation](../references/rlm.md) keep large prompts in an environment and inspect them programmatically. | The essential abstraction is externalized context plus focused subproblems, recursive calls, and synthesis—not the Python REPL itself. Store a manifest and paths outside model context, then use native Codex file and shell tools for inspection. |
| Depth-1 minimum | The main RLM implementation defaults to root depth `0` and `max_depth=1`, where terminal subcalls are plain model completions; [RLM Minimal](../references/rlm.md#minimal-implementation) also demonstrates one subcall layer. | Core uses a fixed root planning turn, one Codex leaf layer, and root synthesis. This preserves the topology only. A Codex leaf is an agent, and the root cannot interleave later child waves, so core is not RLM control-flow parity. |
| Recursive children | The [RLM reference implementation](../references/rlm.md#reference-implementation) creates child RLMs with their own environments, passes remaining timeout and calculated dollar budget, and copies the token cap. | The full target creates child Codex threads governed by one controller-owned, run-wide ledger. This is stronger than copying branch-local limits and avoids claiming that RLM itself provides transactional aggregate token accounting. |
| Parallel batch work | RLM exposes bounded batched calls, while the [Codex sources](../references/codex.md) document async orchestration and native subagents. | Core owns leaf concurrency, verifies concurrent independent SDK threads in C0, and preserves deterministic plan order. Model-directed concurrency is deferred until the same limits can be enforced before child admission. |
| Agent runtime | The [Codex SDK](../references/codex.md#codex-sdk) supports async Python orchestration, starting and continuing threads, and sandbox presets. | Use the stable `openai-codex` Python package, `AsyncCodex`, an exactly pinned release, its bundled runtime, and `Sandbox.read_only` in core. Do not use the TypeScript SDK, a CLI subprocess as the agent runtime, or a raw App Server client. |
| Deep lifecycle | [Codex App Server](../references/codex.md#codex-app-server) documents lifecycle, interrupt, history, fork, compaction, and usage primitives; the Python SDK controls App Server internally. | Use only lifecycle features exposed through the pinned Python SDK. Keep unavailable capabilities deferred unless the integration decision is explicitly revised. |
| Dynamic delegation | The Codex sources document both [Codex as an MCP server](../references/codex.md#codex-as-an-mcp-server) and [native subagents](../references/codex.md#codex-subagents). | The target design gives Codex nodes a constrained rcodex delegation MCP gateway. Codex-as-MCP is not the core design because it introduces a separate orchestration layer above Codex. Built-in subagents remain experimental until depth, node, budget, and admission limits can be enforced. |
| Security | The [RLM repository](../references/rlm.md#reference-implementation) and [Prime Agent](../references/related-implementations.md#prime-agent) describe local model-generated execution and warn about its trust boundary. Codex documents sandbox presets. | Do not reproduce host-process Python `exec`. Core is read-only. Security-sensitive deployments need explicit external isolation if the pinned runtime cannot enforce the required boundary. |
| Usage accounting | RLM and [DSPy.RLM](../references/related-implementations.md#dspyrlm) track composed usage, and App Server documents token-usage events. | Record usage exposed by the Python SDK in core. Defer hard aggregate token and cost enforcement until timing and completeness are verified. |
| Depth terminal behavior | RLM silently falls back to a plain model completion at its maximum depth. | rcodex keeps the terminal node as a delegation-disabled Codex agent and reports denied recursive requests explicitly. This changes behavior intentionally so the caller can distinguish a requested recursive child from an unavailable mode. |
| Child context | An RLM recursive child receives the parent-supplied subcall prompt as its new environment context. | Core and the first recursive target give children a bounded task and references but allow them to inspect the full authorized read-only context pack. This is broader than RLM and is documented; future scoped context views may narrow access if evaluation or security requires it. |
| Persistence and UI | RLM can reuse an environment across separate completions and includes compaction and visualization. | Use JSON artifacts and JSONL events in core. Crash resume and thread compaction do not equal RLM multi-call sessions; reusable sessions, query projections, a daemon, and a trace UI remain later work. |
| Public API | DSPy.RLM presents recursive execution behind a regular module interface. | Keep a stable task/context/result boundary independent of the internal number of Codex threads. |
| Advanced autonomy | Prime Agent includes persistence, background execution, self-improvement, and direct agent communication. | These capabilities can be valuable, but they expand lifecycle and security requirements and are not required to validate Recursive Codex. |

## 3. Minimal-version reasoning

The first implementation needs to test one claim: externalized context plus bounded Codex decomposition can outperform a comparable direct Codex run on representative large-context tasks at an acceptable latency and usage cost.

A fixed two-level workflow is sufficient for that test:

1. A root Codex turn creates a validated plan.
2. A controller launches a bounded number of independent leaf Codex threads.
3. The original root thread synthesizes size-limited leaf results.

Arbitrary recursion, MCP delegation, durable resumption, a database, write-capable worktrees, and a UI do not materially improve this first validation. Adding them before the evaluation would make failures harder to attribute and increase the security and lifecycle surface.

## 4. Interpretation rule

A source showing that a capability exists does not prove that it belongs in the core product. A capability enters core only when it is necessary to validate the minimal product and has a clear security, lifecycle, and test contract.

When future research changes a design decision:

1. add or update the source-only entry under [`references/`](../references/README.md);
2. record the interpretation and decision here or in the relevant specification;
3. link the reasoning to the source entry;
4. update normative requirements only in the appropriate specification.
