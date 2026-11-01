"""drop the migration_job table and its reservation foreign keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03

The ring-migration tool has been removed. This drops its ``migration_job``
table and the ``reserved_by_job_id`` / ``retired_by_job_id`` columns that
pointed at it from ``interface``, ``cable``, ``prefix`` and ``ip_address``
(plus the two CHECK constraints that made reservation and retirement mutually
exclusive on ``prefix`` and ``ip_address``).

Every step is guarded by an existence check so ``alembic upgrade head`` is safe
on a database built by ``create_all`` from the current models (which no longer
know these objects) as well as on a live database that still has them. SQLite
cannot drop a column that carries a foreign key in place, so the column drops go
through ``batch_alter_table`` (copy-and-move).

The downgrade recreates the table and columns empty: the reservation state of
a dropped migration job is not recoverable, and there is nothing to put back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TABLE = "migration_job"

# table -> (column, fk constraint name) pairs that referenced migration_job.id
_FK_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "interface": [("reserved_by_job_id", "fk_interface_reserved_by_job")],
    "cable": [
        ("reserved_by_job_id", "fk_cable_reserved_by_job"),
        ("retired_by_job_id", "fk_cable_retired_by_job"),
    ],
    "prefix": [
        ("reserved_by_job_id", "fk_prefix_reserved_by_job"),
        ("retired_by_job_id", "fk_prefix_retired_by_job"),
    ],
    "ip_address": [
        ("reserved_by_job_id", "fk_ipaddress_reserved_by_job"),
        ("retired_by_job_id", "fk_ipaddress_retired_by_job"),
    ],
}

_CHECKS: dict[str, str] = {
    "prefix": "ck_prefix_reservation_xor_retirement",
    "ip_address": "ck_ipaddress_reservation_xor_retirement",
}


def _columns(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, columns in _FK_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        present = [(col, fk) for col, fk in columns if col in _columns(inspector, table)]
        if not present:
            continue
        # Rebuild the table without the columns. ``batch_alter_table`` recreates
        # it from the reflected definition; the named FKs on these columns go
        # with the columns.
        indexes = {ix["name"]: set(ix["column_names"]) for ix in inspector.get_indexes(table)}
        checks = {ck["name"] for ck in inspector.get_check_constraints(table)}
        with op.batch_alter_table(table) as batch:
            # Batch mode carries the named CHECK constraint over to the rebuilt
            # table, where it would mention columns that no longer exist.
            if _CHECKS.get(table) in checks:
                batch.drop_constraint(_CHECKS[table], type_="check")
            for col, _fk in present:
                # The index on the FK column must go first: batch mode recreates
                # every reflected index on the rebuilt table, and one over a
                # dropped column fails.
                for name, cols in indexes.items():
                    if cols == {col}:
                        batch.drop_index(name)
                batch.drop_column(col)

    if inspector.has_table(_JOB_TABLE):
        op.drop_table(_JOB_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(_JOB_TABLE):
        op.create_table(
            _JOB_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False, unique=True),
            sa.Column("description", sa.String(length=512), nullable=True),
            sa.Column("status", sa.String(length=11), nullable=False),
            sa.Column("snippets_blob", sa.JSON(), nullable=True),
        )

    for table, columns in _FK_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = _columns(inspector, table)
        missing = [(col, fk) for col, fk in columns if col not in existing]
        if not missing:
            continue
        with op.batch_alter_table(table) as batch:
            for col, fk in missing:
                batch.add_column(sa.Column(col, sa.Integer(), nullable=True))
                batch.create_foreign_key(fk, _JOB_TABLE, [col], ["id"])
                batch.create_index(f"ix_{table}_{col}", [col])
            if table in _CHECKS:
                batch.create_check_constraint(
                    _CHECKS[table],
                    "NOT (reserved_by_job_id IS NOT NULL AND retired_by_job_id IS NOT NULL)",
                )
