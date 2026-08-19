# RLM Implementation Alignment

Status: source-alignment review
Date: 2026-08-19
Source baseline: [`alexzhang13/rlm` commit `caf0bffa1acec17c062559433b4cd4ed92eee3d6`](../references/rlm.md#reference-implementation), verified as the `main` branch head on 2026-08-19

## 1. Alignment claim

rcodex targets semantic alignment with RLM, not API, transport, or source-code compatibility.

The semantic target is:

1. keep potentially large context outside the model's immediate prompt;
2. let a root reasoning process inspect and transform that context programmatically;
3. let the root create focused one-shot or recursively capable child calls;
4. allow the root to inspect child results and continue working before it finalizes;
5. preserve ordered batched results and explicit recursion limits; and
6. return one final answer with composed usage and trajectory information where available.

rcodex replaces RLM's model-authored Python REPL with Codex-native threads, turns, file and shell tools, and a controller-owned delegation policy. This is an intentional native substitution. It does not make the fixed core workflow an RLM-compatible runtime.

The [Core Spec](core-spec.md) is a feasibility reduction. The `recursive` strategy in the [Full Target Architecture](recursive-codex-spec.md) is the first mode intended to preserve RLM's model-directed control flow.

## 2. Source control flow

The pinned implementation behaves as follows.

### 2.1 Root completion loop

`RLM.completion()`:

1. falls back to a plain LM completion immediately when `depth >= max_depth`;
2. creates an `LMHandler` and a fresh environment, or reuses the environment when `persistent=True`;
3. builds a root system/user message history while the large payload remains in the environment;
4. repeats for at most `max_iterations`:
   - check the parent timeout;
   - optionally compact root message history;
   - call the root LM;
   - extract every fenced `repl` code block from the response;
   - execute those blocks sequentially in the environment's persistent namespace;
   - check error, cost, and token limits after the iteration;
   - return when an execution result contains a captured final answer; otherwise append the LM response and bounded execution output to the next root turn;
5. asks the root LM for a final answer if the iteration cap is reached without an explicit final-answer signal.

RLM therefore supports several inspect → delegate → inspect-result cycles before finalization. A precomputed plan followed by exactly one child wave is a useful reduction, but not the same control flow.

### 2.2 Environment and call types

`LocalREPL` keeps Python state across root iterations and exposes:

- `context` plus versioned `context_N` values;
- `llm_query` for a plain, one-shot model call;
- `llm_query_batched` for bounded concurrent one-shot calls with input-order output;
- `rlm_query` for a child RLM with its own iterative environment when recursion is configured;
- `rlm_query_batched` for bounded concurrent child RLMs with input-order output;
- `SHOW_VARS()` for environment inspection; and
- `answer`, where setting `answer["content"]` and then `answer["ready"] = True` ends the run.

Direct and recursive subcalls are distinct. An RLM `llm_query` child has no REPL loop. An RLM `rlm_query` child receives the supplied prompt as its context, creates its own environment, and may recurse again while depth allows.

### 2.3 Depth and limits

RLM uses root depth `0`. `max_depth` is the depth at which the implementation becomes a plain LM:

- `completion()` at `depth >= max_depth` performs a plain completion;
- a recursive request whose `next_depth >= max_depth` also performs a plain completion instead of creating a child RLM environment.

The child receives the parent's absolute `max_depth`, `max_iterations`, remaining wall-clock timeout, and calculated remaining dollar budget. The configured `max_tokens` value is copied to the child; it is not transactionally reserved from one run-wide token pool. Root limit checks occur after an iteration, so an individual call can overshoot a threshold before the exception is raised.

rcodex intentionally uses a different public depth rule: `max_depth` is the greatest Codex child depth that may be admitted, and a node at that depth has no delegation tool. A denied recursive request is reported to the parent instead of being silently converted. This preserves a bounded terminal child while making policy decisions observable.

### 2.4 Persistence and optional subsystems

RLM `persistent=True` reuses a supported environment across separate `completion()` calls and adds versioned `context_N` and `history_N` values. This is different from crash recovery or resuming one unfinished run.

The repository also implements optional root-history compaction, several local and isolated environments, custom root and subcall tools, root/depth-1 model routing, trajectory metadata and JSONL logging, a visualizer, and a training harness.

## 3. Alignment matrix

| RLM implementation behavior | Core rcodex | Full recursive target | Classification |
| --- | --- | --- | --- |
| Completion-like task/context → answer boundary | `run()` with structured result | Same API across strategies | Aligned |
| Large payload external to root neural context | Directory plus manifest; bodies stay on disk | Immutable context pack and manifest | Aligned through native storage |
| Root programmatically inspects external context | Codex file and shell tools | Codex file, shell, and approved tools | Native substitution |
| Iterative inspect/delegate/inspect loop | Fixed planning, one child wave, synthesis | A Codex turn may interleave context inspection and repeated MCP delegation | Deferred from core; aligned in target |
| Plain `llm_query` | Fresh delegation-disabled Codex leaf | `delegate(mode="leaf")` | Adapted: a Codex leaf is richer than a one-shot LM call |
| Recursive `rlm_query` | Not available | `delegate(mode="recursive")` starts a recursively capable Codex child | Deferred from core; aligned in target |
| Batched direct/recursive calls | Controller launches ordered leaf batch | `delegate_batch` is bounded, concurrent, and ordered | Aligned, with controller-owned admission |
| Child gets its own environment | Fresh Codex thread; shared read-only corpus | Fresh Codex thread and isolated workspace profile | Aligned at reasoning-state level |
| Child context is the exact supplied subcall prompt | Leaf receives task, root objective, references, and may inspect the full core context root | References remain validated navigation hints unless a later scoped-view policy is selected | Intentional broader Codex context access |
| Persistent REPL variables across root iterations | Root Codex thread state across planning/synthesis only | Codex thread state within a node | Native substitution, not Python-state compatibility |
| Persistent environment across separate completions | Not available | Reusable multi-run sessions deferred | Deferred |
| `answer["ready"]` final-answer signal | Strict final result schema | Strict `NodeResult`/`RunResult` plus completed turn | Native substitution |
| Maximum-iteration fallback answer | Controller has explicit planner repair/synthesis policy and deadlines | Codex owns its internal tool loop; controller owns turn/deadline policy | Adapted |
| Max-depth plain-LM fallback | Leaves cannot delegate | Terminal Codex node has no delegation tool; denied calls are explicit | Intentional policy difference |
| Parent remaining timeout/budget passed to child | Hard controller time/structure limits; token/cost observed only | Transactional run-wide reservations and remaining limits | Strengthened |
| Post-iteration cost/token checks | Usage recorded when exposed | Hard only when timely SDK data permits; otherwise observed | Strengthened and explicitly qualified |
| In-process generated Python | Prohibited | Prohibited in the controller | Deliberately excluded |
| Environment choices and external isolation | Local read-only Codex sandbox | Restricted worker/container profile deferred | Deferred |
| Custom root/subcall tools | Disabled in core | Node-scoped allowlisted tools | Deferred |
| Root versus subcall model routing | One configured model for comparable evaluation | Per-node model/tool profiles deferred | Deferred |
| Root-history compaction | Not available | Codex thread compaction after SDK capability proof | Deferred |
| Trajectory logging | Normalized JSON/JSONL lifecycle artifacts | Full causal event store and audit export | Aligned and strengthened |
| Visualizer | Not available | Trace viewer deferred | Deferred |
| Training harness | Not in product scope | Research-only learned recursive policies | Deferred research |

## 4. Required RLM-alignment invariants

The implementation must preserve these invariants even though the underlying agent is Codex:

1. Corpus bodies are not inserted wholesale into root or child prompts.
2. The root decides which subproblems to delegate; the controller validates and admits them but does not invent the reasoning plan in `recursive` mode.
3. A recursive parent can consume one child result and then delegate again within the same node turn, subject to limits.
4. A leaf cannot recurse. A recursive child receives the gateway only below the configured terminal depth.
5. Each child has fresh Codex reasoning state. Parent and child threads are not aliases.
6. Batch results preserve request order and represent per-item failures without discarding successful siblings.
7. The parent receives bounded child results and may continue to inspect the external context before finalizing.
8. Depth, node, concurrency, time, and output limits are controller-owned and cannot be enlarged by model arguments.
9. The final answer and lifecycle state are schema-owned, not inferred from free-form model prose.
10. Direct and recursive strategies are evaluated with comparable model and sandbox settings.

## 5. Intentional deviations

The following are design choices, not alignment defects:

- A Codex leaf remains an agent with its native tool loop rather than emulating RLM's one-shot `llm_query` exactly.
- A terminal-depth child remains a delegation-disabled Codex agent instead of becoming a plain non-agent LM call.
- Recursive requests that cannot be admitted return an explicit structured denial.
- The controller uses pre-admission structural limits and, in the target, transactional reservations instead of copying mutable remaining-budget values into each branch.
- Core children may inspect the full read-only context pack even when the parent supplies references; references are navigation hints, not confidentiality boundaries.
- rcodex never executes model-generated Python inside its controller process.

These deviations must remain visible in documentation and run metadata where they affect evaluation.

## 6. Roadmap consequence

The specification set is aligned when described with these scope labels:

- `direct`: baseline, not recursive;
- core `two-level`: RLM-inspired depth-1 validation topology, not RLM control-flow parity;
- target `recursive`: semantic RLM analogue using Codex-native delegation;
- `native-subagents`: experimental Codex-specific alternative with weaker admission control unless the SDK proves otherwise.

Dynamic recursive parity therefore requires P1. Persistence across separate calls, model/tool routing, compaction, external environment choices, visualization, and training may remain deferred without invalidating the minimal experiment, but the public documentation must not imply those RLM implementation features already exist.
