"""Add owner_id and visibility to documents + document_sources.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-13 00:01:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default="shared")
        )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])

    with op.batch_alter_table("document_sources") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default="shared")
        )
    op.create_index("ix_document_sources_owner_id", "document_sources", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_document_sources_owner_id", table_name="document_sources")
    op.drop_index("ix_documents_owner_id", table_name="documents")
    with op.batch_alter_table("document_sources") as batch:
        batch.drop_column("visibility")
        batch.drop_column("owner_id")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("visibility")
        batch.drop_column("owner_id")
