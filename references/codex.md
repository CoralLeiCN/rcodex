# Official Codex Sources

Only official OpenAI documentation and source repositories are used for Codex capability claims in the specifications.

## Codex SDK

- Link: [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- Source: [OpenAI Codex Python SDK](https://github.com/openai/codex/tree/main/sdk/python)
- Verified: 2026-08-31
- Source coverage:
  - stable Python `openai-codex` package;
  - `AsyncCodex` orchestration;
  - starting, resuming, reading, continuing, and compacting threads;
  - SDK developer instructions on opened threads;
  - the pinned Codex runtime included with published SDK builds;
  - read-only, workspace-write, and full-access sandbox presets.

## Codex App Server

- Link: [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- Verified: 2026-08-31
- Source coverage:
  - thread, turn, and item lifecycle concepts;
  - thread start, resume, fork, compact, archive, and interrupt operations;
  - completed `contextCompaction` items visible through thread reads;
  - JSON-RPC/JSONL stdio transport;
  - streamed events and thread token-usage updates;
  - descendant-thread inspection available on experimental surfaces.

## Codex as an MCP server

- Link: [Use Codex with the Agents SDK / Codex MCP server](https://learn.chatgpt.com/docs/mcp-server)
- Verified: 2026-08-17
- Source coverage:
  - confirming Codex can be exposed as an MCP server;
  - understanding start/reply tool semantics;
  - Agents SDK orchestration examples.

## Codex subagents

- Link: [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- Verified: 2026-08-17
- Source coverage:
  - native parallel child-thread behavior;
  - custom agent profiles;
  - inherited sandbox behavior;
  - concurrency configuration;
  - context isolation.

## Configuration reference

- Link: [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- Verified: 2026-08-18
- Source coverage:
  - agent enablement and concurrency configuration;
  - sandbox and approval settings;
  - tool and web-search configuration;
  - project configuration boundaries.

## Codex MCP configuration

- Link: [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- Verified: 2026-08-31
- Source coverage:
  - MCP configuration shared by local clients on the same Codex host;
  - user-level and trusted-project configuration locations;
  - server capabilities, credentials, and instructions.

## MCP risks and safety

- Link: [MCP and Connectors](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- Verified: 2026-08-31
- Source coverage:
  - external-service data access and action risks;
  - third-party server trust, prompt-injection, and approval guidance;
  - sensitive-data review and retention considerations.
