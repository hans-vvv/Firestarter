"""Strips noise from rendered and live configs before diffing; applies ignore rules and extra lines."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import yaml
from sqlalchemy.orm import Session

from app.domain.file_locations import COMPLIANCE_EXTRA_LOC, COMPLIANCE_IGNORE_LOC
from app.repositories import get_device_model_by_hostname, get_device_role_by_hostname

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

# Module-level so tests can monkeypatch IGNORE_DIR / EXTRA_DIR at a single point;
# resolved under the live data root (repo root by default, FIRESTARTER_DATA when set).
IGNORE_DIR = COMPLIANCE_IGNORE_LOC.path
BASE_IGNORE_FILE = IGNORE_DIR / "base.yaml"

EXTRA_DIR = COMPLIANCE_EXTRA_LOC.path

# ------------------------------------------------------------------
# SR OS format normalisation patterns
# ------------------------------------------------------------------

_NOISE_PATTERNS: list[re.Pattern[str]] = [re.compile(r"^#")]

_BRACE_RE = re.compile(r"^(configure)\s+\{\s+(.+?)\s+\}$")
_LEADING_SLASH_RE = re.compile(r"^/")


# ------------------------------------------------------------------
# Ignore rule engine
# ------------------------------------------------------------------

IgnoreRule = Callable[[str], bool]

# A redact rule transforms a line by replacing the sensitive part with
# <redacted>, so the structure is compared but the secret value is not.
RedactRule = Callable[[str], str]


def _compile_rules(raw_rules: list[dict]) -> list[IgnoreRule]:
    """Compile YAML rule dicts into callable matchers.

    All rule types use the ``value`` key:

    - ``exact``      : line == value
    - ``startswith`` : line.startswith(value)
    - ``regex``      : re.search(value, line)

    Each rule is compiled into a lambda — a small anonymous function that
    closes over its ``value`` via the ``v=value`` default argument trick.
    Without this trick every lambda in the loop would capture the same
    ``value`` variable by reference and end up using the last value seen.
    By binding ``v=value`` at definition time each lambda gets its own
    private copy of the value it was compiled from.

    The result is a list of ``IgnoreRule`` callables, each of the form
    ``(line: str) -> bool``.  At normalisation time the list is evaluated
    with ``any(rule(line) for rule in ignore_rules)`` — no YAML parsing,
    no string comparisons beyond what the rule itself does.

    Example
    -------
    Given this YAML:

    .. code-block:: yaml

        ignore:
          - match: startswith
            value: "configure system security ssh"
          - match: exact
            value: "configure qos vlan-qos-policy \"default\""

    ``_compile_rules`` produces two lambdas::

        [
            lambda line, v="configure system security ssh": line.startswith(v),
            lambda line, v='configure qos vlan-qos-policy "default"': line == v,
        ]

    So this line is ignored (startswith matches)::

        configure system security ssh server-cipher-list-v2 cipher 190 name aes256-ctr
        # rule(line) -> True -> line dropped

    And this line passes through (neither rule matches)::

        configure router "Base" autonomous-system 65000
        # rule(line) -> False for all rules -> line kept
    """
    compiled: list[IgnoreRule] = []

    for rule in raw_rules:
        match_type = rule.get("match", "").strip() or None
        value = rule["value"]

        if match_type == "exact":
            compiled.append(lambda line, v=value: line == v)

        elif match_type == "startswith":
            compiled.append(lambda line, v=value: line.startswith(v))

        elif match_type == "regex":
            pattern = re.compile(value)
            compiled.append(lambda line, p=pattern: bool(p.search(line)))

        else:
            raise ValueError(
                f"Unknown ignore rule match type {match_type!r}. "
                f"Valid types: exact, startswith, regex."
            )

    return compiled


def _compile_redact_rules(raw_rules: list[dict]) -> list[RedactRule]:
    """Compile YAML redact rule dicts into callable transformers.

    Each rule has a ``match: regex`` with a capture group and a
    ``replace`` string using backreferences (e.g. ``\\1 <redacted>``).
    The pattern is applied with ``re.sub`` so only the sensitive part
    is replaced while the structural prefix is preserved.

    Raises
    ------
    ValueError
        When a rule is missing the ``replace`` key.
    """
    compiled: list[RedactRule] = []

    for rule in raw_rules:
        pattern = re.compile(rule["value"])
        replace = rule.get("replace")
        if replace is None:
            raise ValueError(f"Redact rule is missing 'replace' key: {rule}")
        compiled.append(lambda line, p=pattern, r=replace: p.sub(r, line))

    return compiled


def _load_ignore_rules(
    *, model_name: str, role_name: str, hostname: str
) -> tuple[list[IgnoreRule], list[RedactRule]]:
    """Load and compile ignore + redact rules for *hostname*.

    Loading order (each file is optional except base.yaml):
    1. ``base.yaml``            — always required.
    2. ``<model_name>.yaml``    — loaded when present, silently skipped otherwise.
    3. ``<role_name>.yaml``     — loaded when present, silently skipped otherwise.
    4. ``<hostname>.yaml``      — loaded when present, silently skipped otherwise.

    Each layer adds on top of the previous, allowing coarse-to-fine
    overrides: platform defaults → model quirks → role quirks → device quirks.
    """
    if not BASE_IGNORE_FILE.exists():
        raise RuntimeError(f"Base ignore file not found: {BASE_IGNORE_FILE}.")

    candidates = [
        BASE_IGNORE_FILE,
        IGNORE_DIR / f"{model_name}.yaml",
        IGNORE_DIR / f"{role_name}.yaml",
        IGNORE_DIR / f"{hostname}.yaml",
    ]

    raw_ignore: list[dict] = []
    raw_redact: list[dict] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        raw_ignore.extend(data.get("ignore", []))
        raw_redact.extend(data.get("redact", []))

    return _compile_rules(raw_ignore), _compile_redact_rules(raw_redact)


# ------------------------------------------------------------------
# Extra config loader
# ------------------------------------------------------------------


def _load_extra_lines(*, role_name: str, hostname: str) -> frozenset[str]:
    """Load extra config lines for *hostname* from ``app/compliance/extra/``.

    Extra files contain configuration that must be present on the device
    but is not produced by the config generator — typically for device
    roles that are not yet fully modelled.

    The file format is one flat config statement per line, identical to
    what ``admin show configuration flat`` emits.  The same format
    transforms applied to fetched/rendered configs (leading slash strip,
    brace unwrapping, whitespace strip) are applied here so the lines
    are directly comparable.

    File naming, coarse → fine (all optional):

    - ``base.cfg``         — applies to **every** device (mirrors the way
      ``ignore/base.yaml`` applies globally). Use it for config that must be
      present estate-wide but the renderer does not model — e.g. the local
      ``admin`` user, deliberately dropped from the templates.
    - ``<role_name>.cfg``  — shared extra config for one role.
    - ``<hostname>.cfg``   — per-device extra config.

    When no file exists for *hostname* an empty frozenset is returned —
    extra files are optional.  Only devices with partially modelled
    config need them.
    """
    # Extra lines are the union of the base, role and hostname files, so shared
    # config can be defined once (globally in base.cfg, or per role) and layered.
    candidates = [
        EXTRA_DIR / "base.cfg",
        EXTRA_DIR / f"{role_name}.cfg",
        EXTRA_DIR / f"{hostname}.cfg",
    ]

    lines: set[str] = set()
    for extra_file in candidates:
        if not extra_file.exists():
            continue
        for raw_line in extra_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            line = _LEADING_SLASH_RE.sub("", line)
            line = _unwrap_braces(line)
            if not line:
                # A line that becomes empty after slash-strip / brace-unwrap
                # (e.g. a stray "/" separator) would inject an empty string
                # into the rendered set and surface as a phantom diff entry.
                continue
            lines.add(line)

    return frozenset(lines)


# ------------------------------------------------------------------
# Shared format helpers (module-level so _load_extra_lines can use them)
# ------------------------------------------------------------------


def _unwrap_braces(line: str) -> str:
    match = _BRACE_RE.match(line)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return line


def _is_noise(line: str) -> bool:
    return any(pattern.match(line) for pattern in _NOISE_PATTERNS)


# ------------------------------------------------------------------
# Result container
# ------------------------------------------------------------------


@dataclass(frozen=True)
class NormalisedConfig:
    """Immutable result of normalising one device's flat config text.

    Attributes
    ----------
    hostname:
        Device the config belongs to.
    lines:
        Frozenset of normalised, non-ignored config statements ready
        for symmetric set-diff in the Differ.
    ignored_count:
        Number of lines suppressed by ignore rules.
    extra_count:
        Number of lines injected from the hostname's extra file.
        Zero when no extra file exists for this device.
    """

    hostname: str
    lines: frozenset[str]
    ignored_count: int
    extra_count: int = 0


# ------------------------------------------------------------------
# Normaliser
# ------------------------------------------------------------------


class ComplianceNormaliser:
    """Convert raw SR OS flat config text into a comparable frozenset of lines.

    Normalisation applies to BOTH the rendered (disk) config and the
    live (fetched) config so that the Differ receives two sets that are
    directly comparable.

    Format transforms (applied first, same for both sides)
    -------------------------------------------------------
    1. Split on newlines.
    2. Strip leading/trailing whitespace.
    3. Drop empty lines.
    4. Drop SR OS timestamp comment headers/footers.
    5. Strip leading ``/`` (``/configure`` → ``configure``).
    6. Unwrap brace-singleton lines:
       ``configure { <rest> }``  →  ``configure <rest>``

    Ignore filtering (applied after format transforms)
    ---------------------------------------------------
    7. Load ignore/redact rules in order from ``app/compliance/ignore/``:

       - ``base.yaml``         — always loaded, applies to all devices.
       - ``<model_name>.yaml`` — optional, applies to all devices of this model.
       - ``<role_name>.yaml``  — optional, applies to all devices with this role.
       - ``<hostname>.yaml``   — optional, applies to this device only.

       Each layer adds on top of the previous (coarse → fine).

    8. Drop any line matched by a compiled ignore rule.
    9. Apply redact rules — lines matching a redact pattern have their
       sensitive value replaced with ``<redacted>`` on both sides before
       diffing, so the config structure is compared but secrets are not.

    Extra config injection (applied to rendered side only)
    -------------------------------------------------------
    10. Load extra lines from ``app/compliance/extra/`` in order:

        - ``base.cfg``         — optional, applies to every device.
        - ``<role_name>.cfg``  — optional, shared extra config for the role.
        - ``<hostname>.cfg``   — optional, per-device extra config.

        Lines from all present files are unioned into the rendered frozenset.
        This extends the intended config with statements the generator does
        not yet produce — the Differ then treats them as required config
        that must be present on the device.

    Parameters
    ----------
    session:
        SQLAlchemy session used to look up device model, role, and hostname.
    """

    def __init__(self, *, session: Session) -> None:
        self._session = session
        self._rule_cache: dict[
            str, tuple[list[IgnoreRule], list[RedactRule]]
        ] = {}  # keyed by hostname

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalise(self, *, hostname: str, raw: str) -> NormalisedConfig:
        """Normalise *raw* config text for *hostname*.

        Parameters
        ----------
        hostname:
            Device hostname.
        raw:
            Raw text from ``admin show configuration flat`` or a
            rendered ``.cfg`` file on disk.

        Returns
        -------
        NormalisedConfig
        """
        ignore_rules, redact_rules = self._rules_for(hostname=hostname)

        lines: set[str] = set()
        ignored_count = 0

        for raw_line in raw.splitlines():
            line = raw_line.strip()

            if not line:
                continue
            if _is_noise(line):
                continue

            line = _LEADING_SLASH_RE.sub("", line)
            line = _unwrap_braces(line)
            if not line:
                continue

            if any(rule(line) for rule in ignore_rules):
                ignored_count += 1
                continue

            for redact_rule in redact_rules:
                line = redact_rule(line)

            lines.add(line)

        return NormalisedConfig(
            hostname=hostname,
            lines=frozenset(lines),
            ignored_count=ignored_count,
        )

    def normalise_rendered(self, *, hostname: str, raw: str) -> NormalisedConfig:
        """Normalise the rendered side and inject extra lines if present.

        Identical to ``normalise`` but additionally loads
        ``app/compliance/extra/<hostname>.cfg`` and unions those lines
        into the result.  Call this for the rendered (intended) side
        only — the live side is normalised with ``normalise``.

        Parameters
        ----------
        hostname:
            Device hostname.
        raw:
            Rendered config text from disk.

        Returns
        -------
        NormalisedConfig
            With ``extra_count`` reflecting how many lines were injected.
        """
        base = self.normalise(hostname=hostname, raw=raw)
        role_name = get_device_role_by_hostname(self._session, hostname=hostname)
        if role_name is None:
            raise RuntimeError(f"No role found for hostname {hostname!r}.")
        extra = _load_extra_lines(role_name=role_name, hostname=hostname)

        if not extra:
            return base

        # Apply ignore rules to extra-injected lines too. Without this, a
        # per-host ignore entry has no effect on lines coming from the extra
        # .cfg, since those bypass the rendered-side normalisation path.
        ignore_rules, _ = self._rules_for(hostname=hostname)
        kept_extra = frozenset(
            line for line in extra if not any(rule(line) for rule in ignore_rules)
        )
        suppressed_extra = len(extra) - len(kept_extra)

        return NormalisedConfig(
            hostname=hostname,
            lines=base.lines | kept_extra,
            ignored_count=base.ignored_count + suppressed_extra,
            extra_count=len(kept_extra),
        )

    # ------------------------------------------------------------------
    # Ignore rule loading
    # ------------------------------------------------------------------

    def _rules_for(self, *, hostname: str) -> tuple[list[IgnoreRule], list[RedactRule]]:
        # Cache per hostname because a hostname-level ignore file makes
        # the rule set unique per device, not just per model.
        if hostname not in self._rule_cache:
            model_name = get_device_model_by_hostname(self._session, hostname=hostname)
            role_name = get_device_role_by_hostname(self._session, hostname=hostname)

            if model_name is None:
                raise RuntimeError(f"No device record found for hostname {hostname!r}.")
            if role_name is None:
                raise RuntimeError(f"No role found for hostname {hostname!r}.")

            self._rule_cache[hostname] = _load_ignore_rules(
                model_name=model_name,
                role_name=role_name,
                hostname=hostname,
            )

        return self._rule_cache[hostname]
