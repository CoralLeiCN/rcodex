# rcodex

A proposed native Recursive Codex runtime inspired by Recursive Language Models.

Implementation baseline: Python 3.11+ with the exactly pinned stable `openai-codex` Python SDK and `AsyncCodex`.

## Specifications

- [Core spec](docs/core-spec.md) — minimal implementation; start here.
- [Deferred features](docs/deferred-features.md) — prioritized post-core backlog.
- [Full target architecture](docs/recursive-codex-spec.md) — complete Recursive Codex design.
- [Design rationale](docs/design-rationale.md) — source interpretation and the reasoning behind major decisions.
- [RLM implementation alignment](docs/rlm-alignment.md) — source control flow, preservation matrix, and documented Codex-native deviations.
- [Technology stack decision](docs/tech-stack.md) — accepted language, SDK, packaging, validation, testing, and tooling choices.
- [Specification review](docs/spec-review.md) — alignment findings, resolved inconsistencies, and remaining C0 decisions.
- [References](references/README.md) — source catalog with direct online links.

## Acknowledgements

rcodex is inspired by the [*Recursive Language Models* paper](https://arxiv.org/abs/2512.24601v3) by Alex L. Zhang, Tim Kraska, and Omar Khattab. We gratefully acknowledge their work and the open-source [`alexzhang13/rlm` reference implementation](https://github.com/alexzhang13/rlm) maintained by Alex Zhang and its contributors.

See the [RLM source references](references/rlm.md) for the paper, pinned repository revision, and implementation links.
