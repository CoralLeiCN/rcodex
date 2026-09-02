# Repository Rules

## Compatibility policy

This repository is pre-release. Until this rule is explicitly changed, do not preserve backward
compatibility.

- Replace changed APIs, CLI contracts, schemas, persisted formats, prompts, and runtime
  configuration directly.
- Delete superseded code, tests, fixtures, generated results, documentation, schema versions,
  migration shims, aliases, and redirect stubs.
- Keep only tests and artifacts that validate the current contract.
- Update or delete every internal reference to a superseded contract in the same change.
- Do not add compatibility layers or retain legacy behavior unless the user explicitly changes
  this policy.

This policy applies to repository contents. It does not authorize deletion of user data or
external resources.
