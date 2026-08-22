# Recursive Codex Technology Stack Decision

Status: accepted for core implementation
Decision: ADR-001
Date: 2026-08-19

## 1. Scope and precedence

This document is the source of truth for implementation language, runtime integration, packaging, dependencies, testing, and engineering tooling.

- The [Core Spec](core-spec.md) controls which product features are implemented first.
- This document controls how those features are implemented.
- [Deferred Features](deferred-features.md) controls when additional infrastructure may enter the stack.
- The [Full Target Architecture](recursive-codex-spec.md) describes the later system without overriding core scope or this stack decision.

If a dependency is not listed as core here, adding it requires a documented need and an update to this decision.

## 2. Decision drivers

The stack optimizes for:

1. the user-required Codex Python SDK;
2. a small, inspectable core with few runtime dependencies;
3. deterministic concurrency, cancellation, and cleanup;
4. strict validation at every model and persistence boundary;
5. reproducible local development and CI;
6. an adapter boundary around the evolving Codex SDK;
7. a clean path to dynamic recursion without bringing its infrastructure into core.

## 3. Accepted core stack

| Area | Decision | Core use |
| --- | --- | --- |
| Language | Python 3.11+ | Application and library implementation |
| Codex runtime | Stable `openai-codex`, pinned to one exact version | All Codex threads and turns |
| Codex client | `AsyncCodex` | Root and concurrent leaf orchestration |
| Concurrency | Standard-library `asyncio` | `TaskGroup`, semaphores, deadlines, cancellation |
| Boundary models | Pydantic v2 | Strict request, plan, result, event, and artifact validation |
| CLI | Standard-library `argparse` | `rcodex run`; no CLI framework in core |
| Packaging | `pyproject.toml`, `src/` layout, Hatchling | Wheel, editable install, and console entry point |
| Dependency workflow | `uv` with committed `uv.lock` | Reproducible development and CI environments |
| Persistence | Standard-library JSON and JSONL | Atomic artifacts and append-only events |
| Tests | `pytest` and `pytest-asyncio` | Unit, fake-adapter integration, and opt-in live tests |
| Lint and format | Ruff | One formatter and linter |
| Static typing | mypy in strict mode | Public and internal boundaries |

The only core runtime dependencies are:

- `openai-codex==<version-selected-by-C0>`;
- `pydantic>=2,<3`.

`hatchling` is a build dependency. `pytest`, `pytest-asyncio`, Ruff, and mypy are development dependencies. Exact resolved versions are committed in `uv.lock`.

## 4. Why Python 3.11+

The official Codex Python SDK requires Python 3.10 or later. rcodex intentionally raises the floor to Python 3.11 to use standard-library structured-concurrency primitives, especially `asyncio.TaskGroup` and `asyncio.timeout`, without compatibility helpers.

Concurrency rules:

- The run orchestrator owns task creation; model output never creates Python tasks directly.
- A semaphore enforces `max_concurrency` before a leaf starts.
- Each leaf converts expected runtime, timeout, and model failures into a structured result so one failed leaf does not cancel its siblings.
- Unexpected controller exceptions escape the leaf wrapper and let `TaskGroup` cancel the group.
- Code that catches `asyncio.CancelledError` performs cleanup and re-raises it.
- Deadlines use the event loop's monotonic clock; persisted timestamps use UTC.

## 5. Codex SDK integration

All Codex execution goes through the stable Python `openai-codex` SDK and its bundled pinned runtime.

Required rules:

- Use `AsyncCodex`; do not mix synchronous SDK calls into the event loop.
- Start a fresh thread for each leaf and continue the original root thread for synthesis.
- Pass `Sandbox.read_only` explicitly for every core thread and turn; do not rely on a configured default.
- Keep SDK objects and exceptions inside `CodexAdapter`.
- Store only stable rcodex models outside the adapter.
- Let the SDK own Codex authentication; rcodex does not implement or persist a credential store.
- Record the selected SDK version, bundled runtime version when exposed, thread IDs, model, reasoning effort, sandbox, and effective capability flags in run metadata.
- Do not set `codex_bin` unless an explicit compatibility test requires a locally selected executable.
- Do not call a raw App Server protocol or launch `codex exec` or `codex mcp-server` as the core agent runtime.

