"""General-purpose utilities: DB session context manager, fail-fast helpers, IP tools, and Jinja2 filters."""

from __future__ import annotations

import inspect
import ipaddress
import json
from collections.abc import Sized
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar, cast

import pandas as pd

from app.db.session import SessionLocal


@contextmanager
def db_session(dry_run: bool = False):
    """
    Provide a transactional database session scope.

    Creates a new `SessionLocal` instance and yields it to the caller.
    On normal completion, the transaction is committed unless `dry_run`
    is True, in which case the transaction is rolled back. If an exception
    occurs within the context block, the transaction is rolled back and
    the exception is re-raised. The session is always closed.

    Parameters
    ----------
    dry_run : bool, optional
        If True, the transaction is rolled back instead of committed.

    Yields
    ------
    Session
        An active SQLAlchemy session instance.
    """
    session = SessionLocal()
    try:
        yield session

        if dry_run:
            session.rollback()
        else:
            session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


T = TypeVar("T")


def require[T](value: T | None, message: str) -> T:
    """
    Ensure that a value is present (not None and not empty).

    Missing means:
      - None
      - "" or whitespace-only strings
      - empty sized containers (len(value) == 0), e.g. [], {}, set(), ()

    Numeric 0 is allowed — it is a real value, not "missing".

    Bools are rejected outright (TypeError). The name "require" reads like
    "require this to be true", but the contract is presence-and-non-emptiness.
    Passing a bool expression — e.g. `require(loc.exists(), ...)` — looks
    like a guard but silently passes when the expression is False, because
    False is "present" per the contract above. Use an explicit
    `if not <condition>: raise ValueError(...)` instead.
    """

    if isinstance(value, bool):
        raise TypeError(
            "require() does not accept bool — its contract is "
            "presence/non-emptiness, not truthiness. Use an explicit "
            "`if not <condition>: raise ValueError(...)` instead."
        )

    missing = False

    if value is None:
        missing = True
    elif isinstance(value, str):
        missing = value.strip() == ""
    elif isinstance(value, Sized):
        # catches list/dict/set/tuple/etc.; does NOT catch 0
        try:
            missing = len(value) == 0
        except TypeError:
            missing = False

    if missing:
        frame = inspect.stack()[1]  # caller of require()
        func_name = frame.function
        cls_name = (
            frame.frame.f_locals["self"].__class__.__name__
            if "self" in frame.frame.f_locals
            else None
        )
        origin = f"{cls_name}.{func_name}" if cls_name else func_name
        raise ValueError(f"[{origin}] {message}")

    return cast(T, value)


def _clean_sheet_cell(x: Any) -> Any:
    """Normalise one cell: NaN → None, strings stripped (empty → None), else str-and-strip."""
    if pd.isna(x):
        return None
    if isinstance(x, str):
        x = x.strip()
        return x or None
    return str(x).strip()


@lru_cache(maxsize=32)
def _load_sheet_cached(path: str, _mtime_ns: int, _size: int, sheet_name: str) -> pd.DataFrame:
    """Read + clean one sheet, memoised on (path, mtime, size, sheet).

    ``pd.read_excel`` re-opens and fully parses the whole workbook on every call,
    so the same immutable input (e.g. the test workbook read ~15x per seeded
    fixture, or ``topology.xlsx`` read several times per pipeline run) was being
    parsed over and over. The cache key includes the file's mtime and size, so a
    replaced workbook is transparently re-read — the result is never stale — while
    an unchanged file is parsed at most once. ``_mtime_ns``/``_size`` are cache-key
    inputs only; :func:`load_sheet` supplies them from ``os.stat``.

    The cached frame is owned by the cache; :func:`load_sheet` hands callers a
    copy so in-place mutation of a returned DataFrame cannot corrupt it.
    """
    df = pd.read_excel(path, sheet_name=sheet_name, dtype=object)

    # `DataFrame.apply` is typed as possibly returning a Series; here it always
    # returns a DataFrame (we map element-wise over every column), so narrow it
    # back so the declared `-> pd.DataFrame` return type holds.
    df = cast(pd.DataFrame, df.apply(lambda col: col.map(_clean_sheet_cell)))

    # Drop fully empty rows
    return df.dropna(how="all")


def load_sheet(*, sheet_name: str, wb_name: str | Path) -> pd.DataFrame:
    """
    Load and normalize an Excel worksheet into a cleaned pandas DataFrame.

    The function reads a sheet from the configured workbook, coerces all values
    to object dtype, and applies cell-level normalization:
    - Missing values (NaN) are converted to None
    - Strings are stripped of surrounding whitespace; empty strings become None
    - Non-string values are coerced to strings and stripped

    Fully empty rows (all values None) are removed.

    The parse is memoised per (workbook, mtime, size, sheet) — see
    :func:`_load_sheet_cached` — so repeatedly reading the same unchanged
    workbook is cheap. A fresh copy is returned each call, so callers may mutate
    the result freely.
    """
    stat = Path(wb_name).stat()
    df = _load_sheet_cached(str(wb_name), stat.st_mtime_ns, stat.st_size, sheet_name)
    return df.copy()


def cidr_to_address_mask(cidr: str) -> tuple[str, str]:
    """Convert a CIDR string (e.g. '10.0.0.1/24') to (address, netmask) for SROS templates."""
    ip = ipaddress.ip_interface(cidr)
    return str(ip.ip), str(ip.network.netmask)


def deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge two dictionaries.

    For each key in `override`:
    - If the key exists in both dictionaries and both corresponding values
    are dictionaries, they are merged recursively.
    - Otherwise, the value from `override` replaces the value in `base`.

    The merge is non-destructive: `base` is shallow-copied before merging,
    and a new dictionary is returned.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Tree(dict[str, Any]):
    """Autovivificious dictionary — accessing a missing key auto-creates a nested Tree."""

    def __missing__(self, key: str) -> Tree:
        """Create and return a new child Tree for an unset key."""
        value = self[key] = type(self)()
        return value

    def __str__(self) -> str:
        """Return a pretty-printed JSON representation."""
        return json.dumps(self, indent=4)


def jprint(dict_: dict) -> None:
    """Helper to print user friendly output"""
    print(json.dumps(dict_, indent=4))


def peer_ip_on_p2p(value: str) -> str:
    """
    Given an IPv4 interface string like '10.0.4.19/31' or '192.0.2.1/30',
    return the peer IP address on the point-to-point subnet.

    Supports only /30 and /31.
    """
    iface = ipaddress.IPv4Interface(value)
    ip = iface.ip
    network = iface.network
    prefix = network.prefixlen

    if prefix == 31:
        # Flip last bit (two-address subnet)
        return str(ipaddress.IPv4Address(int(ip) ^ 1))

    if prefix == 30:
        net = int(network.network_address)
        offset = int(ip) - net

        # Valid host offsets in /30 are 1 and 2
        if offset == 1:
            return str(ipaddress.IPv4Address(net + 2))
        if offset == 2:
            return str(ipaddress.IPv4Address(net + 1))

        raise ValueError(f"{value} is not a usable host address in a /30")

    raise ValueError(f"{value} is not /30 or /31")
