"""add cable.cable_length column

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21

Adds ``cable_length`` (whole metres, default 1) to the existing ``cable`` table
— see ``app.models.Cable``.

Because the schema is historically built by ``Base.metadata.create_all`` (which
already knows about the new column), a fresh database may already have it. The
upgrade is guarded by a column-existence check so ``alembic upgrade head`` is
safe whether the column is present or not.

The model declares only a Python-side ``default=1`` (no DB server default), so
the column carries no ``DEFAULT`` in the schema. On a live database with existing
rows, a plain ``ADD COLUMN ... NOT NULL`` has no value for those rows, so the
migration adds the column nullable, backfills existing rows to 1, then tightens
it to NOT NULL — leaving no lingering server default behind.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cable"
_COLUMN = "cable_length"


def _has_column(bind) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        # No ``cable`` table yet: this is a migrations-only / fresh database where
        # ``create_all`` will later build the table with ``cable_length`` already
        # present. Nothing for this additive migration to do.
        return True
    return any(col["name"] == _COLUMN for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        # Column (or table) already accounted for — nothing to do.
        return

    # Add nullable first so existing rows are accepted, then backfill and tighten.
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))
    op.execute(sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = 1 WHERE {_COLUMN} IS NULL"))
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(_COLUMN, existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if not any(col["name"] == _COLUMN for col in inspector.get_columns(_TABLE)):
        return
    op.drop_column(_TABLE, _COLUMN)
