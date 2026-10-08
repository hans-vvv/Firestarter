# 0003 — Where logic lives: YAML vs template vs handler

**Status:** Accepted

## Context

A configuration line can vary for several different reasons, and there are three places to
express that variation: the YAML definition, the Jinja2 template, or the feature handler.
Choosing badly is not fatal but compounds — variation hidden in a handler is invisible to
whoever reads the template, and names hardcoded in two places make every rename an audit.

The context layer was designed deliberately to pass identifying fields down to templates
(`hostname`, `device_role_name`, `device_model_name`, `tenant`, and per-interface `if_role`)
precisely so that trivial gating stays trivial.

## Decision

Route each piece of variation by **what it depends on**:

| The line varies on… | Put it in | Why |
|---|---|---|
| Role, tenant, model, or `if_role` — already in the device context | **Template** `{% if %}` | Trivially visible where the config is read |
| A naming convention that is still being settled — SROS object names, pool names, well-known interface names, DHCP option strings, derived service names | **YAML** | One obvious place to change during design |
| A DB lookup, pool allocation, topology walk, or cross-device state | **Handler** | Genuine data lifting |

Concretely:

- `underlay.j2` gates pe-only QoS with
  `{% if iface.if_role == "NNI" and device_role_name == "pe" %}` — no handler
  involvement.
- A DHCP-serving VPRN's object names live under `parameters.vprns[].dhcp_server` in YAML;
  the handler passes them straight through with no transformation. **A pass-through string
  in a handler is intentional — do not refactor it into a constant**, that defeats the
  purpose.
- Counter-example: BFD on ISIS interfaces is `bfd-liveness ipv4 true` — a stable CLI keyword
  with no name in it. That belongs in the template, not YAML.

The distinction for strings is **"still-fluid name" → YAML** versus **"stable CLI keyword"
→ template**.

## Consequences

**Good**

- Templates stay readable as the source of vendor-syntax truth; role variation is visible
  in place.
- Renames during design touch one file.
- Handlers stay focused on data lifting rather than emitting role-shaped structures.

**Costs**

- Some judgement required at the boundary — "is this name settled yet?" A name that has
  stabilised can stay in YAML harmlessly; the cost of guessing wrong is low and reversible.
- Logic is distributed across three layers, so a newcomer must learn the routing rule. That
  is what this record is for.

## Related

The test suite follows the same instinct one level up: it owns its fixtures rather than
reading real definitions (see [ADR 0001](0001-code-vs-environment-data.md)), which is why
`tests/fixtures/addressing/` exists and an autouse fixture points the addressing resolver at
it.