C0 must verify whether one `AsyncCodex` instance can safely serve all concurrent threads. Until that test passes, client-sharing behavior remains an adapter implementation detail rather than a public contract.

Any desired SDK feature that cannot be proven through the pinned version remains deferred. The specification must not infer Python SDK support solely from App Server or TypeScript SDK documentation.

## 6. Validation and schemas

Use Pydantic v2 at untrusted boundaries:

- CLI and Python run requests;
- planner output;
- leaf results;
- final run results;
- stored JSON artifacts and normalized events.

Boundary models use strict validation and reject unknown fields. They also enforce explicit string, collection, and integer bounds. The controller checks raw byte limits before JSON parsing and again after canonical serialization.

Every persisted contract includes a schema version:

- JSON objects: `schema_version`;
- JSONL events: `event_schema_version`;
- prompts: `prompt_template_version` and a content hash.

Schema changes follow these rules:

- additive optional fields may keep the current major schema version;
- renamed, removed, or semantically changed fields require a new major version;
- readers fail clearly on unsupported major versions;
- migrations are deferred until durable resume is implemented.

## 7. Packaging and dependency policy

Repository conventions:

```text
pyproject.toml
uv.lock
src/rcodex/
tests/
docs/
references/
```

Use a `src/` layout so tests exercise the installed package instead of accidentally importing a repository-root package. Configure the console script as `rcodex = "rcodex.cli:main"`.

Dependency rules:

- Pin `openai-codex` exactly because it defines the runtime protocol boundary and bundles a Codex runtime.
- Give other direct dependencies a bounded compatible range in `pyproject.toml`; resolve every transitive version in `uv.lock`.
- Commit `uv.lock` and use `uv sync --locked` in CI.
- Pin the `uv` release used by CI so lockfile behavior is reproducible.
- Keep runtime, test, and development dependency groups separate.
- Do not read credentials from a committed `.env` file or add a dotenv dependency in core.
- Review dependency upgrades deliberately; an automated update may open a change but must not silently rewrite the lock during CI.

## 8. CLI and configuration

Core uses `argparse` because it needs one command and a small option set. Add Typer, Click, Rich, or a TUI only when the interface becomes complex enough to justify another dependency.

The default state directory is `./.rcodex`, resolved from the invocation directory. The resolved `--state-dir` and `--output` paths must be outside the context root. This prevents Codex from reading prior or in-progress results as corpus input and keeps the input-integrity check meaningful. If the default overlaps the context, rcodex fails closed and asks for an external state directory.

Configuration precedence is deterministic:

1. explicit Python API arguments;
2. explicit CLI flags;
3. rcodex-owned defaults.

Ambient Codex configuration may affect the bundled runtime, so the adapter records the effective capabilities it can observe. Core does not add YAML configuration or automatic environment-file loading. Machine-readable run requests and outputs use JSON.

CLI output contract:

- progress and diagnostics go to stderr;
- the requested result goes to stdout or `--output`, never both;
- a result artifact is attempted for every run that starts;
- exit `0` means succeeded, `1` means failed, `2` means invocation/configuration error, `3` means partial, `124` means timed out, and `130` means interrupted.

## 9. Persistence and observability

Core has one controller process and no database.

- Write JSON artifacts to a temporary file in the destination directory, flush them, and atomically replace the destination.
- Treat `events.jsonl` as append-only; write one bounded JSON object per line and flush state-transition events.
- Give every event a monotonically increasing run-local sequence number; timestamps alone do not define order.
- Use deterministic UTF-8 JSON serialization with sorted keys and fixed separators for hashes and fixtures.
- Generate opaque run and event IDs with standard-library UUIDs.
- Never use model prose as lifecycle state.
- Store normalized rcodex events, not every raw SDK or shell event, in core.
- Redact secrets and avoid persisting full source content, prompts, or command output by default.

