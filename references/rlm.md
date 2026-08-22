# Recursive Language Model Sources

## Paper

### Recursive Language Models

- Authors: Alex L. Zhang, Tim Kraska, Omar Khattab
- Revision used: v3, revised 2026-05-11
- Link: [arXiv:2512.24601v3](https://arxiv.org/abs/2512.24601v3)
- Source coverage: external context, programmatic context inspection, recursive subcalls, and long-context evaluations.

## Reference implementation

### `alexzhang13/rlm`

- Repository: [alexzhang13/rlm](https://github.com/alexzhang13/rlm)
- Commit analyzed: [`caf0bffa1acec17c062559433b4cd4ed92eee3d6`](https://github.com/alexzhang13/rlm/tree/caf0bffa1acec17c062559433b4cd4ed92eee3d6)
- Default branch verification: GitHub `main` pointed to the analyzed commit on 2026-08-19.
- Source coverage: executable semantics, recursion boundaries, batch concurrency, environments, persistence, compaction, usage, and trajectory logging.

Pinned implementation files:

- [`rlm/core/rlm.py`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/rlm/core/rlm.py): main completion loop, final-answer handling, child creation, remaining timeout, and budget propagation.
- [`rlm/core/lm_handler.py`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/rlm/core/lm_handler.py): direct and batched model routing, concurrency, and request protocol.
- [`rlm/environments/local_repl.py`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/rlm/environments/local_repl.py): external context namespace, direct and recursive helpers, persistent state, and local `exec` behavior.
- [`rlm/utils/prompts.py`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/rlm/utils/prompts.py): root-loop instructions, helper semantics, orchestration guidance, and final-answer protocol.
- [`rlm/utils/parsing.py`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/rlm/utils/parsing.py): REPL block extraction and bounded execution-result feedback between root iterations.
- [`README.md`](https://github.com/alexzhang13/rlm/blob/caf0bffa1acec17c062559433b4cd4ed92eee3d6/README.md): supported environments, production warning for the local environment, logging, visualizer, and training overview.

## Minimal implementation

### `alexzhang13/rlm-minimal`

- Repository: [alexzhang13/rlm-minimal](https://github.com/alexzhang13/rlm-minimal)
- Commit analyzed: [`973f8d4acf3af2c86dc170af91607bf8b0c4d0ea`](https://github.com/alexzhang13/rlm-minimal/tree/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea)
- Source coverage: a compact depth-1 RLM implementation with external context, subcalls, and final-answer handling.

Pinned implementation files:

- [`rlm/rlm_repl.py`](https://github.com/alexzhang13/rlm-minimal/blob/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea/rlm/rlm_repl.py): fixed root iteration loop and fallback final answer.
- [`rlm/repl.py`](https://github.com/alexzhang13/rlm-minimal/blob/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea/rlm/repl.py): context loading, one-shot sub-LM helper, local code execution, and final-value handling.
