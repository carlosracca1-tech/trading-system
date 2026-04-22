"""add partial take profit fields to positions

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-12 00:00:00.000000

Adds partial_tp_taken (bool) and initial_qty (int) columns to
the positions table. These support the new "sell 50% at +3%" strategy.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "partial_tp_taken",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True after 50% of position was sold at +3% profit",
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "initial_qty",
            sa.Integer(),
            nullable=True,
            comment="Original qty at entry (before partial TP reduces qty)",
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "initial_qty")
    op.drop_column("positions", "partial_tp_taken")
