# 0002 — Validate only mandatory YAML structure

**Status:** Accepted

## Context

Service definitions are YAML consumed by the feature handlers. The obvious instinct is to
validate them fully — a Pydantic model mirroring the whole document, so a malformed
definition fails fast with a precise error.

The problem is that these shapes **evolve constantly**. Every new feature adds keys. A full
schema means updating the validator on every such change — the same maintenance burden as
keeping a parallel dataclass definition in sync, with the same failure mode: the validator
drifts, or people stop adding to it, or it starts rejecting legitimate new structure.

The same tension exists on the other side of the handler: the context dictionaries handlers
return are deliberately **not** schema-enforced either.

## Decision

**Validate only the mandatory structure — the dicts a handler genuinely cannot run without.
Leave optional and evolving keys unvalidated, for the handler to treat gracefully.**

Likewise, do not impose a schema on handler return shapes.

The safety net for the unvalidated remainder is not a schema but the **compliance snapshot**:
a golden, human-reviewed comparison of rendered output. It catches "the output changed"
without anyone hand-maintaining assertions about intermediate structure, and it applies at
the only altitude that actually matters — the configuration that reaches a device.

## Consequences

**Good**

- Adding a feature key costs nothing in validator maintenance.
- Handlers stay free to reshape their intermediate context as designs firm up.
- Change detection happens where it is meaningful (rendered configuration), reviewed
  deliberately rather than asserted rigidly.

**Costs**

- A typo in an optional key is not caught at load time. It surfaces as missing or wrong
  configuration, found by the compliance diff rather than by a validation error.
- The compliance baseline must actually be reviewed each sprint for this to work. It is the
  safety net; treating a diff as noise defeats the decision.

## How to apply

- Adding a new YAML key for a feature: **do not** add it to the validator unless the handler
  would crash without it. Handle it in the handler with `.get()` and a sensible default.
- Reach for a validator only when absence is genuinely fatal.
- Do not add Pydantic models over handler return shapes.
