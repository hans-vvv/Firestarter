# 0004 — Compliance remediation: push the render delta under an allowlist

**Status:** Accepted

## Context

Initial provisioning renders a device's full intended configuration and pushes it once.
Afterwards the *model* keeps moving — new services, new devices, template changes — so the
renderer emits new intended lines for devices that are already live. Re-provisioning a live
device wholesale to pick those up is disruptive; what is actually needed is to push just the
incremental delta the model now wants, from wherever the device currently is.

Two of the three pieces already exist:

- The compliance differ already computes that delta. `DiffResult.only_in_rendered` is exactly
  "intended config the renderer produces but the device is missing."
- A safe push engine already exists — `app/automation/deploy.py`, which sends lines in MD-CLI
  config mode under `commit confirmed 1` → `commit confirmed accept`. It cannot lock us out:
  the worst case is a device that self-reverts.

What is missing is the *governance* between them. Applying the render delta to live kit
automatically is too risky — a model change can emit anything, and not every device is in a
state to receive it (still `planned`, admin password not yet set). So the open question is
which lines may be pushed, to which devices, and under what conditions.

## Decision

Remediation pushes the render delta (`only_in_rendered`) onto a chosen subset of live devices,
**operator-triggered**, gated by a **per-role allowlist plus conditions**, with a
**plan-before-push preview**. It never removes live configuration.

1. **The renderer stays the single source of intended config.** The remediation spec is an
   *allowlist filter* over pipeline-produced missing lines. It never introduces a line the
   renderer does not produce. This is the crux: because the pushed lines come from the render
   pipeline, a successful push makes the diff go clean on its own — no paired ignore rule, no
   second source of truth, determinism preserved.

2. **One YAML file per `Role.name`, self-contained.** It carries both what is remediable and
   under what conditions:

   ```yaml
   # remediation/<role>.yaml
   conditions:
     status: active                 # required Device.status ("confirmed operational")
     labels:
       admin_password_set: "True"   # required Device.labels key/values; all ANDed
   allow:
     - match: startswith            # same grammar as compliance ignore/*.yaml,
       value: "configure ..."       # matched AFTER normalisation
     - match: regex
       value: "..."
   ```

   The `allow` grammar is the ignore-file grammar (`match: startswith|exact|regex` + `value`,
   matched against the normalised line) — semantics flipped from "suppress this drift" to
   "this line is permitted to be remediated." **Deny by default**: an empty or missing `allow`
   remediates nothing.

3. **Additive by default.** Remediation closes the "missing intended" gap (`only_in_rendered`)
   and does not read `only_in_live` (unexpected config present on the device). Removing live
   configuration was originally out of scope — it is order-dependent and can strip a subtree,
   and deserved its own decision if ever pursued. That decision was later taken narrowly: see
   the **Amendment — `remediation_commands`** below.

4. **Gates bind to real Device fields.** `conditions.status` compares `Device.status` (e.g.
   `active`); `conditions.labels` requires `Device.labels[k] == v` for every entry. Both are
   existing fields — `admin_password_set` is already set by onboarding.

5. **The push reuses the existing deploy engine.** Admin-only, credentials supplied per run and
   never persisted, server re-derives the lines from the operator's selection (the browser
   never sends config text), plan → confirm → `commit confirmed 1` → `commit confirmed accept`.
   Cannot lock out.

