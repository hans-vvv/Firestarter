"""Compliance remediation — project the render delta into a gated, per-role candidate set.

See ADR 0004. Remediation takes a device's compliance diff and decides, per device,
which parts an operator is *permitted* to push back to it. A per-role
``remediation/<role>.yaml`` file drives it with two rule keys plus a conditions gate:

1. **``add``** — the additive half. Its rules reuse the compliance *ignore* grammar
   (``match: startswith|exact|regex`` + ``value``, matched against the normalised
   line) and are tested against the device's **missing-intended** lines
   (``DiffResult.only_in_rendered`` — "missing from device"). A missing line becomes
   an additive candidate only when some ``add`` rule matches it, and the matched line
   itself is what gets pushed — the addition is *derived* from the render delta.

2. **``delete``** — the removal half, and the reason it is separate: a delete command
   cannot be derived from a matched line (the exact MD-CLI ``delete …`` path is not
   the line you see in the diff), so it is stated **explicitly**. A ``delete`` rule
   uses the same match grammar but is tested against the device's **unexpected** lines
   (``DiffResult.only_in_live`` — "unexpected on device"). When a delete rule matches
   at least one such stale line, its ``remediation_commands`` are pushed. These are
   emitted verbatim except for **capture-group substitution**: a ``regex`` rule may name
   groups (``(?P<subnet>\\S+)``) and a command may reference them with ``{subnet}``
   placeholders, which are filled from the matched line. This is how the varying part of
   a line (a subnet, a peer address) reaches an otherwise-fixed command while the rest
   stays literal. A command with placeholders is emitted **once per matched line** — so
   two stale subnets yield two ``delete`` commands — whereas a command with none collapses
   to a single emission however many lines matched. Deny by default: a role with no
   matching rule removes nothing.

3. **Conditions** — the file's ``conditions`` block requires a ``Device.status`` and
   any number of ``Device.labels`` key/values. A device that fails a condition still
   shows its candidates, flagged *blocked* with the reason, so an operator sees "these
   would go, but the device is not ready" rather than the lines silently vanishing.

4. **``base.yaml``** — a role-independent layer applied to **every** device, mirroring
   ``ignore/base.yaml`` (global ignore rules) and ``extra/base.cfg`` (global extra
   config). Its ``add``/``delete`` rules are merged *before* the role's own — coarse →
   fine — so an estate-wide remediation is written once in ``base.yaml`` instead of
   copied into each ``<role>.yaml``. A device is remediable when ``base.yaml`` **or** its
   ``<role>.yaml`` exists; with neither, the role has no policy and nothing is remediable.
   Conditions merge the same coarse → fine way: the role's ``status`` overrides the
   base's when set, and label conditions union with the role winning on key conflicts —
   so ``base.yaml`` can impose a global gate (e.g. only ``active`` devices) that a role
   may tighten or override.

The output is a pure projection of an existing compliance run — no device contact —
that the dashboard renders as a second lens and the deploy path consumes as its plan.
Both halves self-quiesce: an ``add`` closes its own ``only_in_rendered`` gap, and a
``delete`` clears its own ``only_in_live`` line, so once a device is compliant the
rules stop matching and the candidate disappears on its own. A device is reported when
it has *either* additive lines or delete commands, so removing stale config no longer
requires a paired addition — which is the change this key set makes to the earlier
"additive-only, never reads ``only_in_live``" stance (ADR 0004, amended).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.compliance.differ import DiffResult

# The add/delete match grammar IS the ignore grammar — one definition, flipped in
# meaning from "suppress this drift" to "this line may be remediated". Reuse the
# compiler rather than restate it, so the two can never drift apart (ADR 0004).
from app.compliance.normaliser import IgnoreRule, _compile_rules
from app.domain.file_locations import COMPLIANCE_REMEDIATION_LOC
from app.models import Device
from app.repositories.device import get_device_by_hostname

# Resolved under the live data root (repo root by default, FIRESTARTER_DATA when set).
REMEDIATION_DIR = COMPLIANCE_REMEDIATION_LOC.path

# The role-independent layer, merged before every role's own spec (coarse → fine).
# Named to mirror ``ignore/base.yaml`` and ``extra/base.cfg``, the two existing global
# layers. A role literally named "base" would collide with this file — no such role
# exists (roles are pe, core, agg, …), the same harmless theoretical clash the
# ignore layer carries.
BASE_REMEDIATION_FILE = "base.yaml"

# A ``{name}`` placeholder in a delete command: a brace-wrapped Python identifier. The
# identifier form is deliberately narrow so that literal braces in an MD-CLI command
# (were any to appear) are left untouched — only ``{subnet}``-shaped tokens are
# substituted. Shared with the web validator, which cross-checks every placeholder in a
# command against the named groups its rule's regex actually defines.
PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def command_placeholders(command: str) -> set[str]:
    """Return the set of ``{name}`` placeholder identifiers referenced by *command*."""
    return {m.group(1) for m in PLACEHOLDER_PATTERN.finditer(command)}


def _substitute_command(command: str, groups: Mapping[str, str | None]) -> str:
    """Fill ``{name}`` placeholders in *command* from a matched line's capture *groups*.

    *groups* is a delete rule's ``re.Match.groupdict()`` — empty for ``exact`` /
    ``startswith`` rules, which capture nothing. A placeholder with no corresponding
    group (or a group that did not participate in the match, so its value is ``None``)
    is a fail-fast ``ValueError`` rather than a silently malformed command pushed to a
    device: the web validator catches the common case at authoring time, but an optional
    named group that a particular line leaves unset can only surface here.
    """

    def _repl(m: re.Match[str]) -> str:
        name = m.group(1)
        value = groups.get(name)
        if value is None:
            available = sorted(k for k, v in groups.items() if v is not None)
            raise ValueError(
                f"remediation command {command!r} references capture group "
                f"{{{name}}}, which the delete rule's match did not provide "
                f"(available groups: {available})."
            )
        return value

    return PLACEHOLDER_PATTERN.sub(_repl, command)


@dataclass(frozen=True)
class _DeleteMatcher:
    """A compiled ``delete`` rule matcher that also exposes the matched capture groups.

    ``delete`` rules reuse the ignore/add match grammar (``exact`` / ``startswith`` /
    ``regex``), but unlike ``add`` — whose pushed line *is* the matched line, so a bare
    boolean predicate suffices — a delete command is a separate string that may need the
    matched line's captures substituted in. That requires the ``re.Match``, not just a
    yes/no, so delete rules compile to this object instead of the shared boolean lambda.

    It is callable and returns a ``bool`` (so ``any(rule(line) for rule in spec.delete)``
    still reads as before), while :meth:`match` returns the capture ``groupdict`` on a hit
    (``{}`` for the non-regex forms, which capture nothing) or ``None`` on a miss.
    """

    match_type: str
    value: str
    pattern: re.Pattern[str] | None  # set only for ``regex`` rules

    def __call__(self, line: str) -> bool:
        return self.match(line) is not None

    def match(self, line: str) -> dict[str, str | None] | None:
        if self.match_type == "exact":
            return {} if line == self.value else None
        if self.match_type == "startswith":
            return {} if line.startswith(self.value) else None
        # ``regex`` — the only form that can carry named groups for substitution.
        assert self.pattern is not None  # guaranteed by the compiler
        m = self.pattern.search(line)
        return m.groupdict() if m is not None else None


def _compile_delete_matchers(raw_delete: list[dict]) -> list[_DeleteMatcher]:
    """Compile raw ``delete`` rule dicts into :class:`_DeleteMatcher` objects.

    Mirrors the type/value validation of ``normaliser._compile_rules`` (same accepted
    match types, same ``ValueError`` on an unknown one) so a delete rule is validated
    exactly as an add/ignore rule is — but keeps the compiled ``re.Pattern`` around so
    the projection can substitute capture groups into the rule's commands.
    """
    compiled: list[_DeleteMatcher] = []
    for rule in raw_delete:
        match_type = (rule.get("match", "") or "").strip() or None
        value = rule["value"]
        if match_type == "exact":
            compiled.append(_DeleteMatcher("exact", value, None))
        elif match_type == "startswith":
            compiled.append(_DeleteMatcher("startswith", value, None))
        elif match_type == "regex":
            compiled.append(_DeleteMatcher("regex", value, re.compile(value)))
        else:
            raise ValueError(
                f"Unknown delete rule match type {match_type!r}. "
                f"Valid types: exact, startswith, regex."
            )
    return compiled


@dataclass(frozen=True)
class RemediationSpec:
    """Parsed per-role remediation rules.

    Attributes
    ----------
    role_name:
        The ``Role.name`` this spec governs (its file is ``<role_name>.yaml``).
    add:
        Compiled matchers over normalised lines, tested against ``only_in_rendered``.
        A missing-intended line is an additive candidate when
        ``any(rule(line) for rule in add)`` is true; the matched line is pushed.
    delete:
        Compiled :class:`_DeleteMatcher` objects over normalised lines, tested against
        ``only_in_live``. When a rule matches an unexpected-on-device line, its
        ``remediation_commands`` fire, with the line's capture groups substituted in.
    delete_remediation_commands:
        Per-rule operator commands, index-aligned with ``delete`` (entry *i* holds
        rule *i*'s ``remediation_commands``). Each is a near-verbatim MD-CLI command
        (typically ``delete ...``) emitted when that rule matches at least one unexpected
        line on a device — never normalised or filtered, except that ``{name}``
        placeholders are filled from the matched line's named capture groups. Deletes are
        stated here because the exact command cannot be derived from the matched line.
    required_status:
        Value ``Device.status`` must equal for the device to be eligible, or
        ``None`` when the spec imposes no status condition.
    required_labels:
        Mapping every entry of which ``Device.labels`` must match exactly for the
        device to be eligible. Empty when the spec imposes no label condition.
    """

    role_name: str
    add: list[IgnoreRule]
    delete: list[_DeleteMatcher]
    delete_remediation_commands: tuple[tuple[str, ...], ...]
    required_status: str | None
    required_labels: dict[str, str]


@dataclass(frozen=True)
class DeviceRemediation:
    """The remediation candidates for one device, and its eligibility.

    Attributes
    ----------
    hostname:
        Device the candidates belong to.
    role_name:
        The device's role, i.e. which spec was applied.
    eligible:
        True when every condition in the spec is satisfied — the lines may be pushed.
    blocked_reasons:
        Human-readable reasons the device is not eligible; empty when ``eligible``.
    lines:
        The ``add``-matched subset of ``only_in_rendered`` for this device, sorted.
        The additive half of the plan; may be empty when the device only has deletes.
    remediation_commands:
        The verbatim MD-CLI commands from every ``delete`` rule that matched one of
        the device's unexpected lines, in rule then authored order, deduplicated.
        Kept distinct from ``lines`` because they are explicit removal commands, not
        derived render lines — the UI shows them as such and the push sends them after
        ``lines``. May be empty when the device only has additions.

    A device is only ever constructed when at least one of ``lines`` / ``remediation_commands``
    is non-empty — a device with neither is not reported at all.
    """

    hostname: str
    role_name: str
    eligible: bool
    blocked_reasons: tuple[str, ...]
    lines: tuple[str, ...]
    remediation_commands: tuple[str, ...] = ()

    @property
    def line_count(self) -> int:
        """Total commands that would be pushed — additive lines plus delete commands."""
        return len(self.lines) + len(self.remediation_commands)


def _read_raw_spec(path: Path) -> dict | None:
    """Return the parsed YAML mapping at *path*, ``{}`` if present-but-empty, else ``None``.

    The tri-state matters: ``None`` (absent) means "this layer contributes nothing", while
    ``{}`` (present but empty) still counts as a layer that exists — so a bare ``base.yaml``
    makes every role remediable-in-principle even though it adds no rules.
    """
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_remediation_spec(
    role_name: str, *, remediation_dir: Path = REMEDIATION_DIR
) -> RemediationSpec | None:
    """Load and compile the merged remediation spec for *role_name*, or ``None`` if absent.

    The spec is the ``base.yaml`` layer merged with the role's own ``<role_name>.yaml``,
    coarse → fine: ``base.yaml`` applies to every role, ``<role_name>.yaml`` refines it
    (mirroring ``ignore/base.yaml`` and ``extra/base.cfg``). ``None`` is returned only when
    **neither** file exists — the role has no remediation policy and nothing on it is
    remediable. A present-but-empty file (either layer) is not an error; it is loaded so
    its (empty) rule sets are applied consistently.

    Merge semantics:

    - ``add`` / ``delete`` rules concatenate **base-first, then role**, so base rules are
      evaluated before role rules. ``delete_remediation_commands`` stays index-aligned with
      the concatenated ``delete`` list because both are built from the same ordered list.
    - ``conditions`` merge coarse → fine: the role's ``status`` overrides the base's when
      the role sets one; label conditions union with the role winning on key conflicts.
    """
    base_data = _read_raw_spec(remediation_dir / BASE_REMEDIATION_FILE)
    role_data = _read_raw_spec(remediation_dir / f"{role_name}.yaml")
    if base_data is None and role_data is None:
        return None

    base_data = base_data or {}
    role_data = role_data or {}

    # Base first, then role — coarse → fine, so a base rule is tried before a role rule.
    raw_add = (base_data.get("add") or []) + (role_data.get("add") or [])
    raw_delete = (base_data.get("delete") or []) + (role_data.get("delete") or [])

    # The concatenated ``delete`` list feeds both the matchers and their remediation_commands
    # — kept index-aligned because both are derived from this one ordered list.
    delete_remediation_commands = tuple(
        tuple(rule.get("remediation_commands") or []) for rule in raw_delete
    )

    base_conditions = base_data.get("conditions") or {}
    role_conditions = role_data.get("conditions") or {}
    # Role status wins when set (``get`` with the base as default falls back otherwise);
    # labels union with the role overriding the base on any shared key.
    required_status = role_conditions.get("status", base_conditions.get("status"))
    required_labels = {
        **(base_conditions.get("labels") or {}),
        **(role_conditions.get("labels") or {}),
    }

    return RemediationSpec(
        role_name=role_name,
        add=_compile_rules(raw_add),
        delete=_compile_delete_matchers(raw_delete),
        delete_remediation_commands=delete_remediation_commands,
        required_status=required_status,
        required_labels=dict(required_labels),
    )


def evaluate_conditions(*, device: Device, spec: RemediationSpec) -> tuple[bool, tuple[str, ...]]:
    """Return ``(eligible, reasons)`` for *device* against *spec*'s conditions.

    ``eligible`` is true only when the status condition (if any) and every label
    condition hold. ``reasons`` lists each failing condition in readable form; it is
    empty exactly when ``eligible`` is true.
    """
    reasons: list[str] = []

    if spec.required_status is not None and device.status != spec.required_status:
        reasons.append(f"status is {device.status}, needs {spec.required_status}")

    for key, expected in spec.required_labels.items():
        actual = device.labels.get(key)
        if actual != expected:
            reasons.append(f"label {key!r} is {actual!r}, needs {expected!r}")

    return (not reasons, tuple(reasons))


def project_remediation(
    *,
    results: Mapping[str, DiffResult],
    session: Session,
    remediation_dir: Path = REMEDIATION_DIR,
) -> list[DeviceRemediation]:
    """Project a compliance run's per-device diffs into remediation candidates.

    For each device with drift, keep the ``only_in_rendered`` lines its role spec's
    ``add`` rules match (the additive plan), and collect the ``remediation_commands`` of every
    ``delete`` rule that matches one of the device's ``only_in_live`` lines (the
    removal plan). A device is reported only when at least one of the two is non-empty;
    devices with no spec, or no matching rules, are omitted. The result is sorted by
    hostname for stable rendering.

    ``add`` reads "missing from device" and ``delete`` reads "unexpected on device" —
    the two halves of the diff — so a device may be a candidate for additions only,
    removals only, or both.
    """
    specs: dict[str, RemediationSpec | None] = {}
    out: list[DeviceRemediation] = []

    for hostname in sorted(results):
        diff = results[hostname]
        if not diff.only_in_rendered and not diff.only_in_live:
            continue

        device = get_device_by_hostname(session, hostname)
        if device is None:
            continue

        role_name = device.role.name
        if role_name not in specs:
            specs[role_name] = load_remediation_spec(role_name, remediation_dir=remediation_dir)
        spec = specs[role_name]
        if spec is None:
            continue

        # Additive half: the missing-intended lines that an ``add`` rule matches.
        added = tuple(
            sorted(line for line in diff.only_in_rendered if any(rule(line) for rule in spec.add))
        )

        # Removal half: for every ``delete`` rule, the commands it emits for each
        # unexpected-on-device line it matches, with that line's capture groups
        # substituted in. In rule order, then matched-line order, deduplicated
        # (first-seen wins). ``only_in_live`` is a frozenset, so it is sorted before
        # iteration to keep the output deterministic — it matters when substitution makes
        # per-line commands differ; without placeholders the duplicates collapse anyway.
        removed: list[str] = []
        live_lines = sorted(diff.only_in_live)
        for rule, rule_extra in zip(spec.delete, spec.delete_remediation_commands, strict=True):
            if not rule_extra:
                continue
            for line in live_lines:
                groups = rule.match(line)
                if groups is None:
                    continue
                for command in rule_extra:
                    removed.append(_substitute_command(command, groups))
        removed_lines = tuple(dict.fromkeys(removed))

        if not added and not removed_lines:
            continue

        eligible, reasons = evaluate_conditions(device=device, spec=spec)
        out.append(
            DeviceRemediation(
                hostname=hostname,
                role_name=role_name,
                eligible=eligible,
                blocked_reasons=reasons,
                lines=added,
                remediation_commands=removed_lines,
            )
        )

    return out
