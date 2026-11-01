"""Tests for the push guard — the digest that proves shown lines == pushed lines.

These pin the pure guard functions in isolation. Their use in a real present/push
flow (that the digest a page carries equals the digest the push re-derives, and that
a mismatch refuses the push) is exercised in ``tests/web/test_snippets.py`` and
``tests/web/test_compliance_remediation.py``.
"""

from __future__ import annotations

import logging

from app.web.push_guard import command_digest, digest_matches, log_push


class TestCommandDigest:
    def test_is_stable_for_the_same_lines(self):
        lines = [
            '/configure lag "lag-1" admin-state enable',
            "/configure port 1/1/1 admin-state enable",
        ]
        assert command_digest(lines) == command_digest(list(lines))

    def test_changes_when_a_line_changes(self):
        base = ['/configure lag "lag-1" admin-state enable']
        changed = ['/configure lag "lag-2" admin-state enable']
        assert command_digest(base) != command_digest(changed)

    def test_changes_when_a_line_is_added(self):
        base = ['/configure lag "lag-1" admin-state enable']
        more = [*base, '/configure lag "lag-2" admin-state enable']
        assert command_digest(base) != command_digest(more)

    def test_is_order_sensitive(self):
        a = [
            '/configure lag "lag-1" admin-state enable',
            '/configure lag "lag-2" admin-state enable',
        ]
        b = list(reversed(a))
        # A reordered push is a different push — the fingerprint must catch it.
        assert command_digest(a) != command_digest(b)

    def test_is_a_hex_sha256(self):
        digest = command_digest(["x"])
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestDigestMatches:
    def test_true_when_lines_match_the_digest(self):
        lines = ['/configure lag "lag-1" admin-state enable']
        assert digest_matches(lines=lines, presented_digest=command_digest(lines)) is True

    def test_false_when_lines_differ(self):
        lines = ['/configure lag "lag-1" admin-state enable']
        other = ['/configure lag "lag-9" admin-state enable']
        assert digest_matches(lines=lines, presented_digest=command_digest(other)) is False

    def test_false_for_missing_digest_is_fail_closed(self):
        lines = ['/configure lag "lag-1" admin-state enable']
        assert digest_matches(lines=lines, presented_digest="") is False


class TestLogPush:
    def test_records_host_user_digest_and_lines(self, caplog):
        lines = ['/configure lag "lag-1" admin-state enable']
        digest = command_digest(lines)
        with caplog.at_level(logging.INFO, logger="app.web.push_guard"):
            log_push(hostname="core1.site", lines=lines, digest=digest, username="admin")

        record = caplog.text
        assert "core1.site" in record
        assert "admin" in record
        assert digest in record
        assert "lag-1" in record
