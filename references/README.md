# rcodex References

This folder is the source-only catalog for the rcodex specifications.

Online documents are linked rather than copied into this repository. This avoids stale or incomplete local replicas and preserves the original authors' licenses and update history.

Files in this folder contain source identity, version or revision information, direct links, and a brief description of source contents. Interpretations, inferences, requirements, and rcodex design decisions belong in [`docs/`](../docs/).

Research snapshot dates:

- RLM implementation review: 2026-08-19
- Adjacent implementation review: 2026-08-17
- Official Codex documentation review: 2026-08-19

## Catalog

- [RLM sources](rlm.md): paper, pinned repository, implementation files, and minimal implementation.
- [Codex sources](codex.md): official SDK, App Server, MCP, subagent, and configuration documentation.
- [Related implementations](related-implementations.md): DSPy.RLM and Prime Agent.
- [Technology sources](technology.md): primary Python, packaging, validation, testing, and quality-tool documentation.

## Internal specifications

- [Core implementation spec](../docs/core-spec.md)
- [Deferred-feature backlog](../docs/deferred-features.md)
- [Full target architecture](../docs/recursive-codex-spec.md)
- [Research interpretation and design rationale](../docs/design-rationale.md)
- [RLM implementation alignment](../docs/rlm-alignment.md)
- [Technology stack decision](../docs/tech-stack.md)
- [Specification review](../docs/spec-review.md)

## Referencing policy

When adding a source:

1. Prefer the original paper, official documentation, or primary repository.
2. Pin source-code analysis to a commit or release when practical.
3. Include a direct online link.
4. Briefly record what the source contains.
5. Do not copy an entire external document or source file into this repository.
6. Put all interpretation, comparison, inference, and design reasoning in `docs/`, with links back to the relevant source entries.
