"""Device credential repository — the hashed shared passwords used against devices.

Hashing lives here rather than in the web layer because the device paths need it
too, and importing ``app.web`` from ``app.automation`` would be the wrong way
round. Same primitive as the dashboard accounts (werkzeug scrypt), for the same
reason: it is salted, slow, and already a dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from app.models import DeviceCredential

# The named shared credentials this system stores, each as its own hashed record.
# A named record rather than a bare column is exactly so more credentials need no
# schema change: ``device_admin`` is the production SR OS ``admin`` password.
DEVICE_ADMIN = "device_admin"


def get_device_credential(session: Session, name: str = DEVICE_ADMIN) -> DeviceCredential | None:
    """Return the named credential record, or None if it has never been set."""
    return session.scalar(select(DeviceCredential).where(DeviceCredential.name == name))


def set_device_credential(session: Session, *, password: str, name: str = DEVICE_ADMIN) -> None:
    """Store (or replace) the hash of *password* under *name*.

    Idempotent in shape but not in value: setting it again replaces the hash, so
    the previous password stops verifying. That is intended — this record is the
    single statement of what the production password currently is.
    """
    record = get_device_credential(session, name)
    if record is None:
        session.add(DeviceCredential(name=name, password_hash=generate_password_hash(password)))
    else:
        record.password_hash = generate_password_hash(password)
        record.updated_at = datetime.now(UTC)
    session.flush()


def verify_device_credential(session: Session, *, password: str, name: str = DEVICE_ADMIN) -> bool:
    """Return True if *password* matches the stored hash for *name*.

    Returns False when no record exists, rather than raising: "not configured"
    and "wrong password" are the same answer to the only question the caller is
    asking, which is whether this password may be pushed to a device. The caller
    checks :func:`get_device_credential` separately when it needs to tell an
    operator *why*.
    """
    record = get_device_credential(session, name)
    if record is None:
        return False
    return check_password_hash(record.password_hash, password)
