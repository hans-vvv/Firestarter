"""drop the bng_cluster table and device.bng_cluster_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03

The BNG / HSI service family has been removed from the demo. ``bng_cluster``
grouped BNG devices with the PE pairs that fed them and nothing else reads it,
so the table goes together with the ``device.bng_cluster_id`` foreign key (and
its index) that pointed at it.

Every step is guarded by an existence check so ``alembic upgrade head`` is safe
on a database built by ``create_all`` from the current models as well as on a
live database that still has the objects. SQLite cannot drop a column carrying
a foreign key in place, so the column drop goes through ``batch_alter_table``
(copy-and-move); the index on the column must be dropped first because batch
mode recreates every reflected index on the rebuilt table.

The downgrade recreates the table and column empty: cluster membership is not
recoverable and the demo has nothing to put back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "bng_cluster"
_COLUMN = "bng_cluster_id"
_FK = "fk_device_bng_cluster"
_INDEX = "ix_device_bng_cluster_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("device"):
        columns = {col["name"] for col in inspector.get_columns("device")}
        if _COLUMN in columns:
            indexes = {
                ix["name"]: set(ix["column_names"]) for ix in inspector.get_indexes("device")
            }
            with op.batch_alter_table("device") as batch:
                for name, cols in indexes.items():
                    if cols == {_COLUMN}:
                        batch.drop_index(name)
                batch.drop_column(_COLUMN)

    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        )

    if inspector.has_table("device"):
        columns = {col["name"] for col in inspector.get_columns("device")}
        if _COLUMN not in columns:
            with op.batch_alter_table("device") as batch:
                batch.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))
                batch.create_foreign_key(_FK, _TABLE, [_COLUMN], ["id"])
                batch.create_index(_INDEX, [_COLUMN])
