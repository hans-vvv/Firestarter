"""add ce_mgmt_address table

Revision ID: 0001
Revises:
Create Date: 2026-07-16

The project's first Alembic migration. It adds the ``ce_mgmt_address`` table
(persisted, sticky per-CE management IPs — see ``app.models.CeMgmtAddress``)
to the existing, live ``app.db`` without touching any other table.

Because the schema was historically built by ``Base.metadata.create_all`` (which
already knows about this new model), a fresh database may already contain the
table. The upgrade is therefore guarded by an existence check so that
``alembic upgrade head`` is safe to run whether the table is present or not —
it creates the table on legacy databases and no-ops where ``create_all`` already
made it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ce_mgmt_address"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        # Table already present (e.g. a create_all-built database) — nothing to do.
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ce_hostname", sa.String(length=64), nullable=False),
        sa.Column("pair_label", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("ce_hostname", name="uq_ce_mgmt_address_ce_hostname"),
        sa.UniqueConstraint("address", name="uq_ce_mgmt_address_address"),
    )
    op.create_index("ix_ce_mgmt_address_pair_label", _TABLE, ["pair_label"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    op.drop_index("ix_ce_mgmt_address_pair_label", table_name=_TABLE)
    op.drop_table(_TABLE)
