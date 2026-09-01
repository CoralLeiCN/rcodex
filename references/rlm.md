# Recursive Language Model Sources

## Paper

### Recursive Language Models

- Authors: Alex L. Zhang, Tim Kraska, Omar Khattab
- Revision used: v3, revised 2026-05-11
- Link: [arXiv:2512.24601v3](https://arxiv.org/abs/2512.24601v3)
- Source coverage: external context, programmatic context inspection, recursive subcalls, and
  long-context evaluations.

## Reference implementation

### `alexzhang13/rlm`

- Repository: [alexzhang13/rlm](https://github.com/alexzhang13/rlm)
- Commit analyzed:
  [`854e688fbba9d8f8989e3da9989812e4b6dfe270`](https://github.com/alexzhang13/rlm/tree/854e688fbba9d8f8989e3da9989812e4b6dfe270)
- Commit date: 2026-08-25
- Review date: 2026-08-31
- Source coverage: recursive and plain inference, batched calls, depth fallback, limits,
  persistence, compaction, custom tools, callbacks/logging, provider clients, execution
  environments, visualizer input, and the separately scoped training tree.

Pinned inference/runtime files:

- [`rlm/core/rlm.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/core/rlm.py): completion loop, child RLM creation, terminal-depth plain completion, remaining timeout/budget handling, callbacks, persistence, and compaction.
- [`rlm/core/lm_handler.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/core/lm_handler.py): direct and batched model requests, routing, concurrency, and handler protocol.
- [`rlm/core/types.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/core/types.py): public result, usage, callback, and configuration types.
- [`rlm/core/comms_utils.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/core/comms_utils.py): environment-to-handler communication helpers.
- [`rlm/environments/base_env.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/environments/base_env.py): environment contract, helper installation, and recursive-call boundary.
- [`rlm/environments/local_repl.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/environments/local_repl.py): local external-context namespace, direct/recursive helpers, persistence, custom tools, and in-process `exec` behavior.
- [`rlm/logger/rlm_logger.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/logger/rlm_logger.py): nested trajectory metadata and JSONL logging.
- [`rlm/utils/prompts.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/utils/prompts.py): root-loop instructions, helper semantics, orchestration guidance, compaction, and final-answer protocol.
- [`rlm/utils/parsing.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/utils/parsing.py): REPL block extraction and bounded execution-result feedback.
- [`rlm/utils/token_utils.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/utils/token_utils.py): token counting used for compaction decisions.
- [`rlm/clients/base_lm.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/rlm/clients/base_lm.py): common provider-client interface.
- [`README.md`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/README.md): public API, environment/provider matrix, persistence, logging, visualizer, and training overview.

Pinned feature tests:

- [`tests/test_rlm_query.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/tests/test_rlm_query.py): recursive and batched query behavior.
- [`tests/test_depth_metadata.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/tests/test_depth_metadata.py): recursive depth propagation.
- [`tests/test_local_repl_persistent.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/tests/test_local_repl_persistent.py): persistent local environment behavior.
- [`tests/repl/test_custom_tools.py`](https://github.com/alexzhang13/rlm/blob/854e688fbba9d8f8989e3da9989812e4b6dfe270/tests/repl/test_custom_tools.py): root and subcall custom tools.

The upstream [`training/`](https://github.com/alexzhang13/rlm/tree/854e688fbba9d8f8989e3da9989812e4b6dfe270/training) tree was identified only to establish scope. rcodex intentionally does not copy it, expose its workflows, or add its dependencies. The upstream [`visualizer/`](https://github.com/alexzhang13/rlm/tree/854e688fbba9d8f8989e3da9989812e4b6dfe270/visualizer) is likewise not an implemented rcodex UI; rcodex implements the underlying JSON/JSONL trajectory only.

## Minimal implementation

### `alexzhang13/rlm-minimal`

- Repository: [alexzhang13/rlm-minimal](https://github.com/alexzhang13/rlm-minimal)
- Commit analyzed:
  [`973f8d4acf3af2c86dc170af91607bf8b0c4d0ea`](https://github.com/alexzhang13/rlm-minimal/tree/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea)
- Source coverage: compact depth-1 external-context, subcall, and final-answer behavior used in
  the original design comparison.

Pinned implementation files:

- [`rlm/rlm_repl.py`](https://github.com/alexzhang13/rlm-minimal/blob/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea/rlm/rlm_repl.py): fixed root iteration loop and fallback final answer.
- [`rlm/repl.py`](https://github.com/alexzhang13/rlm-minimal/blob/973f8d4acf3af2c86dc170af91607bf8b0c4d0ea/rlm/repl.py): context loading, one-shot sub-LM helper, local code execution, and final-value handling.
