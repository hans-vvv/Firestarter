"""Tests for the device-credential repository.

The store keys each hashed secret by name, so further shared credentials can live
alongside the device-admin password with no schema change. These pin that names
are independent and that verification is per-name.
"""

from __future__ import annotations

from app.repositories.device_credential import (
    get_device_credential,
    set_device_credential,
    verify_device_credential,
)

NAME_A = "secret_a"
NAME_B = "secret_b"
NAME_C = "secret_c"


def test_named_credentials_are_stored_independently(session):
    set_device_credential(session, password="auth-secret", name=NAME_A)
    set_device_credential(session, password="priv-secret", name=NAME_B)

    assert get_device_credential(session, NAME_A) is not None
    assert get_device_credential(session, NAME_B) is not None
    # A name that was never set stays absent.
    assert get_device_credential(session, NAME_C) is None


def test_verification_is_per_name(session):
    set_device_credential(session, password="auth-secret", name=NAME_A)

    assert verify_device_credential(session, password="auth-secret", name=NAME_A) is True
    # Right value, wrong record.
    assert verify_device_credential(session, password="auth-secret", name=NAME_B) is False
    # Wrong value, right record.
    assert verify_device_credential(session, password="nope", name=NAME_A) is False


def test_replacing_a_named_credential_retires_the_old_value(session):
    set_device_credential(session, password="first", name=NAME_C)
    set_device_credential(session, password="second", name=NAME_C)

    assert verify_device_credential(session, password="second", name=NAME_C) is True
    assert verify_device_credential(session, password="first", name=NAME_C) is False
