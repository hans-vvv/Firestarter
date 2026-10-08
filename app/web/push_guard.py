"""Guardrail so the config an operator *sees* is provably the config *pushed*.

Every "present then push" page (remediation, snippets) shows a
device's command lines and then, on a *separate* request, re-derives and commits
them. The lines shown and the lines committed must be identical — a divergence
pushes something the operator never saw, the worst outcome a network-configuration
tool can have. This module makes the agreement checkable at runtime rather than
merely hoped-for by construction:

- :func:`command_digest` fingerprints the exact line sequence that will hit a device;
- the present step ships that digest to the browser (a hidden form field);
- :func:`digest_matches` re-fingerprints the freshly-derived push lines and reports
  whether they still match — so the push proceeds *only* when the current derivation
  is byte-identical to what was shown. A drift (a code path that diverged), a state
  change between viewing and pushing (a device flipped, a spec was edited), or a
  tampered field all fail the check and stop the push before a device is contacted.

The browser never posts commands, only a digest, so tampering can only cause a
*safe refusal*, never a wrong push: the commands are always re-derived server-side
from trusted state, and the digest merely *confirms* they equal what was shown.

:func:`log_push` writes the audit line — what lines, to which device, by whom, under
which digest — so there is a durable record of exactly what reached each device.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardedCommands:
    """The command lines presented for one device, with their push-guard digest.

    ``lines`` are the exact MD-CLI commands shown on the page and — because the
    present and push paths derive them from the *same* function — the exact lines
    the push will commit. ``digest`` fingerprints that sequence; it rides in a hidden
    form field so the push can refuse if a re-derivation no longer matches what was
    shown. Shared by every present/push scenario (remediation, snippets …)
    so the "shown == pushed" carrier is one type, not one per page.
    """

    lines: list[str]
    digest: str

    @classmethod
    def of(cls, lines: list[str]) -> GuardedCommands:
        """Build from lines, computing the digest — the only way they can disagree."""
        return cls(lines=lines, digest=command_digest(lines))


def command_digest(lines: Sequence[str]) -> str:
    """SHA-256 hex of the exact command sequence that will be pushed.

    Computed over the newline-joined lines *as they will be sent to the device*, so
    the digest changes if any line changes, is added, removed, or reordered — the
    fingerprint is of the whole ordered sequence, not a set. A scenario with no
    lines to push refuses before it ever presents a digest, so there is no
    empty-sequence special case to reason about here.
    """
    joined = "\n".join(lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


DIGEST_MISMATCH = (
    "What this page showed no longer matches what would be pushed, so nothing was "
    "sent. Reload the page to see the current commands, then push again."
)
"""Operator-facing refusal message when the presented digest does not match the
freshly-derived push lines. Deliberately actionable — the fix is always to reload."""


def digest_matches(*, lines: Sequence[str], presented_digest: str) -> bool:
    """True only when *lines* fingerprint to *presented_digest*.

    Fail-closed: a missing or blank presented digest is a mismatch, so a push never
    proceeds without a positive match against what the page showed.
    """
    if not presented_digest:
        return False
    return command_digest(lines) == presented_digest


def log_push(*, hostname: str, lines: Sequence[str], digest: str, username: str) -> None:
    """Record what is about to be pushed, for a durable audit trail.

    Emitted immediately before the device is contacted. Admin-state / service
    snippet lines never contain a credential, so logging them verbatim is safe and
    gives a post-hoc answer to "what exactly did we send that device?" tied to the
    digest the operator confirmed.
    """
    log.info(
        "push to %s by %s — digest %s — %d line(s):\n%s",
        hostname,
        username,
        digest,
        len(lines),
        "\n".join(lines),
    )
