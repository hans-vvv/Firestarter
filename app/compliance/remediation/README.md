# Compliance remediation specs

Per-role rules that decide which parts of a device's compliance diff an operator may
push back to it, and under what conditions. See
[ADR 0004](../../../docs/adr/0004-compliance-remediation.md) for the *why*. A spec has
two rule keys — `add` (config to add, derived from the render delta) and `delete`
(config to remove, stated explicitly) — plus an optional `conditions` gate.

## Files

One file per `Role.name`: `<role>.yaml`. The name is the device's **role, not its
hostname** — `load_remediation_spec` looks up `f"{role_name}.yaml"`. For example the
`core1.<site>` devices have role `core`, so their spec is **`core.yaml`**; a file
named `core1.yaml` after the hostname prefix would be silently ignored (deny by
default → nothing marked). Check the role with `SELECT name FROM role`, not the
hostname. These are **environment data** — gitignored, per-server, travelling by data
bundle like the `ignore/*.yaml` files. This directory's `engine.py`, `__init__.py`,
and this README are tracked; the `*.yaml` specs are not.

**Deny by default.** A role with no file *and* no `base.yaml`, or a file with no
matching `add`/`delete` rule, has nothing remediable.

### `base.yaml` — the role-independent layer

`base.yaml` is applied to **every** device, whatever its role — the remediation
counterpart of `ignore/base.yaml` (global ignore rules) and `extra/base.cfg` (global
extra config). Write an estate-wide remediation here once instead of copying it into
every `<role>.yaml`. `load_remediation_spec` merges `base.yaml` with the device's
`<role>.yaml` **coarse → fine**:

- `add` / `delete` rules from `base.yaml` are evaluated **before** the role's own, then
  the role's are appended.
- `conditions` merge: the role's `status` overrides the base's when the role sets one;
  label conditions union, with the role winning on any shared key. So `base.yaml` can
  impose a global gate (e.g. `status: active` — only remediate live devices) that a role
  may tighten or override.

A device is remediable when `base.yaml` **or** its `<role>.yaml` exists; with neither,
its role has no policy. `base.yaml` is itself editable from the dashboard's remediation
editor like any other spec, and validates against the same grammar.

## Format

```yaml
# pe.yaml  (named after Role.name — the pe1.*/pe2.* devices' role)
conditions:                      # optional — omit for no gating
  status: active                 # required Device.status (your "operational")
  labels:                        # every entry must match Device.labels exactly
    admin_password_set: "True"

add:                             # matched against MISSING-from-device lines
  - match: startswith            # startswith | exact | regex — same grammar as ignore/*.yaml
    value: "configure router \"Base\" bgp"
  - match: exact
    value: "configure system name \"core1\""

delete:                          # matched against UNEXPECTED-on-device lines
  - match: startswith
    value: "configure router \"Base\" bgp neighbor \"192.0.2.17\""
    remediation_commands:                 # the explicit command(s) to run when the rule matches
      - 'delete router "Base" bgp neighbor "192.0.2.17"'
```

### `add` — additive remediation (derived)

An `add` rule is matched against the device's **missing-intended** lines
(`DiffResult.only_in_rendered` — the compliance report's *MISSING from device*). A
missing line becomes a candidate only when some `add` rule matches it, and **the
matched line itself is what gets pushed** — the addition is derived from the render
delta, so it can never introduce config the renderer does not already produce. Values
match the *normalised* line (leading slash stripped, braces unwrapped, whitespace
trimmed), exactly as ignore rules do. `add` rules may **not** carry `remediation_commands`.

### `delete` — removal remediation (explicit)

A `delete` rule is matched against the device's **unexpected** lines
(`DiffResult.only_in_live` — the report's *UNEXPECTED on device*). Removal is a
separate key because the exact MD-CLI `delete …` command **cannot be derived** from a
matched line (a `delete` targets a whole subtree by path, not the line you see in the
diff). So each `delete` rule must carry an `remediation_commands` list: the command(s)
emitted when that rule matches an unexpected line on the device. They are **not**
normalised — get the syntax right — with one exception: capture-group substitution
(below).

#### Capturing values from the matched line

Sometimes the *only* varying part of the command is a value that already appears in the
matched line — a subnet, a peer address — and hardcoding it would mean one rule per
value. A `regex` delete rule may name capture groups, and a command may reference them
with `{name}` placeholders, filled from the matched line:

```yaml
delete:
  - match: regex
    value: 'subnet (?P<subnet>\S+) options option 42 ipv4-address \[10\.89\.70\.172\]'
    remediation_commands:
      - '/configure delete service vprn "CE-DHCP-100" dhcp-server dhcpv4
         "dhcp-server" pool "ce_pool" subnet {subnet} options option 42
         ipv4-address 10.89.70.172'
```

Here the pool's `subnet` varies per device but the `ipv4-address` is fixed, so only the
subnet is captured; the rest of the command is literal. Rules:

- Only `regex` rules capture — `exact` / `startswith` supply no groups, so a placeholder
  on them is a validation error.
- Every `{name}` must be a **named group** the rule's own `value` defines; a typo is
  rejected at authoring time (and fails loud at run time if it ever slips through) rather
  than pushing a literal `{name}` to a device.
- A command with a placeholder is emitted **once per matched line** — two stale subnets
  yield two `delete` commands. A command with no placeholder collapses to a single
  emission however many lines matched (unchanged behaviour).

- A device is a candidate when it has **`add` lines OR `delete` commands** — removing
  stale config no longer requires a paired addition.
- Both halves **self-quiesce**: once the missing line is added it leaves
  `only_in_rendered`, and once the stale line is deleted it leaves `only_in_live`, so
  the rules stop matching and the candidate disappears on its own.
- Deleting already-absent config is a no-op/minor on SR OS, and the commit-confirmed
  push reverts anything the device rejects — a re-run is safe.

### `conditions`

A device that fails a condition still shows its candidate lines in the dashboard,
flagged *blocked* with the reason; the lines are simply not pushable until the device
qualifies. All conditions are ANDed.

### Worked example — BGP RR client migration

```yaml
# pe.yaml — migrate BGP RR clients from the old RR (192.0.2.17) to the new (192.0.2.1)
conditions:
  status: active
add:
  - match: startswith
    value: "configure router \"Base\" bgp neighbor \"192.0.2.1\""   # NEW peer — added
delete:
  - match: startswith
    value: "configure router \"Base\" bgp neighbor \"192.0.2.17\""  # OLD peer — still live
    remediation_commands:
      - 'delete router "Base" bgp neighbor "192.0.2.17"'
```

On each device the renderer wants the new peer (its lines sit in `only_in_rendered`, so
`add` pushes them) and the old peer is still configured (its lines sit in
`only_in_live`, so `delete` fires its explicit removal). Both land in one
commit-confirmed transaction; once the migration is complete neither rule matches and
the candidate is gone.

## Note on granularity

A render delta is a coherent, tested unit. Write `add` rules at the granularity of a
whole rendered block (e.g. `startswith` a subtree root) rather than slicing individual
lines out of it — a half-pushed block may reference config the delta did not include.
The commit-confirmed push makes even a bad slice non-fatal (the device reverts), but
block-whole rules keep intent clean.
