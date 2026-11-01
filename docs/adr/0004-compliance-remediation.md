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

