# Architecture Decision Records

Short documents recording **why** a significant decision was made — the context, the choice,
and what it costs. `ARCHITECTURE.md` describes *what* the system is; these describe *why* it
is that way.

The purpose is to stop decisions being silently re-litigated or undone. If you find yourself
thinking "why on earth is it done like this?", the answer should be here — including the
options that were rejected and, where it applies, the assumptions that turned out to be
wrong when tested.

## Format

Each record: **Context** (the forces), **Decision** (what was chosen), **Consequences**
(what it buys and what it costs). Keep them short. A decision that is later reversed is not
deleted — it gets a new ADR that supersedes it, so the history of reasoning survives.

Numbering is sequential; the number never changes.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-code-vs-environment-data.md) | Separate code from environment data | Accepted |
| [0002](0002-minimal-yaml-validation.md) | Validate only mandatory YAML structure | Accepted |
| [0003](0003-where-logic-lives.md) | Where logic lives: YAML vs template vs handler | Accepted |
| [0004](0004-compliance-remediation.md) | Compliance remediation: push the render delta under an allowlist | Accepted |
