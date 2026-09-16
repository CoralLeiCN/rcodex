# Design backlog

These items track the RLM alignment review from 2026-09-14. Status distinguishes implemented
and planned work. The [implemented specification](spec.md) remains the current runtime contract.
The design basis is [RLM paper §2](https://arxiv.org/html/2512.24601v3#S2).

## RLM-001: Expose the corpus programmatically inside the REPL

- Status: Implemented (2026-09-15)
- Priority: High

Every recursive REPL now exposes `context.files()`, `context.read()`, and `context.chunks()`.
Controller-mediated reads are lazy, bounded, read-only, and restricted to manifest files.
Integration coverage verifies corpus processing, transformed subcall inputs, recursive child
access, and persistence across turns without source text in parent model messages.
See the [implemented interface](spec.md#44-repl-context-interface).

## RLM-002: Give recursive children their own external context

- Status: Implemented (2026-09-16)
- Priority: High

`rlm_query(..., context=text)` and `rlm_query_batched(..., contexts=[...])` accept
child-specific transformed text independently of the bounded task instruction. The controller
persists it as a private, manifest-backed input; initial model prompts contain paths without
the supplied text. Children inspect it through `context.files/read/chunks` and can pass further
transformations to descendants. Omitted context inherits the parent's current manifest.

Per-query and run-wide UTF-8 byte limits bound transfer and storage. Terminal-depth fallback
keeps the same context on disk for the leaf's read-only local tools. Node records identify each
context manifest, evidence uses the returning node's manifest, and success checks derived-input
integrity. Tests cover large UTF-8 transformations, batches, descendants, empty/inherited inputs,
terminal access, byte limits, invalid RPCs, and absence of source text from initial prompts.
See the [implemented contract](spec.md#45-child-specific-external-context).

## RLM-003: Keep unprinted intermediate results out of model feedback

- Status: Implemented (2026-09-16)
- Priority: Medium

REPL feedback includes execution and call status, variable names/types, bounded error diagnostics,
and explicitly printed output. Successful child answers, child evidence/uncertainties, and tool
values contribute no automatic content previews. Complete accepted results remain in Python
and durable artifacts for dependent calls and final answers.

Integration coverage verifies scalar and batched leaf/recursive results, selected printing,
UTF-8 feedback limits, retained complete values, and failure diagnostics. Prompts explain how
to inspect stored values explicitly. See the [feedback contract](spec.md#521-model-feedback).
