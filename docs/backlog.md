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

- Status: Backlog
- Priority: High
- Dependency: RLM-001's context interface (implemented)

Currently, `rlm_query(prompt)` becomes a task string capped at 16,384 characters and inserted
into the child's model prompt. Every child receives the original context directory. This does
not provide an external context containing the parent's programmatically transformed input.

Separate the child instruction from its context. Let parent Python supply transformed data or
a context reference that the controller exposes in the child's environment, with only bounded
metadata entering the child's initial model prompt.

Acceptance criteria:

- Single and batched recursive calls accept child-specific context produced by parent Python.
- Child context can exceed the task-string limit under explicit controller resource limits;
  it is not embedded in the child's initial model prompt.
- The child can inspect its supplied context through the REPL and derive further context for
  descendants. Reusing the original directory is not a substitute for delivering that input.
- Terminal-depth fallback defines how the supplied context remains accessible under its limits.
- Tests verify transformed context reaches the correct child, descendants and batches preserve
  their inputs, and source text is absent from initial recursive model prompts.
- Replace affected contracts and update prompts, schemas, tests, and documentation together.

## RLM-003: Keep unprinted intermediate results out of model feedback

- Status: Backlog
- Priority: Medium

Feedback currently includes child-answer and tool-result previews even when Python stores the
results without printing them. Automatic content previews consume parent context and weaken
the program's control over which intermediate values the model sees.

Keep complete returned values in Python variables and durable artifacts. Default feedback
should contain bounded explicitly printed output, execution and call status, variable metadata,
and bounded error diagnostics.

Acceptance criteria:

- Successful unprinted child answers and tool values contribute no content previews to feedback.
- Explicitly printed values remain visible within the configured feedback limits.
- Python can inspect complete returned values and use them in dependent calls or final answers;
  durable artifacts retain the complete results within their configured limits.
- Tests store a distinctive result without printing it and verify its absence from feedback,
  then print a selected portion and verify that only the requested bounded output appears.
- Update feedback prompts, tests, and documentation to reflect the new contract.