Run metadata records enough information to reproduce or explain a result: rcodex version, Python version, platform, SDK version, exposed bundled-runtime version, model, reasoning effort, effective limits, sandbox, prompt version, and manifest hash.

## 10. Test and quality gates

Test layers:

1. Unit tests cover schemas, path containment, manifest determinism, state transitions, limits, ordering, and status reduction.
2. Integration tests use a fake `CodexAdapter` to exercise planning, concurrency, timeout, cancellation, retries, and artifacts deterministically.
3. Live SDK tests verify only capabilities that a fake cannot prove. They are opt-in, serialized where necessary, and excluded from ordinary pull-request checks because they require authentication and consume usage.
4. Evaluation suites compare `direct` and `two-level` with identical model settings and immutable fixtures.

Required local and CI checks:

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv build
```

Configure pytest with `--import-mode=importlib` so tests do not mutate `sys.path`. Configure Ruff and mypy in `pyproject.toml`, target Python 3.11, and require an explicit review before enabling preview or newly introduced rule families.

CI runs offline unit and fake-adapter integration tests on the oldest supported Python version and the newest version supported by the pinned SDK. At least one job runs on Linux; a macOS job covers platform-specific path and process behavior. Live Codex tests use a separate manually triggered or protected workflow.

## 11. Core exclusions and deferred stack

| Technology or capability | Core | Earliest admission |
| --- | :---: | --- |
| Raw App Server JSON-RPC client | No | Only after an explicit replacement ADR |
| TypeScript Codex SDK | No | Only after an explicit replacement ADR |
| MCP SDK and delegation server | No | P1 dynamic recursion |
| SQLite | No | P1 budget ledger; broader projection in P2 |
| Containers or restricted workers | No | P2 external isolation |
| OpenTelemetry | No | P2/P3 when a service or trace backend exists |
| FastAPI or another HTTP framework | No | P3 service API |
| Task queue or distributed scheduler | No | Research/remote workers |
| Typer, Click, Rich, or TUI framework | No | P3 CLI/UI expansion if justified |
| Binary document parsers | No | P3 format adapters |
| Agents SDK | No | Research into alternative orchestration |

Deferred technology is not preselected unless the target specification requires a protocol. At implementation time, compare maintained options, record versions and security implications, and update this ADR before adding dependencies.

## 12. Rejected alternatives

### TypeScript as the primary language

Rejected because the project explicitly requires the Codex Python SDK and Python is a natural fit for the RLM-inspired evaluation and orchestration work. A second runtime would duplicate models, packaging, and lifecycle logic.

### Raw App Server integration

Rejected because it would couple rcodex to a lower-level transport and lifecycle schema already managed by the Python SDK. App Server remains a capability reference, not the integration boundary.

### Codex CLI subprocesses as nodes

Rejected because parsing subprocess output would weaken typed lifecycle, result, and thread handling. The SDK already owns the local runtime process.

### A framework-heavy core

Rejected for the first version because a web framework, task queue, ORM, telemetry stack, and UI do not help validate the direct-versus-two-level product hypothesis.

## 13. Review triggers

Revisit this decision when:

- the pinned SDK cannot satisfy a core acceptance criterion;
- an SDK upgrade changes thread, sandbox, output, usage, or cancellation behavior;
- dynamic MCP recursion enters implementation;
- durable resume requires transactional persistence;
- write-capable or security-sensitive workloads require external isolation;
- the project becomes a hosted or multi-user service;
- Python 3.11 reaches the project's support-policy cutoff.

Every revision must update the date, the affected specifications, the lockfile policy if needed, and the source catalog.
