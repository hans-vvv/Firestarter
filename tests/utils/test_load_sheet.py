"""Tests for :func:`app.utils.utils.load_sheet` — cleaning semantics plus the
per-(workbook, mtime, size, sheet) parse cache."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import app.utils.utils as utils_mod
from app.utils import load_sheet


def _write_xlsx(path: Path, rows: list[dict], *, sheet: str = "Sheet1") -> None:
    with pd.ExcelWriter(path) as xl:
        pd.DataFrame(rows).to_excel(xl, sheet_name=sheet, index=False)


def test_cleaning_normalises_cells(tmp_path):
    wb = tmp_path / "wb.xlsx"
    _write_xlsx(
        wb,
        [
            {"a": "  spaced  ", "b": "", "c": 42},
            {"a": None, "b": None, "c": None},  # fully-empty row → dropped
            {"a": "x", "b": "y", "c": None},
        ],
    )

    df = load_sheet(sheet_name="Sheet1", wb_name=wb)

    assert len(df) == 2  # the all-None row is dropped
    assert df.iloc[0]["a"] == "spaced"  # stripped
    assert pd.isna(df.iloc[0]["b"])  # empty string → missing
    assert df.iloc[0]["c"] == "42"  # non-string coerced to stripped str
    assert pd.isna(df.iloc[1]["c"])  # NaN → missing


def test_returns_independent_copy(tmp_path):
    wb = tmp_path / "wb.xlsx"
    _write_xlsx(wb, [{"a": "one"}])

    first = load_sheet(sheet_name="Sheet1", wb_name=wb)
    first.iloc[0, 0] = "MUTATED"
    second = load_sheet(sheet_name="Sheet1", wb_name=wb)

    # Mutating a returned frame must not leak into a later read from the cache.
    assert second.iloc[0, 0] == "one"


def test_unchanged_file_is_parsed_only_once(tmp_path, monkeypatch):
    wb = tmp_path / "wb.xlsx"
    _write_xlsx(wb, [{"a": "one"}])

    calls = {"n": 0}
    real = pd.read_excel

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(utils_mod.pd, "read_excel", _counting)

    load_sheet(sheet_name="Sheet1", wb_name=wb)
    load_sheet(sheet_name="Sheet1", wb_name=wb)
    load_sheet(sheet_name="Sheet1", wb_name=wb)

    assert calls["n"] == 1  # cached after the first parse


def test_rewritten_file_is_reread(tmp_path):
    wb = tmp_path / "wb.xlsx"
    _write_xlsx(wb, [{"a": "before"}])
    assert load_sheet(sheet_name="Sheet1", wb_name=wb).iloc[0, 0] == "before"

    # Rewrite with different content: mtime and size change, so the cache key
    # changes and the new content is read rather than a stale cached frame.
    _write_xlsx(wb, [{"a": "after-a-much-longer-value-to-change-size"}])
    assert load_sheet(sheet_name="Sheet1", wb_name=wb).iloc[0, 0].startswith("after")


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sheet(sheet_name="Sheet1", wb_name=tmp_path / "nope.xlsx")
