from __future__ import annotations

"""Tests for ``SeedHandler.seed_sites``.

The ``Site`` sheet identifies a site by ``SiteName`` alone; the postal columns
(``Address``, ``Postal Code``, ``City``) are optional descriptive data stored on
the site's ``Address`` row when filled.
"""

import openpyxl
from sqlalchemy import select

from app.excel_data_handling.seed import SeedHandler
from app.models import Site


def _workbook(tmp_path, header, *rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Site"
    ws.append(header)
    for row in rows:
        ws.append(row)
    path = tmp_path / "sites.xlsx"
    wb.save(path)
    return path


def test_seeds_sites_with_optional_postal_address(session, tmp_path):
    path = _workbook(
        tmp_path,
        ["SiteName", "Address", "Postal Code", "City"],
        ["ams-001", "Stationsplein 1", "1012 AB", "Amsterdam"],
        ["rtm-001", None, None, None],
    )

    inserted = SeedHandler(session=session, wb_name=path).seed_sites()

    assert inserted == 2
    ams = session.scalar(select(Site).where(Site.name == "ams-001"))
    rtm = session.scalar(select(Site).where(Site.name == "rtm-001"))
    assert ams is not None
    assert ams.address is not None
    assert (ams.address.street, ams.address.postal_code, ams.address.city) == (
        "Stationsplein 1",
        "1012 AB",
        "Amsterdam",
    )
    assert rtm is not None
    assert rtm.address is None


def test_site_name_is_the_only_required_column(session, tmp_path):
    path = _workbook(tmp_path, ["SiteName"], ["tst-001"])

    assert SeedHandler(session=session, wb_name=path).seed_sites() == 1
    assert session.scalar(select(Site).where(Site.name == "tst-001")) is not None


def test_seed_sites_is_idempotent_and_skips_blank_rows(session, tmp_path):
    path = _workbook(
        tmp_path,
        ["SiteName", "Address", "Postal Code", "City"],
        ["tst-001", None, None, None],
        [None, None, None, None],
        ["tst-001", "changed", None, None],
    )
    seeder = SeedHandler(session=session, wb_name=path)

    assert seeder.seed_sites() == 1
    assert seeder.seed_sites() == 0
    assert len(session.scalars(select(Site)).all()) == 1
