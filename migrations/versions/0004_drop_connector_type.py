"""drop the connector_type reference table

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03

The connector-override page has been removed; ``connector_type`` was the
controlled vocabulary behind its dropdown and nothing else reads it. The
``interface.connector`` column itself stays — the device recipes still set it
and ``underlay.j2`` renders it as ``connector breakout``.

Guarded by an existence check so ``alembic upgrade head`` is a no-op on a
database built by ``create_all`` from the current models.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "connector_type"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=32), nullable=False, unique=True),
    )
