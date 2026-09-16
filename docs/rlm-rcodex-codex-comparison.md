# Upstream RLM, rcodex, and Codex

This document compares the recursive execution concepts in upstream RLM, rcodex, and an
ordinary Codex task.

Ordinary Codex means a standard Codex task with its general-purpose tool loop and optional
subagent delegation. Current Codex clients can create parallel, inspectable subagent threads,
but those threads do not inherently implement RLM's terminal-versus-recursive call protocol.
rcodex imposes that protocol on Codex and records it as part of its runtime contract.

| Concept | Upstream RLM | rcodex | Ordinary Codex |
| --- | --- | --- | --- |
| Root | Root LM operating a Python REPL | Root retained Codex thread operating a dedicated restricted Python REPL | Current main Codex task/thread |
| Programmatic corpus access | Input stored in the REPL's context variable | Built-in `context.files`, `context.read`, and `context.chunks` lazily deliver manifest-backed text into Python variables through controller RPC | Native tools can inspect files in the agent's ordinary tool loop |
| Terminal leaf | `llm_query()` makes a plain LM call and returns text to Python | `llm_query()` starts a fresh terminal Codex leaf and returns its answer string to Python | No distinct leaf type; the main agent or a subagent completes its assigned work |
| Recursive child | `rlm_query()` creates a child RLM with its own REPL | `rlm_query(..., context=text)` creates a fresh recursive Codex node with its own retained thread, REPL, and external text context; omitted context inherits the parent's manifest | Optional delegated subagent thread, not an automatic `rlm_query()`-style recursion |
| Parallel children | `llm_query_batched()` or `rlm_query_batched()` | The same two batched functions run bounded child calls concurrently | Parallel subagent threads or concurrent independent tool calls |
| Depth boundary | `rlm_query()` falls back to a plain LM call at `max_depth` | A requested recursive node is executed as a leaf with read-only local access to its supplied or inherited context | No RLM-style `max_depth` fallback contract; execution is governed by runtime, concurrency, context, and tool limits |
| Tree recording | Nested completion or trajectory metadata | Explicit normalized `NodeRecord` tree | Main and subagent threads are inspectable, but are not normalized into RLM leaf/recursive `NodeRecord` values |
| Root programming interface | Generated Python executed in an RLM environment | Generated fenced Python executed in a separate restricted worker; hidden JSON-lines RPC connects queries to the controller | General Codex tool calls and shell/program execution, without an injected RLM query API |
| Intermediate interaction | Python can inspect, transform, branch on, and make dependent calls from returned strings | Query/tool results resume the running Python block and persist in variables; model feedback shows explicitly printed output, status, variable metadata, and bounded errors without automatic result previews | The agent reasons from tool/subagent results in its ordinary conversation loop |
| Durable structured output | Completion and trajectory metadata | Strict typed `RunResult`, `NodeRecord`, `IterationRecord`, and JSON/JSONL artifacts; JSON is not the root programming language | Task transcript and product-managed task state, without rcodex's normalized recursive schema |
| File editing | Depends on the selected RLM environment | Codex nodes and the REPL are read-only; trusted caller-supplied controller tools may have caller authority | A normal Codex coding task can edit workspace files when its sandbox and approvals allow it |

The essential distinction is that recursion is part of the algorithm in upstream RLM, rcodex
now exposes the same Python-level query interaction while implementing children as explicit Codex
nodes, and ordinary Codex uses tools and subagents as general capabilities rather than as an RLM
execution protocol. An rcodex Python namespace lasts for one recursive node in one run; resuming a
persistent root Codex thread in a later invocation starts a fresh namespace.

rcodex's context proxy connects corpus inspection with Python-level query calls without copying
source text through the parent model. It preserves the restricted worker boundary and reads at
most 64 KiB per read. Recursive calls can supply transformed text separately from their task
instructions, including distinct inputs for batched calls. The controller stores it in a
child-specific artifact and exposes it through the child's context interface without embedding
it in the initial prompt. Descendants inherit that context or supply a further transformation.
See the [child-context contract](spec.md#45-child-specific-external-context) for limits and
terminal-depth behavior. [RLM-002 is implemented](backlog.md#rlm-002-give-recursive-children-their-own-external-context).

The upstream behavior is pinned in the [RLM sources](../references/rlm.md). Codex subagent
behavior is documented in the [official Codex references](../references/codex.md#codex-subagents).
