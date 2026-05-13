"""Initial schema — baseline reflecting the SQLModel definitions.

For SQLite-first installs we simply create the entire schema via
``SQLModel.metadata.create_all``; for Postgres this still works because the
underlying SQLAlchemy DDL is dialect-aware. Future revisions should be
hand-written diffs against this baseline.

Revision ID: 0001
Revises:
Create Date: 2026-05-13 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op  # noqa: F401
from sqlmodel import SQLModel

from app import models  # noqa: F401 — register tables on metadata


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(bind)