6. **UI: a second compliance presentation, computed as a projection of the same run** — no
   extra device contact. Each `only_in_rendered` line for a device lands in one bucket:
   - **Eligible** — allow-listed *and* conditions pass → selectable for push.
   - **Blocked** — allow-listed but a gate fails → shown *with the reason* (e.g. "device is
     `planned`, needs `active`" / "missing label `admin_password_set`").
   - **Not allow-listed** — stays in the ordinary compliance drift view, never appears here.

   The two views are complementary and non-overlapping: **standard compliance view = all
   drift; remediation view = the allow-listed slice that is actionable (or blocked and why).**

### Rejected alternatives

- **Automatic remediation, no operator step.** Too risky — a model change can emit arbitrary
  config, and pushing it unattended to live kit has no safe failure story worth the saving.
- **Re-provision wholesale / bake everything into templates immediately.** Disruptive, and it
  does not fit the actual need: *extend* an already-provisioned device with the new delta.
- **Hand-authored remediation blocks as their own config source.** This would be a second
  source of intended config living outside the render pipeline. Every pushed line would then
  show as permanent `only_in_live` drift unless mirrored by an ignore rule, and "what config
  does this device have?" would require reading templates *and* remediation YAML. Rejected to
  keep the renderer the single source of truth.
- **Block-granularity matching of the allowlist.** Rejected as overengineering. A render delta
  is already a coherent, tested unit; line-level filtering is sufficient. The one residual —
  an allowlist that slices a rendered block in half — is a spec-authoring concern, and the
  commit-confirmed engine makes even a bad slice non-fatal (the device reverts).

## Consequences

**Good**

- **Determinism preserved.** The renderer stays the only source of intended config; after a
  successful push the compliance diff goes clean by itself, with no paired ignore rule.
- **Reuses proven parts** — the ignore grammar, the commit-confirmed deploy engine, and real
  Device gate fields — rather than building new machinery.
- **Idempotent.** Re-running remediation pushes nothing once a device already matches the
  render; MD-CLI is declarative, so a no-op push is a no-op.
- **One reviewable control surface per role:** which lines, under which conditions, in one
  file. What is *not* allow-listed simply is not pushable.

**Costs**

- The per-role spec files are **environment data** (gitignored, like `ignore/*.yaml`) — they
  must be authored and kept in step with the estate. A stale allowlist silently withholds
  legitimate remediation; that is a deliberate fail-closed trade, but it is a maintenance duty.
- A carelessly written allowlist can slice a rendered block. Non-fatal (the engine reverts),
  but the spec author carries that responsibility.
- The compliance page gains a second lens; a newcomer must learn that "remediation candidates"
  is a filtered projection of drift, not a separate dataset or a separate device fetch.

## Amendment — `remediation_commands` (config-driven removal)

*Status: Accepted, extends decision point 3.*

> Naming note: this key was originally introduced as `extra_lines` and later renamed
> to `remediation_commands` for clarity. References below use the current name.

A production BGP RR migration needed each client to **stop** peering with the old route
reflector as it started peering with the new one. The additive half (peer with the new RR)
was already remediable — those lines are in `only_in_rendered`. The removal half
(`delete router "Base" bgp neighbor "<old>"`) is not: the renderer never emits `delete`, and
the old peer lives in `only_in_live`, which remediation does not read. This is exactly the
"if ever pursued" case point 3 named, and it is now pursued — narrowly.

**Decision.** An `allow` rule may carry an optional `remediation_commands` list: verbatim MD-CLI
commands (typically `delete …`) that are emitted for a device **when that rule matches at
least one of its missing-intended lines**, and pushed alongside the additive lines in the same
commit-confirmed transaction. The removal is thus *gated on the paired addition* — it fires
only where the render delta already justifies touching that config, and it self-quiesces: once
the migration lands the rule stops matching and the `remediation_commands` stop firing.

**Why this shape, and its limit.** Coupling the delete to the render delta keeps the renderer
the driver of *what changes* and keeps `only_in_live` out of the remediation engine entirely —
no second notion of "what is wrong", no unpaired removals authored by hand. The deliberate
consequence: a device that already has the new peer but still carries the old one shows nothing
in remediation; its stale peer surfaces in the ordinary compliance drift view for an operator
to adjudicate. `remediation_commands` are raw commands — not normalised, not matched against anything —
so the spec author owns their exactness; the commit-confirmed push keeps even a wrong one
non-fatal (the device reverts). Deleting already-absent config is a no-op/minor on SR OS, so
re-runs stay idempotent.

## Amendment — `add` / `delete` keys (explicit removals over `only_in_live`)

*Status: Accepted. Renames `allow`→`add` (decision point 2) and supersedes the coupling
limit of the `remediation_commands` amendment above.*

The first amendment coupled every removal to a paired addition, deliberately keeping
`only_in_live` out of the engine. In practice operators needed to remove **stale config that
has no paired addition** — a decommissioned SNMP community, a retired peer left behind on a
device the renderer no longer mentions. Under the coupled model those never appear in
remediation at all; they only sit in the drift view. And overloading a single `allow` rule to
mean both "add these matched lines" *and* "also run these deletes" conflated two very different
things: additions are **derived** from the match (the matched line is what you push), whereas a
delete command **cannot** be derived from a matched line — the exact MD-CLI `delete …` path is
not the line you see in the diff.

**Decision.** Split the single `allow` key into two, each using the same match grammar but
reading a different half of the diff:

- **`add`** (renamed from `allow`) — matched against `only_in_rendered` ("missing from
  device"). The matched line is pushed. May not carry `remediation_commands`.
- **`delete`** (new) — matched against `only_in_live` ("unexpected on device"). Each rule
  carries `remediation_commands`: the explicit verbatim command(s) run when the rule matches at least
  one unexpected line. Removals are now stated, not derived — which is the whole point, since a
  delete cannot be inferred from the match.

A device is a candidate when it has `add` lines **or** `delete` commands, so an unpaired
removal is now expressible. Both halves still self-quiesce (an `add` clears its
`only_in_rendered` line; a `delete` clears its `only_in_live` line), and both are still gated by
`conditions` and pushed in one commit-confirmed transaction.

**Why this reverses "never reads `only_in_live`".** Reading the live side was avoided to keep a
single notion of "what changes" (the renderer) and to prevent hand-authored, unpaired removals.
The `delete` key accepts that trade-off knowingly and narrowly: it never *derives* a removal
(the operator writes the exact command), it is deny-by-default and condition-gated like `add`,
and the commit-confirmed push keeps a wrong command non-fatal. The BGP RR migration from the
first amendment is now expressed as an `add` rule (new peer) plus a `delete` rule (old peer),
each reading its natural side of the diff, instead of a delete riding on the addition's match.
Legacy `allow:` is rejected by the spec editor with a rename hint rather than silently ignored.

## Amendment — capture-group substitution in `delete` commands

*Status: Accepted. Extends the `delete` key above; changes nothing about `add`.*

The `delete` amendment states each removal command verbatim, which works until the command's
target varies per device by a value that is **already in the matched line** — e.g. a DHCP
option-42 removal whose `subnet` differs per pool. Verbatim commands force one `delete` rule per
subnet; the address to remove is fixed but the subnet is not.

**Decision.** A `regex` delete rule may name capture groups (`(?P<subnet>\S+)`), and its
`remediation_commands` may reference them with `{name}` placeholders, filled from each matched
`only_in_live` line before the command is emitted. Everything else stays literal. This mirrors
the existing redact-rule mechanism (`normaliser._compile_redact_rules`), which already carries
capture groups from a matched compliance line into a substitution — the concept is not new to
the codebase, only its second use.

Two consequences follow, both narrow:

- **Emission is now per matched line.** A placeholder command runs once for each `only_in_live`
  line the rule matches (two stale subnets → two deletes); a command with no placeholder still
  collapses to a single emission, so pre-existing specs are unaffected. Matched lines are sorted
  before substitution so the output stays deterministic (a core principle) despite `only_in_live`
  being a set.
- **Placeholders are validated against the rule's own groups.** The spec editor rejects a `{name}`
  with no matching named group in that rule's `value` (and any placeholder on an `exact` /
  `startswith` rule, which capture nothing), so a typo is caught at authoring time rather than
  pushing a literal `{name}`; the engine also fails loud at run time as a backstop. This keeps the
  deny-by-default, "a wrong command is caught not pushed" posture of the `delete` key intact — the
  substitution only fills a value that was already present in the drift the device reported.

## Related

- [ADR 0001](0001-code-vs-environment-data.md) — the per-role remediation spec files are
  environment data: gitignored, travelling by data bundle, exactly like the compliance
  `ignore/*.yaml` files whose grammar they reuse.
- [ADR 0003](0003-where-logic-lives.md) — remediation deliberately does **not** add a fourth
  place for configuration to live. The allowlist *gates* existing rendered output; it does not
  author config. Where a line comes from is unchanged.
- Builds on the compliance differ (`app/compliance/differ.py`, `DiffResult.only_in_rendered`)
  and the snippet-deploy push path (`app/automation/deploy.py`).
