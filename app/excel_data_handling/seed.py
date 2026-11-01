"""Seeds the database with initial reference data (roles, sites, prefix pools, etc.)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

from app.domain.file_locations import TOPOLOGY_EXCEL_LOC
from app.models import (
    Address,
    IntegerResourcePool,
    PrefixPool,
    PrefixPoolType,
    Role,
    Site,
)
from app.utils import load_sheet


def _cell(value: Any) -> str | None:
    """Stripped cell text, or ``None`` for an empty / NaN cell."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).strip()
    return text or None


class SeedHandler:
    """
    Seeds the reference data every pipeline run needs before topology is built:
    sites, roles, prefix-pool types, prefix pools and integer resource pools.
    Every seeder is idempotent — rows that already exist are skipped.
    """

    WB_NAME = TOPOLOGY_EXCEL_LOC.path

    def __init__(self, session, wb_name: str | Path = WB_NAME):

        self.session = session
        self.wb_name = wb_name

    def seed_sites(self) -> int:
        """
        Seed Site rows from the 'Site' sheet.

        ``SiteName`` is the only required column and the sole identity of a
        site. The optional ``Address`` / ``Postal Code`` / ``City`` columns, when
        present and filled, are stored on the site's ``Address`` row — purely
        descriptive data that nothing else derives from. A site that already
        exists is left untouched (idempotent).

        Returns number of newly inserted sites.
        """
        df = load_sheet(sheet_name="Site", wb_name=self.wb_name)

        inserted = 0
        seen: set[str] = set()

        for row in df.to_dict("records"):
            site_name = _cell(row.get("SiteName"))
            if not site_name or site_name in seen:
                continue
            seen.add(site_name)

            exists = self.session.scalar(select(Site.id).where(Site.name == site_name))
            if exists:
                continue

            street = _cell(row.get("Address"))
            postal_code = _cell(row.get("Postal Code"))
            city = _cell(row.get("City"))
            address = (
                Address(street=street, postal_code=postal_code, city=city)
                if (street or postal_code or city)
                else None
            )

            self.session.add(Site(name=site_name, address=address))
            inserted += 1

        if inserted:
            self.session.flush()

        return inserted

    def seed_roles(self) -> int:
        """
        Read Role rows from the 'Role' sheet.
        Returns number of newly inserted roles.
        """
        df = load_sheet(sheet_name="Role", wb_name=self.wb_name)

        role_names = sorted({str(v).strip() for v in df["RoleName"].dropna() if str(v).strip()})

        inserted = 0

        for role_name in role_names:
            if self.session.scalar(select(Role).where(Role.name == role_name)) is not None:
                continue

            self.session.add(Role(name=role_name))
            inserted += 1

        if inserted:
            self.session.flush()

        return inserted

    def seed_resource_pools(self) -> int:
        """
        Read ResourcePoolName, StartRange and StopRange rows
        from the 'ResourcePools' sheet.
        If pool exists then insertion is skipped.

        Returns number of inserted pools.
        """
        df = load_sheet(sheet_name="ResourcePools", wb_name=self.wb_name)
        inserted = 0

        for _, row in df.iterrows():
            resource_pool_name = row["ResourcePoolName"]
            range_start = int(cast(Any, row["RangeStart"]))
            range_end = int(cast(Any, row["RangeEnd"]))

            exists = self.session.scalar(
                select(IntegerResourcePool).where(IntegerResourcePool.name == resource_pool_name)
            )

            if exists:
                continue

            self.session.add(
                IntegerResourcePool(
                    name=resource_pool_name,
                    range_start=range_start,
                    range_end=range_end,
                )
            )
            inserted += 1

        if inserted:
            self.session.flush()
        return inserted

    def seed_prefix_pool_types(self) -> int:
        """
        Read Name row from the 'PrefixPoolTypes' sheet.
        If name exists then insertion is skipped.

        Returns number of inserted names.
        """
        df = load_sheet(sheet_name="PrefixPoolTypes", wb_name=self.wb_name)
        inserted = 0

        for _, row in df.iterrows():
            name = row["Name"]

            exists = self.session.scalar(select(PrefixPoolType).where(PrefixPoolType.name == name))

            if exists:
                continue

            self.session.add(
                PrefixPoolType(
                    name=name,
                )
            )
            inserted += 1

        if inserted:
            self.session.flush()
        return inserted

    def seed_prefix_pools(self) -> int:
        """
        Seed PrefixPool rows from the 'PrefixPools' sheet.
        Returns number of newly inserted pools.
        """
        df = load_sheet(sheet_name="PrefixPools", wb_name=self.wb_name)

        inserted = 0

        for _, row in df.iterrows():
            pool_name = str(row["PrefixPoolName"]).strip()
            type_name = str(row["PrefixPoolType"]).strip()
            prefix = str(row["Prefix"]).strip()

            if not pool_name or not type_name or not prefix:
                continue

            if (
                self.session.scalar(select(PrefixPool).where(PrefixPool.name == pool_name))
                is not None
            ):
                continue

            pool_type = self.session.scalar(
                select(PrefixPoolType).where(PrefixPoolType.name == type_name)
            )
            if not pool_type:
                continue

            self.session.add(
                PrefixPool(
                    name=pool_name,
                    prefix=prefix,
                    type_id=pool_type.id,
                )
            )
            inserted += 1

        if inserted:
            self.session.flush()

        return inserted
